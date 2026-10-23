# Kernel 公开发布可用性评估（2026-10-23，基于 0.45.12）

本文回答五个问题：①外部用户下载后要能用还缺什么、Discord/Codex 初始化能否自动化；②扩展 Claude 与国产廉价模型；
③提示词全为中文对英语用户的影响与最小改造；④PostgreSQL 等组件如何交付；⑤对比 Hermes Agent，看板设计与需要加强的功能。
只做分析，未改任何代码。标注【事实】的是查代码或官方文档得到的，【建议】是我的判断。

---

## 1. 外部用户“下载即可用”还差什么

### 1.1 现状：今天只有你这台机器能跑起来【事实】

| 阻断点 | 证据 | 影响 |
|---|---|---|
| 没有自己的镜像/编排 | 运行环境来自 `H:\AI\Dockerfile.AI`（不在 kernel 仓库），基础镜像是私有的 `binance_perpetual:latest`，还 COPY 了 IB Gateway 包 | 外部用户根本构建不出容器 |
| 写死路径 | `/home/v/AI`、`/home/v/miniconda3/bin/python`（settings.py）、`/home/v/AI/bin/codex`（codex_cli.py:52）等共 7 处；`deploy.ps1` 写死 `docker exec AI` | 换目录/换用户就坏 |
| 数据库配置在仓库外 | `settings.py` 从 `rag-stack/.env` 读 POSTGRES_DB/USER/PASSWORD；`rag-postgres` 定义在 `H:\AI\docker-compose.yml` | 用户不知道要建什么库 |
| 默认 `kernel` 项目没有种子 | 全新安装时 `assistant-data/vaults/kernel` 不存在；它只在你单独发布的 project-kernel 仓库里，而且那里有你的个人背景 | 路由、共享背景、建新项目都依赖 kernel 项目 |
| 部署脚本仅 Windows | `deploy.ps1`、`Backup-AssistantState.ps1`、`Initialize-*.ps1` 都是 PowerShell + 宿主机 docker | Linux/macOS 用户无法用 |
| 示例配置含真实 ID | `deploy/core-discord.example.yaml` 里是你真实的 guild_id `1481086241414975562` | 需要脱敏 |
| 没有 LICENSE | 仓库根目录无许可证 | 法律上别人无权使用/修改 |
| 一次性迁移脚本混在 scripts/ | `r0453_*`、`round5_*`、`r0456_*` 等 | 新用户会困惑，误跑有风险 |
| Codex CLI 版本未固定 | harness 依赖 `codex debug models`、`app-server config/read`、`skills.config`、`features.*` 等内部行为 | 用户装了新版 Codex 可能直接坏 |
| 默认会推 GitHub | 只要 `gh` 已登录就自动建私有仓库并推送 | 新用户应默认关闭，按需打开 |

已经做得好的（可直接复用）：迁移从空库可以跑通（0003 用 `CREATE TABLE IF NOT EXISTS`，测试每次都在临时库从零迁移）；
新建项目时 Kernel 已经能用 bot token **自动建 Discord 分类和 #pm/#supervisor 频道**并写回 `core-discord.yaml`
（`bootstrap.py: DiscordChannels.ensure / add_routing`），网关会热加载；README 的敏感数据一节已经很完整。

### 1.2 Discord 的哪些步骤能自动化【事实 + 建议】

| 步骤 | 能否自动 | 说明 |
|---|---|---|
| 创建 Discord 服务器 | **用户手动**（客户端点一下“创建我的服务器”） | 机器人建服务器的接口限制多、不可靠，不建议依赖 |
| 创建 Application + Bot、拿 token | **必须用户手动**（Developer Portal） | 官方 API 只有 GET/PATCH `/applications/@me`，**没有创建 Application 的接口**；用用户 token 模拟网页操作属于 self-bot，违反 Discord 条款，门户还有验证码，不能也不应该代劳 |
| 打开 Message Content 特权 intent | **可以自动**（要验证） | 官方文档：PATCH `/applications/@me` 的 `flags` 只允许改 “limited” 意图位，包括 `GATEWAY_MESSAGE_CONTENT_LIMITED`（机器人在 100 个服务器以内适用）。网关需要这个 intent（discord_app.py:107）。API 失败时回退为提示用户去门户打开开关 |
| 获得 client_id / 应用 owner | **自动** | 用 bot token 调 GET `/applications/@me` |
| 邀请机器人进服务器 | **半自动**：程序生成邀请链接，用户点一次授权 | OAuth 授权必须本人点；链接带 `scope=bot+applications.commands` 和计算好的权限位（查看频道、发消息、管理频道、读历史、嵌入链接、附件） |
| guild_id | **自动** | 邀请后调 GET `/users/@me/guilds`，只有一个就直接选 |
| allowed_user_ids | **自动** | 默认取 guild owner（GET `/guilds/{id}`）或应用 owner |
| kernel 项目的频道 | **自动** | 复用 `DiscordChannels.ensure` |

