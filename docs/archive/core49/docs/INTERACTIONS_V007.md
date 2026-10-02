# 多轮交互与优化实施分离 — 迁移 0007

实施日期：2026-09-06。基于普通 Python 阶段任务，复用 Kernel 数据库、队列、审批、Outbox、Codex CLI 和单 AI 容器。

## 已部署的能力

1. 多题问题原文、逐题答案、修订关联及来源登记；支持部分、反序和幂等回答。
2. 非阻塞 `ask_user` / `submit_stage_for_confirmation` 工具，通过每次执行的 stdio MCP 接入 Codex。
3. 区分补充回答与正式批准；有修改意见的批准先返回原阶段，不直接推进。
4. 按阶段显式装配角色、skill、完整文档和问答历史，记录不可变 prompt 快照。
5. 飞书问题全文展示、成果全文分段、发送回执、卡片及引用回复映射；发送未启用，见下文限制。
6. 人工反馈/手动请求 → 优化分析 → 提案确认 → 独立候选实施 → 测试 → 审查 → 人工验收。

## 执行与等待

```text
Worker 领取阶段 job
 → 固定角色与 skill 快照，启动 CLI 和本轮专用的阶段 MCP 工具
 → Agent 登记提问/确认意图，工具迅速返回 draft 回执
 → Agent 正常结束本轮
 → Kernel 验证 CLI 完成事件与产物，提交审批/问题/Outbox
 → CLI 进程已结束，业务任务 waiting_user
 → 用户逐题回答或正式批准
 → Kernel 保存问题原文＋答案，创建对应后续 job
```

工具本身不会等待用户，不替用户批准，也不会直接创建下一阶段。

工具调用但 CLI 中途失败时，只保留 stage_tool_requests 中的 draft，不发送孤立问题。重试使用新的 turn_key；已保存的成功阶段产物仍可在提交事务前崩溃后复用。正式问题在成功阶段事务内发布。

当前同阶段的后续轮次从完整业务交接新建 Session，不依赖悬挂的调用；跨阶段也独立 Session。未改原有普通对话的 Session 轮换策略和 token 占用口径。

## 数据登记

新增五张表，已有审批仍是业务决定的权威：

| 对象 | 用途 |
|---|---|
| stage_tool_requests | 本轮工具提交的草稿意图、job/Worker/轮次绑定、发布状态 |
| interaction_questions | 问题 ID、原文、必答标记、序号、替代的问题及成果版本 |
| interaction_answers | 对应问题的答案、回答人、来源和时间 |
| channel_deliveries | Outbox 分段、平台消息 ID、问题/审批关联和内容哈希 |
| channel_events | 原始入站事件、去重、处理状态及重试信息 |

node_runs 增加 prompt_binding。问题和答案原始行禁止 UPDATE/DELETE；更正问题通过新一轮新问题与 supersedes_question_id 关联。审批通过或进入下轮后，不能用旧问题改变新成果。

GET 交互返回每题原文和答案。原文无需用户重复填写；恢复时 Kernel 从记录组装 question_history。answer_json 为 false、0 或 null 仍可表示有效答案，是否已答通过回答记录/时间判断。

## API 使用

仍使用现有 Kernel Bearer token，无新增账号体系。

### 创建建设流程

`POST /v2/projects/{project_id}/builds`，带 `X-Idempotency-Key`：

```json
{
  "request": {
    "goal": "设计并实现日报能力",
    "user_id": "实际回答人的标识",
    "delivery": {"channel": "feishu", "app_id": "应用ID", "chat_id": "接收聊天ID"}
  },
  "execution_profile": {}
}
```

delivery 可省略，此时仍可通过 API 查看问题和回答；不会推测外部收件人。飞书交互的 user_id 应与回调中的实际 open_id 对应，不使用示例字符串 owner 代替真实标识。

### 查看与回答问题

- `GET /v2/interactions/{interaction_id}`：interaction_id 与 approval_id 相同。
- `POST /v2/interactions/{interaction_id}/answers`：带 `X-Idempotency-Key`。

```json
{
  "decided_by": "实际回答人的标识",
  "answer": {
    "action": "answer",
    "answers": [{"question_id": "返回的问题UUID", "value": "上午九点"}]
  }
}
```

多个问题可分别提交。必答项未齐时返回 waiting_user/unanswered_question_ids；全部必答项齐全后只创建一个当前阶段的后续 job。单个答案已提交后若需改变，先要求修订问题，不能覆盖历史记录。

正式确认：`answer={"action":"approve"}`。要求修改：`answer={"action":"edit","edited_payload":{"feedback":"修改意见"}}`。approve 同时携带非空 edited_payload 或 feedback 时，按修改处理。输入澄清不能用 approve 跳到下一阶段。

旧 `/v2/approvals/{id}/decisions` 和 `/responses` 入口保留；旧审批继续按原协议，新审批按 protocol_version=7 分派。新协议不会把带修改内容的 approve 当成无条件批准。

## 角色、skill 和提示词快照

每项目的角色路径为：

```text
P/role.md
P/stages/requirements/role.md
P/stages/architecture/role.md
P/stages/implementation/role.md
P/stages/testing/role.md
P/stages/review/role.md
P/optimizer/role.md
P/optimizer/implementation/role.md
```

六个 skill 位于 `P/.agents/skills/`：assistant-requirements、assistant-architecture、assistant-implementation、assistant-testing、assistant-review、assistant-optimization-analysis。优化实施复用 implementation skill，加上自己独立 role 的候选改进要求。

