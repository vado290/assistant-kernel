# 普通 Python 阶段任务（当前实现，迁移 0006）

ProjectBuild 不再使用 LangGraph。没有图构造、动态工厂导入、图 checkpoint、
interrupt ID、resume map、子图状态检查或等待轮次 token。
Kernel 的业务数据库是唯一的阶段状态来源。

## 执行方式

```text
创建 build → requirements job → 正常结束 → 需求审批
批准 → architecture job → 正常结束 → 架构审批
批准 → implementation job → testing job → review job
审查通过／返工上限 → 人工验收
验收批准 → succeeded；拒绝 → cancelled；修改 → 新一轮实现
```

每个 job 只执行一个阶段。当前阶段完成后，在同一数据库事务里保存阶段结果、
更新 task/run 状态、创建下一阶段 job 或审批及 Outbox。等待用户时没有函数悬挂。
用户答案只查业务审批记录，创建下一个任务；不会恢复旧 Python 调用。

## 代码边界

- `domain/project_build.py`：固定阶段与批准/修改/拒绝/返工分支；不是通用图引擎。
- `services/project_build.py`：复用 CodexStageExecutor，一次执行一个阶段。
- `repositories/stages.py`：阶段完成事务、审批、幂等、重试及取消。
- `workflow_versions`：记录内置业务定义版本和哈希，不动态导入旧代码。
  运行中定义哈希变化会拒绝继续，必须先排空在途任务或明确迁移业务版本。
- `workflow_runs.current_stage/current_stage_attempt`：当前业务阶段和单调递增的阶段轮次。
- `node_runs`：每次阶段的输入、产物、状态、job ID 及完成回执。
- `approval_requests.node_run_id`：审批对应哪一次阶段结果。
- `approval_decisions.answer_json/next_job_id`：用户答案与其触发的下一任务。
- `control_jobs` 复用现有队列、lease、重试、死信；新增任务类型仅 `stage_run`。
- Session、模型原始记录、产物、Session 轮换 checkpoint 保持原职责。

## 接口

保留现有入口：

- `POST /v2/projects/{project_id}/builds`
- `GET /v2/tasks/{task_id}`、`GET /v2/workflow-runs/{run_id}`
- `GET /v2/tasks/{task_id}/approvals`
- `POST /v2/approvals/{approval_id}/decisions`
- `POST /v2/approvals/{approval_id}/responses`
- `POST /v2/workflow-runs/{run_id}/retry`
- `POST /v2/jobs/{job_id}/cancel`

创建和回复仍需 `X-Idempotency-Key`，接口仍由原 Kernel Bearer token 保护。
本业务回答必须含合法 action；例如 responses 的
`{"answer":{"action":"approve","edited_payload":{}},"decided_by":"owner"}`。
decisions 继续接受 `{"decision":"approve","decided_by":"owner"}`。
需求/架构支持 approve/edit/reject/clarify；验收支持 approve/edit/reject。
回答中的 edited_payload 保存在阶段业务上下文里，供后续模型读取，不覆盖原始请求。
错误审批人、冲突重复回答、旧阶段回答均拒绝；相同回答幂等返回原 next_job_id。

普通工作流注册只支持 `python_script`，沿用原子进程执行链与源文件哈希校验。
内置 ProjectBuild 只通过 builds API 创建，不支持外部注册/激活其处理器。
没有新增 n8n 编排适配器；n8n 仍负责入口、通知、定时和外部动作。

## 故障与恢复

- 已完成阶段不重跑：node_runs 保存结果和 job 回执。
- 进程重启后从数据库领取下一阶段，不需要任何图 checkpoint。
- 当前阶段失败时重试同一阶段。审批修改则创建新的阶段轮次及新审批 ID。
- 同一 build 有 PostgreSQL session advisory lock，防止多个 Worker 同时执行。
- 写业务完成状态前验证 job owner、lease、取消标记，旧 Worker 不得提交。
- 有限重试耗尽后同步标记 stage/task/run 失败；修复基础设施后调用 retry。
  业务拒绝、成功、取消的 run 不可用 retry 复活。
- 普通阶段取消在本阶段返回后生效；不保证立即终止任意 Python/远端工具副作用。
- Codex 产物记录带 stage_run_id，可复用“产物已保存、阶段事务未提交”窗口中的结果。
  模型/外部动作成功但结果尚未落库时仍可能重试。付款、发信等动作必须自行幂等或走 Outbox；
  不承诺跨系统 exactly-once，也不再提供任意图节点的断点恢复/时间旅行。

## 迁移和历史

0006 要求没有在途旧 ProjectBuild/旧图任务或未决定的旧审批，发现即拒绝切换。
旧 LangGraph 源码、升级脚本和测试已从运行树移除；旧设计与验收文档只作历史归档。
删除旧图专属字段和 assistant_checkpoints 四张表及 schema，保留业务/Session checkpoints。
过期审批 Outbox 标记 cancelled，不删除历史审批、任务、模型记录和产物。
已经执行的 0001–0005 迁移不改写；新数据库顺序应用到 0006 即得到当前结构。

部署：先备份源码及 assistant/assistant_checkpoints 数据，停止 Kernel 三个服务，执行：

```text
env -u PYTHONPATH /opt/assistant-kernel-venv/bin/python scripts/remove_graph_runtime.py --apply --backup-file <已验证备份路径>
```

随后用当前 pyproject 更新 Kernel 包元数据、卸载 Kernel venv 内不再使用的 LangGraph
发行包，再重启 Kernel API/MCP/Worker。无需重建 AI 容器或修改其他服务。
卸载前核查反向依赖和已注册活动脚本，不删除其他服务的 Python 库。

隔离数据库测试：

```powershell
docker compose -f H:\AI\docker-compose.yml exec -T -e KERNEL_STAGE_INTEGRATION=1 AI env -u PYTHONPATH /opt/assistant-kernel-venv/bin/python -m pytest -q /home/v/AI/assistant-kernel/tests
```

测试数据库使用 `kernel_stage_test_<随机UUID>`，测试结束后自动清理，不调用收费模型。
恢复旧实现必须同时恢复配套源码、依赖、数据库备份；已删旧字段后不能只回滚代码。