结论：用户最少只需做 **3 个手工动作**——建服务器、在门户建应用并复制 token、点邀请链接。其余由 `init` 完成。

### 1.3 模型凭据【事实】

- **Codex（ChatGPT 订阅）**：`codex login --device-auth` 设备码登录，无需服务器上有浏览器（用户需先在 ChatGPT 安全设置里允许设备码登录）；
  `init` 把 `CODEX_HOME` 指向 `assistant-data/system/codex-home-template` 执行即可，正好就是 `auth_sync` 用的主副本。
- **Codex（API key）**：`codex login --with-api-key` 或环境变量；OpenAI 官方推荐自动化场景用 API key。
- **Claude**（见第 2 节）：用 `ANTHROPIC_API_KEY`。注意 Anthropic 条款：第三方产品不得提供 claude.ai 登录、不得代用户走 Pro/Max 订阅额度，
  所以发布方文档里应把 API key 作为 Claude 的标准方式；用户自己的订阅凭据怎么用由用户自己负责。
- **GitHub（可选）**：`gh auth login`（设备码），默认不开推送。
- **火山/通义 key（可选）**：只有用到 `volcengine_*` 工具才需要；默认 PM 模板里列了这些工具，未配置时应隐藏或返回“未配置”。

### 1.4 发布方要做的整理（按优先级）【建议】

**P0（不做就无法使用）**
1. 加 LICENSE（如 Apache-2.0 / MIT），脱敏示例配置。
2. 仓库内自带 `Dockerfile` + `docker-compose.yml`：公开基础镜像（`python:3.10-slim` + Node + git + supervisor），
   **固定 Codex CLI 版本**（npm `@openai/codex@x.y.z`），Postgres 服务（见第 4 节）；docling 放进可选 profile。
3. 去掉写死路径：全部来自环境变量，默认值相对 `ASSISTANT_DATA_ROOT`；数据库配置全部走 `ASSISTANT_POSTGRES_*`（保留 `RAG_ENV_FILE` 作为你自己的兼容入口）。
4. kernel 项目种子：在包里加 `core/kernel_template/`（脱敏的 charter、PM/Supervisor role、`capabilities: [routing, project_management, shared_context, kernel_maintenance]`），`init` 时生成。
5. `assistant-kernel init`（交互式、可重复执行，每步已完成就跳过）：
   ```text
   检查 docker → 生成 .env（随机 PG 密码、内部 token）→ 启动 postgres → migrate
   → 选择模型引擎并登录（codex device-auth / API key / Claude key）
   → 粘贴 Discord bot token → 自动开 intent、取 client_id → 打印邀请链接，等待机器人入服
   → 自动取 guild_id、owner → 生成 kernel 项目、建频道、写 core-discord.yaml
   → 启动服务 → 在 #pm 发欢迎消息 → 运行 doctor
   ```
6. `assistant-kernel doctor`：逐项检查 DB、迁移、模型登录、Discord 连接/intent/权限、频道映射、Codex 版本，给出修复提示。
7. `QUICKSTART.md`（中/英）：“3 个手工步骤 + 一条命令”。

**P1**
8. 新安装默认 `git_push: false`，`init` 里问是否开启。
9. 跨平台运维：`deploy.sh`/`make`，或者直接 `docker compose up -d --build` + 容器启动时自动 migrate；备份改成容器内 `pg_dump` 的脚本。
10. 一次性迁移脚本移到 `scripts/legacy/`；GitHub Actions 跑测试（PG service container）；发版打 tag 并推 GHCR 镜像。

