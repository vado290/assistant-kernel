# 简化架构首批落地 — 2026-09-05

> 后续更新：多轮交互、阶段工具、角色/skill 绑定与优化候选流程已在迁移 0007 中实现，见 [新实施记录](INTERACTIONS_V007.md)。本文保留为首批实施历史；其中的 CLI 单参数限制已由 stdin 输入替代，飞书实发仍未启用。

状态：代码已实施并重启 Kernel API/MCP/Worker。按用户要求，仅做边界明确且易验证的部分。

## 本次已实现

### Agent 目录与角色视图

- `GET /v2/agents?project_id=P`：查询已登记 Agent，可按项目过滤。
- `GET /v2/agents/{agent_id}/runtime`：查询 home_path、cwd、codex_home、role_path、skills_path、当前 primary role_version_id。
- `POST /v2/agents/{agent_id}/runtime`：初始化已登记 Agent 的目录和角色文件；不会创建不存在的 Agent。
- 查询接口只读，不创建目录。路径由现有 agents.project_id 和 Vault 根规则确定，无新表或另一份路径权威数据。
- PM/大管家的角色文件为 Vault 根下 role.md；阶段角色在项目内独立 roles 目录下，避免覆盖 PM 的 role.md。
- 初始化缺失的 role.md，并保存 `roles/versions/{role_version_id}.md`。已有手工编辑文件保留；`role_file_matches_active=false` 表示文件与数据库发布版本不同。
- 实际执行继续使用数据库已发布的 primary 角色；编辑 role.md 不会自动发布。已有 PUT `/v2/agents/{agent_id}/role` 是发布入口，之后再初始化可导出新版本快照。
- 初始化同时准备 knowledge、capabilities、artifacts、runs、sources、reports、.agents/skills。未安装新的业务技能。
- 阶段执行优先使用已发布的 primary 角色；不再每轮强行取写死的 STAGE_ROLES。secondary 不会覆盖 primary。

当前已为 4 个已有 Agent 初始化角色文件与版本快照，不创建新的业务项目。

### 完整阶段交接与人工意见

- 下一阶段直接读取前序产物 Markdown 全文，验证项目 URI、最终路径和内容 SHA-256。
- 不再只依赖前 2000 字的摘要；数据库状态仍保存文档引用，全文只在执行时装配到 prompt。
- 每次审批按顺序追加 `approval_history`，保留连续 clarify/edit/approve 的意见。
- implementation 返工显式获得 `acceptance_decision`，修复人工验收修改意见未进入下一轮 prompt 的问题。
- 缺失、跨项目、内容被修改的产物会报错，不能带着未经验证的替代内容继续。
- 当前限制：单文档 256 KiB、组合文档 512 KiB；最终 prompt UTF-8 超过 120000 字节明确报错。这是现有 CLI 使用单个命令行参数传 prompt 的边界，不是模型最大窗口。未实现超长材料自动拆分，不静默截断正式需求。

`domain/project_build.py` 已改变，因此新建 build 使用新的内置定义哈希。部署前无在途 control job；旧成功/取消记录不重新执行。未来改变该定义仍需先处理在途任务。

### 人工反馈收集入口

复用 change_requests，以 target_type=user_feedback 区分原始反馈；完整可读副本保存在 `P/artifacts/feedback/{feedback_id}.md`。

```text
POST /v2/projects/{project_id}/feedback
GET  /v2/projects/{project_id}/feedback
GET  /v2/projects/{project_id}/feedback/{feedback_id}
```

POST 使用原 Kernel Bearer token，并要求 `X-Idempotency-Key`。请求示例：

```json
{
  "text": "日报太长，重复内容也多",
  "expected_behavior": "正文缩短，只保留变化",
  "user_id": "owner",
  "channel": "local",
  "impact": "medium"
}
```

可选字段：external_message_id、source_message_id、task_id、workflow_run_id。后三个使用内部 UUID，提交时验证属于该项目；如果同时传 task 和 run，还检查两者一致。暂时没有关联的反馈可以直接提交，不强制填写运行编号。

返回 feedback_id、storage_uri、content_sha256、status=received、optimizer_started=false。相同项目、幂等键、请求重复提交复用原记录；冲突请求被拒绝。按其他项目查询同一 feedback_id 返回 404。

Markdown 文件先写临时文件再替换，随后提交数据库记录。文件系统与 PostgreSQL 不是跨介质事务；失败时可能留下孤立文件，但不会把失败事务伪装成已收集。按同一幂等键重试可恢复缺失导出；已有文件内容变化会报告冲突，不覆盖人工改动。

这个入口只是可靠收集与查询，不会自动触发 Optimizer，也未接入飞书自然语言反馈或 PM 的自动识别流程。不会发送新的外部消息。

## 明确未实施

- Shell/Python 的项目强隔离、跨目录审批与批准后的访问执行。
- 全局/父目录 skill 发现过滤；仅准备项目内技能目录。
- 飞书 Outbox 实发、卡片回调和引用回复闭环。
- 自动建项目/移交 PM、结构化 needs_input 访谈协议。
- Session 延迟轮换及 usage 口径调整；维持原运行逻辑。
- Optimizer 自动分析、改进提案生成/实施及自动发布。

路径 API 做的是 Vault 路径检查。此次额外拒绝项目根重定向到兄弟 Vault、以及文件路径经软链接逃出本项目；不能把这些检查称为所有进程都已被隔离。接口明确返回 scope_enforcement 的限制说明。

## 验证与部署

- 实际 AI 容器 Kernel venv，隔离 PostgreSQL 完整测试：39 passed in 57.63s。
- 随后增加 CLI 参数长度边界，相关测试文件：9 passed in 0.48s，包含新边界用例。
- 新用例覆盖：只读目录查询、自定义 primary 角色、保留手工文件、反馈并发幂等、文件恢复/冲突、跨项目反馈关联拒绝、完整文档/连续意见/验收返工、文档哈希和链接路径检查。
- 所有模型使用 fake Harness；未调用收费模型，未向飞书发信。
- 生产 `/health/ready`、`/v2/agents`、反馈列表接口均 HTTP 200；活动 control jobs=0，遗留临时测试数据库=0。
- 核心服务已恢复运行；无数据库结构迁移、无新运行依赖、无新容器、无 Compose/n8n/IB/VNC/交易模块修改。

源码备份：`H:\AI\assistant-data\backups\simple-foundation-20260905-111548\assistant-kernel`。
若回退，配套恢复本批修改的源文件并重启 Kernel 三服务；无需恢复数据库 schema。此次生成的角色快照是附加文件，保留即可。已有人工反馈不应随源码回退删除。