已给四个已有项目准备上述配置；不启动新的业务任务。旧编码角色目录和历史快照保留，不删除、不覆盖手工文件。数据库 primary role 仍是生效角色，role.md 与其不一致时可通过现有 Agent 目录接口识别；直接编辑 role.md 不等于发布。

阶段第一次执行时，取当前已发布角色及该项目 skill，保存到 `P/runtime/prompt-snapshots/{sha256}.md`，将路径、哈希、角色版本和 skill 名登记到 node_runs.prompt_binding。重试使用原快照，发现快照被修改会拒绝运行。后续新阶段/新轮次才可绑定新的 skill 版本。

CLI prompt 已改为 stdin 输入，解决原先单个命令行参数约 120 KB 的限制。业务上下文仍有显式大小限制，超限报错，不静默截断需求。

原始调用日志在 `P/runs/agent-traces/invocations/{turn_key}/`，包含请求关联、stdout.jsonl、stderr.log。输出边执行边保存，超时或失败仍保留部分轨迹。成功记录继续关联原生 Session 归档；没有 turn.completed、没有最终文本或出现失败事件不会发布阶段交互。

## 优化分析到候选实施

`POST /v2/projects/{project_id}/optimizations`，带 `X-Idempotency-Key`：

```json
{
  "goal": "减少日报重复内容",
  "user_id": "实际回答人的标识",
  "feedback_ids": ["已收集反馈的UUID"],
  "target_paths": ["scripts/daily_brief.py", ".agents/skills/daily-brief/SKILL.md"],
  "execution_profile": {}
}
```

target_paths 是本次明确选定的现有文本文件，1–50 个；系统计算文件基线并带入用户反馈原文。未创建日报示例文件，实际调用需换成已有文件。

优化提案与已有 change_requests 关联，阶段结果持续登记分析产物和实施产物。分析使用独立 Session；确认后实施在 `P/runs/{task_id}/candidate/` 修改副本，测试和审查也使用该候选目录。正式源文件不自动替换；发现正式基线已经改变时拒绝继续旧提案。

候选副本仅包含显式选定的文件，运行所需其他本地依赖也应包含在目标文件选择中；本版不自动推测并复制整个项目。

人工验收后返回 `candidate_accepted_not_published`，附候选目录、文件哈希和验证记录。自动发布/全量定时优化在本轮计划中后置，未实现。PM/用户可通过 run、提案和反馈关联核查结果；未新增 PM 自动识别自然语言并发起优化的工具，当前入口是明确的 API/手动触发。

## 飞书接入与当前实际状态

- `POST /channels/feishu/callback`：验证平台 token/app，快速持久化事件并返回；耗时处理在 Worker。
- `POST /v2/channels/feishu/events`：给已有可信网关/n8n 使用，沿用内部 Bearer token，传入完整事件信封。
- `GET /v2/channels/feishu/status`：只返回配置状态，不泄露凭据。

配置位置是现有 service-credentials.json 的 services.feishu，需 app_id、app_secret、verification_token 和 enabled。未自动填写或启用。

旧 codex_team.py 中有 App ID/Secret，但 verification token、接收 user/chat 等字段为空。本轮未迁移旧凭据，没有已核实的真实收件会话，因此生产显示 configured=false / sender_enabled=false / callback_configured=false；未向任何真实飞书联系人发送消息。

Sender 显示每题完整原文和成果版本，支持成果正文分段发送。卡片批准绑定实际已送达的问题消息；普通答案通过引用回复关联，也可在同用户同聊天仅一个待答问题时匹配。多个候选时发送澄清提示，不按最新问题猜测。

默认 HTTP 回调实现处理未加密、带校验 token 的事件。加密事件应通过现有完成解密/验证的网关进入内部 API；本轮不新增加密回调解码依赖或 WebSocket Gateway。应用订阅、回调可达性、平台权限和真实送达仍待现场配置验证。

发送分段、平台回执及原始渲染计划持久化，重试优先复用回执，并提供稳定的发送 uuid。超过重试上限进入 dead_letter。平台成功但本地回执未存的窗口依赖平台幂等/结果核对，不承诺跨系统天然 exactly-once。

## 验证、备份与未完成范围

开发副本完整测试：55 passed in 95.06s；最后两处小调整的相关测试：2 passed in 7.89s。包含真实 PostgreSQL、独立 MCP stdio 子进程、模拟模型和模拟飞书服务；不包含真实 LLM 工具选择或飞书实发效果验证。

部署后的实际代码再次运行完整隔离测试：**55 passed in 95.33s**。最终核查：迁移 0007、临时测试数据库 0、活动 control jobs 0、健康接口 ready；飞书 configured/sender_enabled/callback_configured 均为 false。代码哈希与验收记录已写入备份目录的 deployment-verification.json。

部署后 /health/ready 和新增 API 检查通过；迁移 0007 生效；API、MCP、Worker 已恢复。部署前无在途任务。未新增容器、运行依赖、角色权限平台；未改 n8n、Compose、IB/VNC/交易模块。

备份：`H:\AI\assistant-data\backups\interactions-v007-20260906-075315`，包含部署前源码和 assistant schema dump。dump SHA-256：`847797BDB38E105EFA5DF99001707042910452ED798F1FA63E6E25D9F07F71FB`。

回退前必须处理新协议的在途任务；旧代码不理解优化阶段和新交互。不要在有未完成新任务时只换回旧代码。恢复数据库会回退备份后的业务记录，须先另行归档这些记录。

目录强隔离/跨目录访问审批、Session 延迟轮换及 usage 口径调整仍未完成，不能把角色中的范围规则当成操作系统沙箱。