---

## 2. 扩展 Claude 与其他模型

### 2.1 改造难度：中等，接口已有，耦合点集中【事实】

- 已有抽象：`harness/base.py` 的 `HarnessAdapter` 协议（`run_turn(AgentTurnRequest) -> AgentTurnResult`）；
  全系统只有一个调用点 `core/executors/common.py: call_model`；装配点只有 `dispatch_main.py:22`。
- 输出契约（JSON outcome/questions/document）与模型无关；技能里没有 Codex 专属工具名（`apply_patch`、`spawn_agent` 等都没有出现），只有 9 处提到 `.agents/skills` 路径。

Codex 专属的耦合点与 Claude Code 对应方案：

| 耦合点 | 位置 | Claude Code（`claude -p --output-format stream-json`）对应 |
|---|---|---|
| 进程调用与 JSONL 事件解析 | codex_cli.py | stream-json 的 `system/init`（session_id）、`result`（文本、usage、cost） |
| 会话续接 `exec resume` | physical_session_id | `--resume <session_id>` |
| Kernel 工具 MCP 注入与“无继承 MCP”校验 | mcp_config.py（用 codex app-server config/read） | `--mcp-config <json> --strict-mcp-config`，`--allowedTools mcp__kernel_scoped__*` |
| 技能目录 | `agents/<agent>/.agents/skills/` | Claude Code 读 `.claude/skills/`：harness 在 agent 目录建链接或复制；用独立 `CLAUDE_CONFIG_DIR` + `--setting-sources` 隔离用户级技能/设置 |
| 沙箱（read-only / workspace-write / 权限 profile） | codex_cli.py | `--permission-mode`、`--allowedTools/--disallowedTools`、`--add-dir`、Bash 沙箱设置；只读阶段禁用 Edit/Write。语义不是 1:1，但 README 已声明项目间不是硬边界，容器才是边界 |
| 模型目录/上下文窗口 | codex_models.py（`codex debug models`） | 静态表或直接 `model_default` |
| 登录同步 | auth_sync.py（ChatGPT refresh token 只能用一次） | 不需要，按引擎跳过 |
| 子 agent | `features.multi_agent` | Claude Code 子 agent（`.claude/agents/`） |

**工作量估计**：新增 `harness/claude_code.py` 约 300–400 行 + 引擎注册表（按 agent `config.yaml` 的 `harness.engine` 选择）+
`call_model` 里按引擎跳过 auth_sync/模型目录 + 技能目录映射 + 测试。一个 Round 可以完成（含真实回合验证）。
主要风险不在代码，而在**行为差异**：同一套技能在不同模型上的遵从度不同，需要用 kernel-v12-validation 跑一遍完整任务流验收。

### 2.2 国产廉价模型的三条路【事实 + 建议】

| 路线 | 做法 | 评价 |
|---|---|---|
| **A. Claude Code 引擎 + 兼容端点（推荐）** | 设置 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 指向 DeepSeek（`/anthropic`）、智谱 GLM（`/api/anthropic`）、Kimi、通义 等的 Anthropic 兼容接口 | 有了 2.1 的引擎后几乎零成本：每个模型配置只是一组环境变量。工具调用、MCP、会话续接都能用 |
| B. Codex + 自定义 `model_providers` | Codex 已在 2026-02 移除 `wire_api="chat"`，只支持 Responses API；大多数国产接口只有 chat/completions，需要 LiteLLM 之类的转换代理；且 `codex_models.resolve` 对非 openai provider 只允许 `context_policy: model_default` | 可行但多一层代理，不推荐作为主路 |
| C. 无工具的简单调用走直连 chat | 已有 `model_providers/chat.py`（OpenAI 兼容）和 `provider-secrets.json` 的 `chat_models` | 适合摘要、分类、日报、产品里的批量文本处理，不需要 agent 能力 |

**配置设计**（新增 `assistant-data/config/models.yaml`，密钥仍放在 `provider-secrets.json`）：

```yaml
profiles:
  strong:     {engine: codex,       model: gpt-5.6-sol}
  claude:     {engine: claude_code, model: claude-sonnet-5-5}
  cheap:      {engine: claude_code, model: deepseek-v4-flash,
               base_url: https://api.deepseek.com/anthropic, key_ref: deepseek}
  text-cheap: {engine: chat, provider: dashscope, model: qwen-plus}
defaults:            # 不写就用 strong
  triage: cheap
  digest: text-cheap
  supervisor.consult: cheap
```

