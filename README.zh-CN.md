# Super Assistant（超级助理）

[English](README.md) · 中文

系统内部的名字是 **Assistant Kernel**（Python 包名 `assistant_kernel`），本仓库就是它的源码。

一个以文档驱动的个人助理。你在 Discord 里和每个项目的 **PM**、**Supervisor** 对话；Kernel 替它们运行一次次简短的
`codex exec` 回合，所有状态都保存在 PostgreSQL 里，每个项目的文件放在各自的文件夹，并有各自的 git 历史。

整体设计、各项决定和每一轮的进展见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 和
[docs/ROUND5_PLAN.md](docs/ROUND5_PLAN.md)。`docs/` 里更早的文档描述的是以前的版本，留作历史记录。

## 一个任务怎么走

```text
#pm 频道的消息 ─► 分诊：直接回复、开一个任务，或转交给另一个项目
任务 ─► Supervisor 评估 ─► Discord 里的一张卡片（提问 / 下一步 / 决定）
你回答或点按钮 ─► PM 在当前阶段做一轮 ─► Supervisor 再评估 ……
阶段：需求 → 设计 → 实施 →（Kernel 校验）→ 复核 → 发布 → 完成
```

- 每个项目同时只有一个进行中的任务；进行中的任务每时每刻只等一件事：一次运行，或一张卡片。
- 文档（需求、设计）和每一次发布都要你批准；文档一改，原来的批准就失效。项目文件只能通过任务改变：
  候选文件先校验再发布；代码项目的仓库和授权可写的外部目录在实施阶段直接修改，批准发布时提交。
- `#pm` 里的消息交给当前任务（或开新任务）；`#supervisor` 里的消息是咨询。
- 文字命令：`/status`、`/pending`、`/cancel confirm`、`/evolve`、`/iterations`、`/grants`、`/jobs`、`/secrets`
  （`/cancel 确认` 也可以）。

## 语言与看板

- **语言**：界面统一用英文——卡片、按钮、通知、命令、看板，以及 Kernel 写进项目的文件；技能、角色和提示词也都是英文。
  agent 按你使用的语言回复：总结、问题和文档正文跟随你的语言（判断不出时用英文），文档标题保持英文。
- **看板**：`http://127.0.0.1:18001/dashboard`（由 `kernel-api` 提供，只能从本机访问）。用 `assistant-data/config/dashboard-token`
  里的令牌登录（首次启动时生成）。第一页是 Discord 设置：在 Discord 开发者门户自己创建应用和机器人（Discord 没有提供这一步的
  API），粘贴 bot token，看板会检查它、在 Discord 允许时打开 Message Content intent、给出邀请链接、列出机器人加入的服务器并
  保存设置，网关随即用新设置重启。

## 项目

每个项目是一个文件夹 `assistant-data/vaults/<项目>/`：

| 路径 | 内容 |
|---|---|
| `project.yaml` | 交付方式、能力 |
| `knowledge/charter.md` | 项目章程：用途、范围、路由 |
| `agents/<agent>/` | `role.md`、`config.yaml`，以及这个 agent 自己的技能（`.agents/skills/`） |
| `knowledge/` 里的其他文件 | 背景信息存档、用户画像、行业速写、项目地图——由 Kernel 写入 |
| `runtime/` | Kernel 内部数据，包括项目的凭据库——从不进 git |
| `runs/` | 各任务的工作文件——从不进 git |

