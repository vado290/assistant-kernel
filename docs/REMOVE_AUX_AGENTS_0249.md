> 历史版本记录：其中 Butler/intake 的入口描述已失效。当前退役规则见 [KERNEL_MANAGEMENT_MERGE.md](KERNEL_MANAGEMENT_MERGE.md)；本次未部署生产。

# 0.24.9 删除 Kernel child Run 与 Operator Agent

2026-10-04 已部署 `0.24.9/core5.v7`。用户确认本轮仅删除上述两项，保留现有 PM/Supervisor 上下文与 Skills 管线，不新增结构化 Skills 选择功能。

## 删除及保留

- 删除 Kernel child 创建、结果合同与执行分支、父任务汇总、失败回收、waiting_children 运行逻辑和专用配置生成。
- 删除 Operator 的 Role/Skill、Project 模板及 Discord Agent 接口支持。飞书事件中的 operator 用户字段、供应商 URL 等同名非 Agent 内容保留。
- 创建 Run 和消息入口拒绝 child/Operator；Supervisor 合同及事务提交拒绝 kernel_child_run，旧请求不能退回普通聊天执行。
- 每个生产 Project 仅有 PM、Supervisor 两个活动主 Agent，平级协作。PM 每轮有效结算仍生成 Supervisor consultation；Supervisor 按已有 Action Plan/Work Mode 向下一轮提供上下文、指令建议，由 Kernel 加载已有 Skills。
- 保留既有 Codex native_subagent 请求/结果路径；它不是本次删除的 Kernel child Run。
- 保留全局 Butler 路由、五个 Work Mode、问卷、审批、Validation/冷审计、发布、日志、健康检查、Incident、取消、恢复和 2＋4 worker 容量。

专用配置归档于 `assistant-data/backups/child-removal-20261004-0248/project-agents`，退出活动 Agent Registry。未删除外部 Discord 频道或消息。当前导航删除退役 Agent 条目并生成新不可变地图 generation，历史 generation 保留。

## 验证

- 完整回归 **300 passed**；包括 PM→Supervisor 每轮触发、屏障恢复、Action Plan 事务接受、现有上下文/Skills 绑定及退役请求拒绝。
- 配置归档/恢复测试证明业务 Run 和 Schema 不变；专用配置备份经过 SHA 验证后才退出活动目录。
- 三个 Project：butler-maintenance、kernel、trading-risk-bot，活动 Agent 均为 **pm、supervisor**。
- 数据库保留 `core5.v7` 六张业务表；运行代码不再产生 waiting_children，历史 Schema 的枚举保留兼容性，无 DDL 变更。
- 原 waiting_user 任务、问题 ID、交互序号 1 及 request/result 未变，业务签名仍为 `762e01dee284bd73af4eec66da59af36`。
- 生产 API 四个无网络 Python 脚本仍真实并行，共同执行窗口重叠约 **3.420 秒**，全部成功，幂等重试返回同一 Run，交互池保持就绪。
- 源码与安装包/Role/Skill 资源 **92/92 哈希一致**，版本化 venv 的 pip check 无冲突；CORE5_VERIFY_OK，readiness ready，Discord ready，无降级项。
- 没有重建容器；Docling 及其他外围服务没有重启。

相较本轮开始的 0.24.8 工作区，生产 Python 从 **27,262 → 26,925 行**，净减 **337 行**，非空行同样减少 337；另删除 Operator 的两个专用提示文件。问卷模块逐字节未变。

以上是可自动验证的结果，未发现被保留功能的回归，不是对未知场景“绝对无副作用”的保证。真人 Discord 发消息/点击卡片仍未用用户账号实测。

## 发布证据和恢复

源码、未提交改动、原数据库和 Supervisor 配置备份：`assistant-data/backups/child-removal-20261004-0248`。wheel、源码哈希、测试日志、部署记录和生产 canary 位于 `assistant-data/runtime/releases/0.24.9`。

部署脚本在停止 Kernel 服务后归档 Agent 配置，再切换版本并检查健康；失败时可通过 retire_aux_agents.py --restore 恢复归档配置及地图，再恢复 0.24.8 指针。业务 Run 不由退役脚本取消或推进。Git 改动尚未提交。
