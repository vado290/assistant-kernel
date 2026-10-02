# 历史归档：多图注册、中断与恢复（0005，已被 0006 替代）

此文不再描述当前实现。当前实现见 ../PYTHON_STAGES.md。

## 范围

复用 `workflow_versions / workflow_runs / control_jobs / approval_requests /
approval_decisions / outbox_events`。没有增加容器、第二套任务库或工作流引擎。
现有 `python_script` Routine Workflow 不变；ProjectBuild 节点和路由不变，
外层切换为统一 GraphProcessor。原 builds、decisions API 保留。

调用链：

```text
注册 .py → 静态检查工厂 → 按 SHA256 保存快照 → workflow_versions(draft)
验收后激活 → current version 指针
创建 run → 固定 workflow_version_id + 唯一 graph_thread_id → graph_start job
Worker → 查询版本 → 校验快照与运行时版本 → 动态加载工厂 → compile → invoke
interrupt → checkpoint → 本轮全部 approval + outbox → waiting_user
回复 approval_id → 查询 run / 校验身份、轮次、幂等 → 收集本轮全部答案
答案齐全 → 一个 graph_resume job，携带 interrupt_id→answer
Worker → 同版本工厂 + 同 thread checkpoint → Command(resume=map)
完成 → run/task 终态 + outbox + 本次 job receipt
```

## 图工厂契约

在 Project Vault 中准备一个自包含 Python 模块，例如 `workflows/review.py`：

```python
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

class State(TypedDict, total=False):
    draft: str
    answer: object

def build_graph(*, checkpointer, services):
    def review(state):
        answer = interrupt({"question": "请审核", "draft": state["draft"]})
        return {"answer": answer}
    graph = StateGraph(State)
    graph.add_node("review", review)
    graph.add_edge(START, "review")
    graph.add_edge("review", END)
    return graph.compile(checkpointer=checkpointer)
```

- 工厂必须为同步顶层函数，返回使用 Kernel 提供 checkpointer 的 CompiledStateGraph。
- 当前执行器调用同步 invoke；异步工作可按现有 ProjectBuild 方式在工作线程中封装。
- 使用动态 `interrupt()`，不使用静态 interrupt_before/after。
- 支持作为图节点登记的可检查子图；不要在节点内部动态隐藏子图结构。
- 不在模块导入/工厂构造阶段执行业务；这些代码会在新进程/新调用执行。
- 不在全局变量保存某次运行的状态。状态放 Graph State、数据库或 Vault。
- 单文件快照不支持相对导入。标准库/已部署依赖可用；外部工具、服务和依赖包
  仍需独立版本治理，并不因保存一个 .py 就冻结整个运行环境。
- `services` 是受控依赖注入接口，现提供 `stage_executor` 给系统 ProjectBuild。
- 节点重试不是 exactly-once 外部副作用保证。付款、发信、文件发布仍须业务幂等键/Outbox。

## API

以下请求全部使用现有 Kernel Bearer 认证。

1. `POST /v2/projects/{project_id}/workflows/{workflow_id}/versions`

```json
{
  "definition_relative_path": "workflows/review.py",
  "input_schema": {"type":"object","required":["draft"]},
  "output_schema": {"type":"object"},
  "spec": {
    "executor": {
      "type": "langgraph_factory",
      "factory": "build_graph",
      "trusted_code": true
    }
  }
}
```

注册不会执行该模块。原文件被复制为
`vault://{project}/.graph-definitions/{sha256}.py`；版本行记录路径、哈希、工厂名
及 Python/LangGraph/checkpoint 包版本。原文件后续修改不影响已登记版本。
快照被修改、缺失、运行时版本不匹配时拒绝执行，不回退到最新版。
`system-project-build` 是保留名称。

2. `POST /v2/workflow-versions/{id}/activate`，仍需 tests_uri、review_uri、
   accepted_by、acceptance_evidence。旧 active 版本退休，但已绑定运行继续使用旧版本。
3. `GET /v2/projects/{project}/workflows/{workflow}/versions` / `GET /v2/workflow-versions/{id}`。
4. `POST /v2/projects/{project}/workflows/{workflow}/runs`，带 `X-Idempotency-Key`：
   `{"input":{"draft":"...","user_id":"owner"},"workflow_version_id":"可选UUID"}`。
   不指定版本时仅在创建时取当前版本，之后不再跟随 current 指针。
5. `GET /v2/workflow-runs/{id}` 查看版本、thread、checkpoint、pending_interrupts、状态。
6. `GET /v2/tasks/{task_id}/approvals` 获得审批 ID。
7. `POST /v2/approvals/{id}/responses`，带幂等键：
   `{"answer":false,"decided_by":"owner"}`。answer 支持 JSON，包括 false/0/空字符串/null。
   每个回答都通过 `Command(resume={interrupt_id: answer})`，不使用裸值真值判断。