默认项目 `kernel`（维护本 Kernel、创建和路由项目、公共背景）单独发布为 [project-kernel](https://github.com/vado290/project-kernel)，说明见它的 README。

## 敏感数据：存在哪里、谁能看到

这一节如实说明现状。把真实账号交给系统之前，请先读完。

### 1. 从不进入任何 git 仓库的东西

| 内容 | 位置 | 说明 |
|---|---|---|
| Discord bot token、模型服务商的密钥 | `assistant-data/config/service-credentials.json`、`provider-secrets.json` | 不在任何仓库里；备份脚本也不复制它们 |
| Codex 登录信息 | 各项目 `runtime/codex-home/` 里的 `auth.json` | 备份时会被删掉 |
| 项目凭据（登录账号、API 密钥） | `assistant-data/vaults/<项目>/runtime/credentials.json` | 每个项目的 git 都忽略 `runtime/` |
| 任务状态、消息、卡片 | PostgreSQL 的 `core` schema | 只在你自己的数据库里 |

本仓库只有代码、提示词和技能模板；测试里出现的密钥都是占位符。

### 2. 项目凭据怎么收集、怎么使用

1. 任务需要某个登录或密钥时，PM 先查项目已有的凭据（`list_credentials` 只列名称、服务和用途，从不显示值）。
2. 只有查不到时，PM 才向你提问，并把这个问题标记为凭据。卡片里的输入框没有预填答案，并会写明答案存到哪里。
3. 你提交后，Kernel 把值写进项目的凭据文件（第一次使用时创建，文件权限 600，写入时加锁）。对话记录里只留下
   “已保存为项目凭据 `<key>`”。
4. 确实需要用到值的 agent 用 `get_credential` 读取。技能要求 agent 不得把值抄进文档、代码、地图、日志或消息；
   这是提示词规则，不是技术上的锁。
5. `/secrets` 列出名称；`/secrets delete <key> 确认` 删除一条。

### 3. 谁能看到凭据的值

- **同一个项目的 agent**（PM、Supervisor、产品 agent）能通过 Kernel 工具读取。
- **其他项目的 agent**：Kernel 工具只作用于它自己的项目，没有读取别的项目凭据的途径。但 agent 还有命令行，可以读取
  本机上的文件（实施阶段以外只读；实施阶段因为要改代码，不受限制）。所以同一套安装里的各个项目之间**不是严格的
  安全隔离**，靠的是 agent 遵守规则。必须严格分开的工作，请放在另一套安装里。
- **模型服务商**：agent 在某一轮里读取了值，这个值就作为该轮上下文的一部分发送给模型服务商。
- **你的电脑**：每个项目的 `runtime/codex-home/` 里保存着 Codex 的会话日志（被 git 忽略）。

如果某个密钥不能接受上面这些情况，就不要交给助理保管；给项目的密钥尽量只开最小权限（例如交易所只读密钥）。

### 4. 个人背景信息

Kernel 会逐渐积累关于你和你的工作的记忆：背景信息存档（`knowledge/background.md`）、用户画像
（`knowledge/user-profile.md`）、行业速写，以及公共背景（`kernel/knowledge/shared/`）。这些文件都留在你自己的安装里。
私有项目会把它们提交进该项目自己的私有 git 历史；公开的 `kernel` 项目则忽略它们，每套安装各自生成（公共背景从
`core/shared_template/` 的模板开始，以后由 Supervisor 用卡片提议修改，你批准后才写入）。

### 5. 版本历史与 GitHub

- 每次发布前后，Kernel 都会提交项目文件夹（以及项目可写的外部目录），每一次改动都能追溯、能回退。
- 推送的目标是**你自己**的 GitHub 账号（`gh` 当前登录的账号，或 `CORE_GITHUB_OWNER`）下的仓库；新建的仓库一律是
  **私有**的。你执行 `gh auth login` 之前，什么都不会推送。
- 设置为 `git_push: false` 的项目只在本地提交，从不推送——私密的项目请这样设置。
- 把仓库改为公开，永远是你自己在 GitHub 上操作的一步。

### 6. 备份

`scripts/Backup-AssistantState.ps1` 会在 `assistant-data/backups/` 生成本地备份（数据库导出、各项目文件夹，包括
其中的凭据文件，但不含 Codex 登录信息）。这个目录不被任何仓库跟踪。备份请放在你自己控制的磁盘上，不要放到公开的共享位置。

### 7. 已知限制

问题超过 5 个的卡片会分页填写：前面几页已填的答案在提交前会作为草稿保存在数据库里——如果其中有凭据问题，
它的答案也会被保存。修复已在计划中。

### 8. 使用者自查清单

- 不要提交 `assistant-data/config/*.json`；你要公开的仓库里不能包含 `assistant-data/`。
- 密钥只开最小权限；凡是曾经贴到不该出现的地方的密钥，都要作废重新生成。
- 私密项目保持 `git_push: false`；把仓库改为公开之前，先检查里面的内容。

## 代码地图

| 路径 | 作用 |
|---|---|
| `core/sql/` | `core` schema 的数据库迁移（由 `assistant-kernel-migrate` 执行） |
| `core/rules.py` | 阶段、允许的跳转、关卡、批准的推导（纯函数） |
| `core/machine.py` | 任务状态机——唯一改变任务状态的代码 |
| `core/cards.py` | 每个项目唯一的那张卡片 |
| `core/kernel.py` | 入口：消息、卡片操作、认领 / 完成 / 失败的运行、恢复 |
| `core/dispatch.py` | 调度器：并发执行已认领的运行、租约、定期维护 |
| `core/executors/` | PM、Supervisor、分诊、校验、发布、建项目和作业的执行器 |
| `core/tools.py` | 一次模型调用可用的 Kernel 工具（stdio MCP，只作用于本项目，每次调用都检查租约） |
| `core/credentials.py` | 每个项目的凭据库 |
| `core/projectgit.py`、`core/projectmap.py` | 项目的版本历史与 GitHub；项目地图 |
| `core/skills.py`、`core/vault_sync.py` | 技能加载与模板同步；Kernel 自己的文件写入（存档、画像、公共背景） |
| `core/gateway/` | Discord 网关（卡片、按钮、弹窗、发件箱、提醒、命令） |
| `harness/` | `codex exec` 运行器、工具服务器启动、进程控制 |
| `services/` | 复用的库：工作区范围、文档、候选文件、校验 |
| `prompts/core/`、`prompts/skills/` | 输出约定；复制到每个 agent 文件夹里的技能模板 |

## 运维

```powershell
.\scripts\deploy.ps1            # 构建 wheel → test venv 安装并测试 → prod venv → 迁移 → 重启 → 检查
.\scripts\deploy.ps1 -TestOnly  # 只装 test venv 并测试
```

Kernel 运行在 Docker 容器里（路径在 `/home/v/AI` 下，数据在 `assistant-data/`）。生产进程（`deploy/supervisord.conf`）：
`kernel-core-dispatcher`、`kernel-core-gateway`、`kernel-api`（`/health/live`、`/health/ready`、`/version`、`/dashboard`），
以及从项目已发布的 `jobs.yaml` 登记的项目服务。

测试：`tests/core/`（每个测试使用一个临时数据库），以及 `tests/` 下的少量库测试。

## 许可证

[MIT](LICENSE)。`deploy/seccomp-codex.json` 改编自 Moby 的 seccomp 配置，仍按 Apache License 2.0 授权
（[deploy/LICENSE_moby_profiles.txt](deploy/LICENSE_moby_profiles.txt)）。
