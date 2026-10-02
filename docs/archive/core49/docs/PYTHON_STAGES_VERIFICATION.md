# Python 阶段化移除验收 — 2026-09-04

## 实际结果

- Kernel venv 卸载旧图依赖后，完整测试：**33 passed in 37.02s**。
- 测试使用临时 PostgreSQL 数据库，结束后数量为 0；没有生产测试任务或收费模型调用。
- 覆盖阶段顺序、三轮审批、修改/澄清/拒绝、返工上限、重复及冲突回答、审批身份、
  并发 Worker、阶段完成回执、死信重试、租约失效、取消、跨进程继续下一阶段。
- 已保存的 Codex 产物可在阶段业务提交前崩溃后复用；Session 轮换 SQL 仍可正常创建 checkpoint。
- 新数据库从历史迁移顺序升级到 0006，通过；有在途旧工作时迁移会拒绝。
- 普通 python_script 注册/激活/执行分派通过；旧图工厂类型注册被拒绝。

## 线上切换

- 0006 已应用，当前业务没有排队/运行任务。
- API、MCP、Worker 均 RUNNING，/health/ready=ready。
- 当前运行代码和依赖清单没有旧图运行器、interrupt/resume map、图 checkpoint token 引用。
- assistant_checkpoints 的四张旧表和 schema 已移除。
- 原 18 条图 checkpoint（3 个 thread）已包含在切换前备份中。
- 7 条 Session 轮换 checkpoint 的 ID、state_json、content_sha256 前后完全一致。
- 3 条无效的历史审批 Outbox 从待投递变为 cancelled；审批和任务历史保留。
- 11 个普通 Python 工作流版本保留。
- 当前 Kernel 环境 importlib.find_spec('langgraph') 为 None，pip check 无破损依赖。
- 未修改 Compose、n8n、IB、VNC、交易代码或 Docling 服务。

## 备份及恢复边界

备份目录：`H:\AI\assistant-data\backups\remove-interrupt-20260904-161943`

- assistant-kernel：移除前源码、测试、迁移、依赖和文档。
- retired-generated：旧 build 副本、workflow 目录及旧缓存/包元数据。
- postgres-before.dump：assistant 与 assistant_checkpoints 两个 schema 的 PostgreSQL custom dump。
- dump SHA256：`9CC7F50A2D690D2B3BE05376C26327A41935CA5C6AF5208B6C450A56F892D51F`。

从 Kernel venv 卸载的发行包：

- langgraph 1.2.11
- langgraph-checkpoint 4.2.0
- langgraph-checkpoint-postgres 3.1.2
- langgraph-prebuilt 1.1.0
- langgraph-sdk 0.4.4

旧源码与数据可恢复，但必须配套恢复相应依赖和数据库，不能在 0006 新结构上只运行旧源码。
历史设计文档放在 docs/archive，历史迁移 0001–0005 未篡改。

## 明确限制

现在恢复的是业务阶段，而不是任意 Python 调用栈/图节点。
外部操作完成但证据未落库的窗口仍需要外部幂等键，不承诺跨系统 exactly-once。
生产只做迁移与接口只读检查；真实模型与飞书实发未在本次验证中执行。