8. 旧 `POST /v2/approvals/{id}/decisions` 继续支持 approve/edit/reject/clarify，
   转成 `{"action":"approve","edited_payload":{}}`。若 interrupt 指定 allowed_actions，
   两个回复入口都验证它。任务 input/request 的 user_id 用于 required_user_id。
9. 多分支等待采用**本轮全部回答齐全再恢复**策略。前几个回复返回
   `waiting_other_answers`；最后一个回复返回唯一 resume job_id。
10. `POST /v2/workflow-runs/{id}/retry`，带幂等键，在修复基础设施问题后重试。
    只允许因 job dead-letter 而失败的图，不允许复活业务拒绝/取消/成功的运行。

权限边界：trusted_code 是管理员确认，不是 Python 沙箱。代码在现有受信任 Kernel
进程中导入，具有相同权限。不得把注册/激活 API 直接暴露给不可信用户；用户批复
只能提交 approval_id 和答案，不能指定路径、工厂、版本或 thread_id。

## 稳健性与恢复

- 根图实际 `checkpoint_ns=''`，LangGraph 用它区分子图，不是业务版本命名空间。
  旧版本传入 `workflow-version:...` 的根图实际也存于空命名空间（已实测）。
- `workflow_run_id → workflow_version_id → definition_sha256` 固定代码，
  `graph_thread_id → checkpoint` 固定状态；恢复不依赖内存中的 graph 对象。
- 同一节点连续 interrupt 可复用 checkpoint_id 和 interrupt_id。checkpoint_token
  还纳入任务及嵌套子图已保存的 resume 列表，区别等待轮次；approval ID 按 run+token+interrupt
  确定性生成，重复落库/重启不会新建多条同轮审批。
- 读取 runtime 的 task `__resume__` pending writes 是针对锁定 LangGraph 版本验证的
  兼容边界。升级依赖必须重跑全套 PostgreSQL 测试，不能跳过版本校验。
- 同一 run 的 PostgreSQL session advisory lock 覆盖 invoke 和业务提交；新的 Worker
  即使接管超时 job，也不能与仍活着的旧调用并行执行同一图。
- 写业务结果前校验 job worker_instance_id 和有效 lease，阻止旧 Worker 提交状态。
- 崩溃时 checkpoint 与业务表不是一个事务：重试检查已有 checkpoint，使用 None
  继续而不再次提交初始输入；已消耗答案不再次应用。本次结果另有 last_job receipt，
  覆盖业务提交后、finish_job 前的崩溃窗口。
- 回复、审批状态、resume job 入队在一个事务里；冲突重复回复拒绝。
- 死信时同时关闭 task/run；显式 retry 使用旧 job 的持久化命令，继续原 checkpoint。
- 取消队列中的图 job 同步关闭 task/run。运行中的同步节点不能强行中断：当前
  invocation 返回后才检查取消并提交 cancelled；这不是实时硬停止能力。
- 长期等待不占用 worker；当前 control worker 仍逐 job 执行，没有新增并发 worker 池。

## 旧任务兼容

新 ProjectBuild 自动登记项目内 system-project-build v1，使用相同节点名称和 State。
旧 project_build/project_build_resume job 仍可领取；首次接触时补 workflow_run/version
关联，保留 task_id 作为 thread_id。旧 pending approval 根据原 payload 中的 approval_id
与当前 checkpoint 精确配对，不猜测、不换链接。已决定的旧批复支持幂等重发。

## 部署与验证

1. 保存 source 备份和 assistant / assistant_checkpoints schema 的 PostgreSQL dump。
2. 确认无正在执行的 control job，停止 kernel-api、kernel-mcp、kernel-worker。
3. 执行 `/opt/assistant-kernel-venv/bin/python scripts/upgrade_multigraph.py --apply`。
4. 仅重启上述 Kernel 服务。不重建 AI、不触碰 IB/VNC/trading/n8n 配置。
5. 验证 API route、health、迁移号。生产模型端到端测试需另外明确执行。

隔离数据库测试（每次自动创建并删除 `kernel_graph_test_<随机UUID>` 数据库）：

```powershell
docker compose -f H:\AI\docker-compose.yml exec -T -e KERNEL_GRAPH_INTEGRATION=1 AI /opt/assistant-kernel-venv/bin/python -m pytest -q /home/v/AI/assistant-kernel/tests
```

测试覆盖多图、多版本、动态快照、并行审批、连续同 ID 中断、真假/空/null 答案、
独立 Python 进程恢复、checkpoint/业务提交间崩溃、重复 job、冲突答案、租约、死信
重试、取消、旧 ProjectBuild 和原 python_script 路径。

回退：0005 是只增加字段/索引的迁移，可保留。只在尚未产生新 graph_start/graph_resume
工作时恢复旧源码并重启；若已有新图任务，先停止入口，保留 DB+快照并修复向前，
不要把新任务交给不认识它们的旧 Worker。