agent 的 `config.yaml` 里写 `harness: {profile: cheap}` 即可覆盖。**哪些环节适合便宜模型**：triage、日报/摘要、Supervisor 咨询、产品项目里的简单批处理；
**不建议**：需求澄清、设计、build、review、release 审核（错误代价高）。产品代码要用便宜模型，可加一个 Kernel 工具或小库按 profile 名调用，避免每个项目自己管密钥。

---

## 3. 中文提示词对英语用户的影响

### 3.1 结论：能用，但有三类问题，其中一类会悄悄丢数据【事实】

| 层 | 现状 | 对英语用户的影响 |
|---|---|---|
| 技能与契约提示词（27 个技能 + 12 个契约，约 257KB 中文） | 模型读中文指令、处理英文对话没问题 | 理解层面**基本无影响** |
| 输出语言 | `pm-output.md` 写死“summary……**中文**” | 用户写英语，卡片和总结仍是中文 → **体验问题** |
| **Kernel 按中文标题解析文档** | `背景信息存档`、`用户画像观察`、`项目用途/范围/路由信息`、`迭代记录`、`本项目对 PM 的特别要求`，以及“无”这类哨兵值（bootstrap.py、common.py、vault_sync.py、iterlog.py、rules.py） | 模型如果跟着用户写英文标题，Kernel 就找不到这一节：背景存档、用户画像、路由目录、迭代记录**静默丢失** → **功能问题，最需要修** |
| Kernel 自己生成的界面文字 | Python 里约 600 行中文字面量（卡片、按钮、报错、日报、`/cancel 确认`），集中在 supervisor.py、digest.py、machine.py、gateway/render.py、gateway/service.py | 英语用户看到的系统界面是中文 → **体验问题** |

### 3.2 最小侵入改造（可行，按顺序做）【建议】

1. **语言设置**：安装级默认值（`init` 时选）+ `project.yaml: language: en|zh-CN`，可按项目覆盖。
2. **解析与语言无关**（必须做，约几十行）：`skills.section_text` 支持别名列表，例如
   `SECTIONS = {"archive": ["背景信息存档", "Background archive"], ...}`；哨兵值同时认 `无 / None / N/A`。
   契约里要求“标题必须原样使用”，标题按项目语言注入。
3. **输出语言参数化**（必须做，1 行契约 + 注入）：把“中文”改成 `{reply_language}`，由 Kernel 注入
   “面向用户的文字使用 {language}；JSON 键和指定标题保持原样”。
4. **界面文字抽取**：`i18n/zh-CN.yaml`、`i18n/en.yaml` + `t("key")`，先做 gateway（卡片、按钮、命令），再做 digest/supervisor 的提示片段。机械性工作。
5. **技能本身**：功能上**没有必要翻译**。长期看，公开项目的受众是国际用户，建议把技能的“源语言”改成英文、输出按用户语言自适应
   （避免维护中英两套模板，每次版本升级都要改两份）。这是一次较大的重写，放在 1–4 之后单独排期。

做完 1–3，英语用户就能正常使用；做完 4，体验基本完整。

---

## 4. PostgreSQL 等组件

**现状【事实】**：Kernel 与 RAG 共用 `rag-postgres`（镜像 `pgvector/pgvector:0.8.6-pg18-trixie`，库 `rag`，用户 `rag_service`），
定义在仓库外的 `H:\AI\docker-compose.yml`，密码在 `rag-stack/.env`。Kernel 用到的 schema：`assistant`（迁移台账）、`core`、`archive_core5`；
**不需要 pgvector**，只用到 PG13+ 自带的 `gen_random_uuid()`。

**用户能否直接用**：镜像是公开的，但编排和凭据都不在仓库里，所以现在不能“直接用”。【建议】

