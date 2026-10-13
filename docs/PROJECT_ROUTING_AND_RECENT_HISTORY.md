# Project 路由与历史读取

2026-10-13 当前源码规则，尚未部署生产。

- 已绑定频道继续进入绑定 Project 的 PM；专用 Supervisor 频道保持自身接口归属。
- 相关回复保持其原 Project/Run 关联。旧 Butler 身份只能读取历史，不能恢复执行。
- 无绑定或历史 Butler 入口返回提示，不默认新建 Kernel PM 任务。
- `/butler` 不再注册；遗留文字命令只给出退役提示，不更改绑定。
- `/bind_project`、`/unbind_project` 继续使用现有权限和归属检查；解绑后没有默认 Agent。
- `list_projects` 只返回现有可用项目，不提供独立管理入口；创建能力本轮下线。

`list_recent_history` 保持限定 Project/会话、分页及撤销过滤；历史消息、批准和 URI 不迁移或重编号。
当前源码仅保留必要历史标识解析与拒绝检查，不能把它们视为仍有 Butler Agent。