- kernel 仓库的 compose 自带 `kernel-postgres`（`postgres:17` 即可），命名卷、healthcheck、**默认不映射 5432 到宿主机**；`init` 生成随机密码写入 `.env`。
- 也支持用户已有的 Postgres（`DATABASE_URL` 或 `ASSISTANT_POSTGRES_*`），只需要该库的 CREATE SCHEMA 权限。
- 容器启动时自动执行 `assistant-kernel-migrate`（幂等）。0003 的旧数据归档在空库上什么都不做，不需要合并迁移。
- 备份：容器内 `pg_dump` + 打包 vaults（不含 `codex-home` 登录信息）的跨平台脚本。
- 你自己的安装继续通过环境变量指向 `rag-postgres`，不受影响。
- 其他组件：docling-serve 放进可选 profile（体积大，只有文档解析才需要）；Codex CLI、gh、Node 打进镜像并固定版本。

---

## 5. 与 Hermes Agent 对比，以及看板设计

> 你说的“hermas”我理解为 Nous Research 的 **Hermes Agent**。以下关于 Hermes 的内容来自其官方文档和公开文章（版本号各来源不一致，以官方文档为准）。

### 5.1 Hermes 有什么【事实】

- **本地 Web 看板**（`hermes dashboard`，默认 127.0.0.1:9119）：Status、Chat（浏览器里内嵌终端界面）、Config（表单编辑 150+ 配置项）、API Keys、
  Sessions（全文搜索）、Logs、Analytics（token、缓存命中率、成本，按模型分）、Cron、Profiles、Skills（技能市场 + 安全扫描）、MCP、
  Webhooks、Pairing（审批消息用户）、Channels（各平台 token 与连接测试）、System（doctor、备份/恢复、升级、网关启停）。
- **Kanban**：SQLite 任务板，多个 profile（命名 agent）协作，每个 worker 是独立进程，`kanban_*` 工具集，支持人工阻塞/解除、崩溃恢复，看板里有 Kanban 页。
- **安全**：只监听本机时免登录；监听非本机地址时强制认证（用户名密码 / Nous Portal OAuth / 自建 OIDC），未配置认证就拒绝启动。
- **网关**：一个进程接 Discord、Telegram、Slack、WhatsApp、Signal 等 20+ 平台。
- **模型**：Nous Portal、OpenRouter、OpenAI、Anthropic、任意 OpenAI 兼容端点和本地模型，会话中可 `/model` 切换。
- **记忆与技能**：MEMORY.md / USER.md（有字数上限，逼迫整理）+ SQLite 全文检索历史会话；完成复杂工作后自动沉淀技能并在使用中改进（agentskills.io 标准，可设置写技能需批准）。
- **安装**：一行安装 + `hermes setup` 向导。

### 5.2 异同与各自优缺点【建议】

| 维度 | Kernel | Hermes |
|---|---|---|
| 定位 | 项目交付操作系统：需求→设计→实施→Review→发布，有审批门 | 通用个人 agent：广度优先，随叫随做 |
| 状态 | PostgreSQL，事务；唯一状态所有者 `machine.py`；每任务唯一游标 | SQLite；Kanban 行 + 进程 |
| 人在回路 | 每项目一张卡片，审批绑定文档/提交的 sha，内容变了审批自动失效 | 任务可阻塞等人，但没有按内容失效的审批 |
| 交付与追溯 | git 交付（worktree、commit、fast-forward），发布前验证，项目变更流水 | 没有正式的交付/发布流程 |
| 项目隔离 | 每项目独立 vault、agent、技能、凭据库、git 历史 | profile 隔离 |
| 自我改进 | 每周复盘出建议，经批准后迭代技能，有版本和变更日志 | 技能自动生成和改进，可选需批准 |
| 安装上手 | **弱**：无安装器、无向导、只在作者机器上跑 | **强** |
| 模型 | **只有 Codex** | 多家，随时切换 |
| 渠道 | **只有 Discord** | 20+ |
| 可观测性 | **弱**：只有 /health、/version 和 Discord 文本命令 | 看板、日志、成本分析 |
| 语言 | 只有中文 | 英文为主 |

一句话：Kernel 的护城河是**治理与可追溯**（审批、状态机、git 交付），Hermes 的长处是**上手、广度和可视化**。
Kernel 不该去学 Hermes 的“随意性”，而应补齐上手、多模型、可观测性这三块短板。

### 5.3 Kernel 看板设计【建议】

**原则**
- 看板只是 PostgreSQL 状态的**视图**；任何写操作都调用与 Discord 相同的 Kernel 入口（`kernel.py` 的卡片动作、消息），不另立状态，
  “唯一状态所有者”保持不变。网页点批准 = 点同一个卡片项，旧按钮失效判断照样生效。
- Discord 仍是主入口，看板是补充（看全局、看历史、看成本、做配置）。

**架构**：扩展现有 `kernel-api`（FastAPI，8001）：加 `/api/v1/*` 只读接口 + 打包进 wheel 的静态单页（无需前端构建链，如 htmx/Alpine）。
默认只监听 127.0.0.1；监听其他地址时必须用 token 登录（`InternalCredentials.kernel_api_token` 已有），未配置就拒绝启动（照 Hermes 的做法）。

**页面（按做的先后顺序）**

| 页面 | 内容 | 数据来源 |
|---|---|---|
| 总览 / Kanban | 列 = 阶段（queued、requirements、design、build、validating、review、release、completed），卡 = 任务；显示在等什么（某个 run 或某个卡片项）、已等多久、离 7 天自动结束还剩多久 | core.tasks、runs、interactions |
| 任务详情 | 时间线（事件、run、消息、卡片）、文档版本与差异、审批是否仍有效（sha 是否一致）、关联 commit | task_events、drafts/修订、iterations |
| 待处理 | 所有项目当前打开的卡片项，可直接回答/批准 | interactions |
| Run 与用量 | 耗时、失败、重试、token、按模型估算成本，按项目/天汇总 | runs.result 里的 usage（建议增加 `core.run_usage` 视图或表）+ models.yaml 单价 |
| 作业与服务 | core.jobs、项目服务状态、日志尾部 | jobs、supervisord |
| 项目 | 章程、agent 及其模型 profile、技能版本与模板差异、目录授权、凭据**名称**（不显示值）、git_push 开关 | vault 文件、skill_updates、grants |
| 系统 | 健康、版本、迁移、模型登录状态、Discord 连接、备份、doctor | — |
| 首次运行向导 | 第 1 节 `init` 的网页版 | — |

**分期**：D1 只读 Kanban + 任务详情 + Run 列表（一个 Round）；D2 网页操作卡片 + 用量/成本；D3 配置与首次运行向导。

### 5.4 还需要加强的功能（按优先级）【建议】

- **P0**：安装器 / init / doctor；多引擎 harness（Claude + 兼容端点）；语言最小改造（3.2 的 1–3）；仓库自带 compose 与 Postgres。
- **P1**：看板 D1–D2；用量与预算上限（现在的 `CORE_MODEL_CAP` 只是并发上限，没有花费上限）；跨平台备份/恢复；版本化镜像与升级命令；
  入站 Webhook 触发任务（你计划中的飞书 webhook 也走这里）。
- **P2**：更多渠道（`GatewayService` 已经和 `discord_app.py` 分开，加 Telegram/Slack/飞书适配器可行）；历史全文检索（PG `tsvector`，覆盖消息和文档）；
  跨安装共享技能模板（导出/导入，附版本与安全审查）；需要严格隔离的项目用每项目独立容器（README 已承认同一安装内项目之间不是硬边界）。

---

## 来源
- Discord Application 资源（PATCH `/applications/@me`，只能改 limited intent 位，没有创建接口）：https://docs.discord.com/developers/resources/application
- Codex 认证（device-auth、复制 auth.json、自动化推荐 API key）：https://developers.openai.com/codex/auth
- Codex 移除 chat wire API：https://github.com/janhq/jan/issues/7413 ，https://codex.danielvaughan.com/2026/04/23/codex-cli-custom-model-providers-configuration-guide/
- DeepSeek Anthropic 兼容 API：https://api-docs.deepseek.com/guides/anthropic_api
- Claude Code 接入 GLM 等：https://www.datallmlab.com/blog/claude-code-with-glm-5.html
- Claude 第三方登录政策：https://code.claude.com/docs/en/legal-and-compliance ，https://platform.claude.com/docs/en/agent-sdk
- Hermes Web Dashboard：https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard
- Hermes Kanban：https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban
- Hermes 综述：https://parallel.ai/articles/what-is-hermes-agent ，https://fast.io/resources/hermes-ai.md
