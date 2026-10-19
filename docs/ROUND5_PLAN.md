# Round 5 方案：业务产物的结构、运行与运营闭环

> 状态：第 3 版，用户已同意（2026-10-18）。基于 0.43.2（main 51023f3）。
> 不推翻 Kernel 的现有架构：PM / Supervisor 的任务、卡片、验证、发布、复盘机制保持不变；
> 业务产物（product）更独立、更丰富：由登记过的"功能"组成，每个功能一个入口脚本、一个进程，由 Kernel 统一登记、启动和观察。

## 1. 已确认的决定

| # | 决定 |
|---|---|
| D1 | 每个项目一个 Codex home，PM、Supervisor 共用；产物另有自己的 Codex home（产物代码自己调用 agent） |
| D2 | 技能隔离靠目录：每个 agent 一个平级技能目录 `<agent 目录>/.agents/skills/`，相同技能允许重复；agent 目录以上的任何一级都不放 `.agents/skills`；agent 运行时的工作目录就是它自己的目录，Codex 原生发现并按需取用 |
| D3 | 关闭 Codex 自带的技能（每次运行时读取 Codex home 的 `.system` 目录动态关闭） |
| D4 | 产物内 agent 的配置写在 `product.yaml`；PM / Supervisor 保留各自的 `config.yaml` |
| D5 | 产物内 agent 只干活，不与用户交互；所需流程、角色、凭据在设计阶段由 PM 定好；运行中的问题记入日志，由 PM 解决 |
| D6 | PM 负责执行（建造、修改、修复），Supervisor 负责监控和分析，各有对应技能 |
| D7 | 产物的 MCP server 只暴露给产物内 agent；PM / Supervisor 不直接调用 |
| D8 | 凭据按项目隔离存于项目内一个 JSON；先查后问（PM 在设计 / 建造时通过问题卡片索取）；key 由 LLM 命名并带说明字段；不刻意对 LLM 隐藏 |
| D9 | 每个项目一套独立的 Python 环境 |
| D10 | 产物内 LLM agent 不使用交易类（不可逆）MCP 工具；不可逆动作由产物的确定性代码执行 |
| D11 | 产物由"功能"组成：每个功能一个入口脚本（`product/entries/`），在 `product.yaml` 登记；只有登记过的功能能被启动，不能任意启动某个模块 |
| D12 | 每个项目只有一份地图：文件交付在 `knowledge/map.yaml`，代码交付在其仓库（默认 `docs/map.yaml`，与 Round 4 决定 8 一致）；纯索引（名称、路径、用途、摘要），用 `project:` / `grant:` 前缀索引项目目录与外部目录的文件；历史由 git 提供（2026-10-19 修订，用户同意方案 A） |
| D13 | 每个项目一个 git 仓库（整个项目目录），发布前快照、发布后提交，自动创建 GitHub 私有远程仓库并推送；项目可写的外部目录各自一个仓库，在任务进入实施阶段时快照、发布后提交，使发布提交只含本任务的改动（2026-10-19 修订） |
| D14 | 产物工具包是 Kernel 维护的一个小库，按固定版本安装进各产物环境，复盘时建议升级 |
| D15 | 产物内 agent 通过 Codex（`codex exec`）调用，原生获得技能和 MCP，登录与主副本同步 |
| D16 | 产物整体发布、整体重启：每次发布后该项目所有常驻功能一起重启 |
| D17 | kernel 项目的产物是 kernel 源码，位置、交付和运行方式与普通项目不同，要兼容 |
| D18 | build 阶段可能需要复制外部资源、安装包等不受 git 跟踪的东西，相应技能写明做法 |
| D19 | 所有功能统一在一张登记表里，用 `run` 字段说明何时运行：`always`（常驻）、`window`（时间窗内常驻）、`cron` / `interval`（定时运行、做完退出）、`manual`（按需）；底层用 supervisord 还是 Kernel 作业调度由 Kernel 根据 `run` 自动决定 |
| D20 | 标记 `confirm: true` 的功能（真实资金、对外发送等）手动启动前先确认；定时和常驻的随发布一起批准 |
| D21 | 每个运行中的功能都有自己的日志，格式统一，Kernel 每日汇总，复盘技能收集分析 |
| D22 | 管理分两层：开发、发布按项目；运行（启停、心跳、告警、日志）按功能 |
| D23 | 任务边界：Supervisor 的"完成"判断标准写进技能；等待用户超过 7 天无回应的任务自动结束 |
| D24 | Kernel 每个版本自动给各项目补齐新增的模板技能、刷新项目没改过的副本；项目删除过的不再加回，改过的留给复盘决定（2026-10-19，R5.2 修复） |

## 2. 已核实的事实

- Codex 0.163 从工作目录向上直到 git 根收集 `.agents/skills` 和 `AGENTS.md`，另加 Codex home 的 `skills/`；本容器的 git 根是外层 `/home/v/AI`，其上没有技能或 `AGENTS.md`。现状：技能放在项目根，PM 看得到全部 22 个技能（含 Supervisor 的），Kernel 又注入一遍。
- `-c skills.config=[{path=…, enabled=false}]` 可关掉指定技能（`debug prompt-input` 下已测试）。
- 产物脚本目前用容器共享的 miniconda Python 运行（91 个包）。
- supervisord：容器内只有一个，Kernel 的 3 个程序在其中，配置已包含 Kernel 的程序目录 `runtime/kernel-services/programs/*.conf`；dispatcher 以 root 运行，可通过本地 socket 控制。登记、重启、查询、异常告警的代码 Round 3 已有，只在单元测试中用过。`deploy.ps1` 只重启 Kernel 自己的程序。
- Kernel 的作业调度（Round 3）：到点建 `job` run，dispatcher 以子进程运行脚本，有超时、重试、告警、错过只补最近一次、上一次未结束则跳过；同样只在单元测试中用过。
- 任务结束的现有方式：发布成功；你批准 Supervisor 的"完成"或"取消"提议；你批准复盘的后续任务；项目创建完成；你点停止或 `/cancel`；多次失败后停止。没有超时。
- git：项目目录目前不在任何仓库里。R5.2 时更正：trading-risk-bot 目录、`H:\AI\crypto`、`H:\AI\IB` 里的 `.git` 都是**空文件夹**（09-22 建），git 实际落到外层 `H:\AI` 仓库，它们都没有自己的历史；crypto、IB 的内容由外层仓库跟踪（各有白名单式 `.gitignore`）。容器里原本没有 GitHub 凭据和 `gh`（R5.2 已安装 gh 2.102.0，并写进 `Dockerfile.AI`）；容器 home 不持久，只有 `/home/v/AI` 是挂载的。
- R5.0 核实结果（2026-10-18，真实 `codex exec`，验证项目的 Codex home）：
  1. **MCP**：Codex 默认把 MCP 工具延迟加载（`tool_search_always_defer_mcp_tools=true`），模型需要时再找到并调用，工具多也不占初始上下文；`enabled_tools` 之外的工具不可用（已测）。没有声明只读 / 非破坏性标注的工具在 `approval_mode="auto"` 下会被"需要批准"挡住；产物 MCP 由 AgentRunner 以 `default_tools_approval_mode="approve"` 传入（只对 `product.yaml` 允许的工具），已测可正常调用。Kernel 自己的工具带标注，不受影响（生产 PM 正常调用）。
  2. **续接会话**：在另一个工作目录 `exec resume` 正常，记得上一轮内容，并以新目录为工作目录。
  3. **关闭自带技能**：`exec` 下 `skills.config` 同样生效，模型只看到工作目录下的技能。

## 3. 目录结构

规则：`.agents/skills` 只能出现在 `agents/<名称>/.agents/skills`；项目根、`product/` 根、`agents/` 这一级都不放；项目内不放 `AGENTS.md`（kernel 源码仓库例外）。Kernel 在验证时检查。

```
<vault>/                               项目（一个 git 仓库）
  project.yaml                         项目配置（只保留 Kernel 读取的字段）
  .gitignore                           runtime/、runs/、product/data/
  knowledge/  charter.md  map.yaml  user-profile.md  domain-brief.md
  agents/
    pm/          config.yaml  role.md  .agents/skills/<技能>/SKILL.md     PM 的工作目录
    supervisor/  config.yaml  role.md  .agents/skills/<技能>/SKILL.md     Supervisor 的工作目录
  product/                             业务产物（kernel 项目除外，见 §9.3）
    product.yaml                       功能登记表与运行声明
    entries/                           每个功能一个入口脚本（只有这里的脚本能被启动）
    workflows/  gateways/  lib/        功能内部的代码（流程、外部连接、公共代码）；按需
    agents/<名称>/  role.md  .agents/skills/<技能>/SKILL.md              产物 agent 的工作目录
    mcp/<server>/                      MCP server；按需
    config/                            各功能的配置文件
    assets/                            随发布的小型资源
    tests/  requirements.txt
    data/                              运行数据：每个功能 data/<功能>/，共享数据 data/shared/；不入 git
  runs/                                任务记录；产物日志 runs/product/（不入 git）
  runtime/                             Kernel 内部，不入 git
    codex-home/                        PM / Supervisor 的 Codex home
    product-home/                      产物 agent 的 Codex home
    venv/                              产物的 Python 环境
    credentials.json                   项目凭据
```

## 4. `product.yaml`（schema `product.v1`）

```yaml
schema_version: product.v1
kit: "1.0"
python: {requirements: requirements.txt}
tests: {command: "python -m pytest -q tests", timeout_seconds: 600}
grants: [crypto]                       # 用到的授权目录 → 环境变量 GRANT_CRYPTO=<路径>
functions:
  binance-live:
    entry: entries/binance_live.py
    run: always                        # 常驻；发布时启动，崩溃自动重启
    description: 币安实盘交易：加载 config/binance.yaml 的策略，实时下单
    when_to_use: 正常交易时始终运行
    side_effects: 真实下单
    confirm: true                      # 手动启动前确认
    credentials: [binance_api]
  ib-live:
    entry: entries/ib_live.py
    run: {window: {start: "0 9 * * 1-5", stop: "30 15 * * 1-5"}}   # 时间窗内常驻
    description: IB 实盘交易
    side_effects: 真实下单
    confirm: true
    credentials: [ib_account]
  backtest:
    entry: entries/backtest.py
    run: manual                        # 按需运行，做完退出
    description: 用历史数据回测 config/ 中的策略，结果写 data/backtest/
    timeout_seconds: 3600
  daily-report:
    entry: entries/daily_report.py
    run: {cron: "0 8 * * *"}           # 定时运行，做完退出；最短间隔 1 分钟
    description: 汇总前一天的交易，写日报并通知
    timeout_seconds: 600
    agents: [analyst]
agents:
  analyst:
    purpose: 读取每日数据，写出三条经营发现
    model: {reasoning_effort: medium, timeout_seconds: 900}
    core_skills: [weekly-analysis]
    mcp: {market: [get_quote]}
mcp:
  market:
    transport: stdio
    command: [python, mcp/market/server.py]
    tools:
      - {name: get_quote, effect: read, purpose: 查询报价}
assets:
  - {path: data/models/x.bin, source: "https://…", sha256: "…", note: "模型文件，发布时下载"}
```

**功能分类规则**（写进 workflow 设计技能）：默认是"做完退出"；满足任一项即为常驻（`always` 或 `window`）——要实时响应外部事件、要在内存里保持状态、触发比每分钟一次更频繁。需要常驻功能内存状态的定时动作（例如 14:55 平仓）写在该功能内部的定时器里，不单独登记。

**校验**：`entries/` 下的每个脚本都已登记、每个登记都指向存在的入口；`run` 合法（定时间隔不短于 1 分钟，`window` 的开始和结束都要有）；做完退出的功能必须有超时；描述字段齐全；agent、核心技能、MCP 引用存在且一致；`effect: trade` 的工具不出现在任何 agent 的 `mcp` 里；依赖可安装；端口跨项目不冲突；两个常驻功能声明同一凭据时给出警告。

## 5. 运行机制

**5.1 PM / Supervisor 的技能与角色**：Kernel 显式注入 `role.md` 和当前阶段 / 用途的核心技能全文；工作目录设为该 agent 的目录，其余技能 Codex 原生按需取用；关闭 Codex 自带技能。代码交付的 build / review（工作目录必须是任务 worktree）沿用现有的"按需技能清单"。模板技能按 `load.roles` 复制到对应 agent 目录。

**5.2 产物工具包（D14，不依赖 Kernel）**

| 组件 | 作用 |
|---|---|
| 入口辅助 `run()` | 每个入口脚本调用它：读取功能配置、建立数据目录、日志、心跳（常驻）、优雅停止、演练模式 |
| EventEngine | 事件队列和分发线程，按事件类型注册处理函数，内置定时事件（参照 vnpy）；常驻功能内部使用 |
| 内部定时器 | 常驻功能内部的定时动作 |
| Workflow / Gateway | 流程与外部连接的基类 |
| AgentRunner | 调用 LLM agent：`codex exec`，工作目录 `agents/<名称>`，产物 Codex home，按 `product.yaml` 传入 MCP，登录与主副本同步 |
| StateStore | 每个功能自己的 SQLite（`data/<功能>/state.sqlite`）：键值状态、业务记录 |
| 日志 | 统一的 JSON 行格式（§6.3），自动遮蔽已知凭据的值 |
| 优雅停止 | 收到停止信号后给功能时间保存状态、按设计处理挂单等，再退出 |
| 演练模式 | 独立数据目录，`DRY_RUN=1`，外部副作用跳过 |

**5.3 产物 MCP**：AgentRunner 按 `product.yaml` 为每个 agent 传入允许的 server 和工具（`enabled_tools`，审批方式 `approve`）；Codex 延迟加载 MCP 工具，按需取用；PM / Supervisor 的运行不加入任何产物 server（D7）。

**5.4 Python 环境（D9）**：验证时在临时环境（共享 pip 缓存）安装依赖并运行测试；发布后依赖或工具包版本有变化时建 `runtime/venv.new`，成功后替换，失败保留旧环境并告警；所有功能都用 `runtime/venv` 运行；agent 的 shell 设置 `PIP_REQUIRE_VIRTUALENV=1`。

**5.5 凭据（D8）**：`runtime/credentials.json`，每条 `{key, value, service, purpose, used_by, created_at, updated_at, source}`；Kernel 工具 `list_credentials`、`get_credential`；PM 的问题卡片支持"凭据"字段，提交时 Kernel 写入文件；功能启动时得到 `PROJECT_CREDENTIALS` 路径和所声明 key 的环境变量 `CRED_<KEY>`；`/secrets` 列出、删除。

## 6. 功能的启动、控制与观察

**6.1 Kernel 怎样执行四种 `run`**（登记在 `core.jobs`，它即功能登记表）

| `run` | 底层 | 说明 |
|---|---|---|
| `always` | supervisord 程序 `proj-<项目>-<功能>`（自动启动、崩溃重启） | 发布时启动；停止时给优雅停止的时间 |
| `window` | 同一个 supervisord 程序，但不自动启动 | Kernel 的调度在开始时间启动、结束时间停止；时间窗内崩溃由 supervisord 重启 |
| `cron` / `interval` | Kernel 现有作业调度 → `job` run → 子进程 | 超时、重试、告警、错过只补最近一次、上一次未结束则跳过 |
| `manual` | 做完退出的功能：`job` run；常驻功能：supervisord 程序 | 由你、PM 或 Supervisor 启动 |

所有功能都用 `runtime/venv` 的 Python 在 `product/` 下运行，环境变量包括项目 ID、功能名、数据目录、日志目录、凭据、授权目录、产物 Codex home、发布版本、`DRY_RUN`。

**6.2 发布（D16）**：PM 完成 → 验证 → 你批准发布（卡片列出将要重启的常驻功能）→ 发布执行：放置文件 → git 提交（项目与外部目录）→ 依赖有变化时重建环境 → 按登记表增删改 supervisord 程序和作业 → 重启全部正在运行的常驻功能 → 检查每个常驻功能进入运行并写出心跳。正在运行的"做完退出"功能允许跑完（用旧代码），下次运行用新代码。失败时告警，卡片提供"回滚到上一版"（git 回退后重新执行发布步骤）和"开修复任务"。

**6.3 日志与归档（D21）**
- 每个功能写 `runs/product/<功能>/<日期>.jsonl`，每行：`{ts, function, run, version, level, event, msg, data}`，`level` 为 `info | warn | error | incident | notify`；标准输出和错误也写在同一目录。
- `incident`：Kernel 发现后告警到 Supervisor 频道；`notify`：Kernel 转发到该行指定的频道（默认 PM 频道）。
- 常驻功能每 30 秒写 `heartbeat.json`（含进程号和发布版本）；超过 3 分钟没有更新即告警。
- Kernel 每日汇总前一天：各功能的运行次数、失败、耗时、各级别条数、高频错误、事件清单，存 `runs/product/summaries/<日期>.json`（长期保留）；原始日志保留 30 天。

**6.4 控制与选择功能**
- 命令：`/product status | start | stop | restart | run | dryrun <功能>`；PM / Supervisor 有对应工具，另有 `product_runs(功能)`、`product_logs(功能, 时间, 级别, 关键词)`。
- 只能从登记表中选择；PM / Supervisor 依据登记的描述、使用时机和副作用选择功能。
- `confirm: true` 的功能：Supervisor 发起时先出确认卡片；你自己发命令时需加"确认"（与 `/cancel` 相同）。
- `/product status` 随时由登记表、supervisord 状态、心跳和最近运行统计得出，不另外维护。

**6.5 PM / Supervisor 分工（D6）**：Supervisor 读心跳、运行记录、日志与汇总，向你解释告警，判断重试 / 演练 / 开修复任务，并为复盘提供运行事实；PM 执行建造、修改、修复与演练验证。

## 7. 验证、发布、git 与复盘闭环

- **预览目录**：build 阶段 Kernel 把"当前产物 + 本任务候选文件"合成到 `runs/<任务>/preview/`，PM 在其中跑测试、演练；依赖没有变化时直接用 `runtime/venv`，有变化时在任务目录里建临时环境。
- **验证**：结构规则（§3）、`product.yaml` 校验（§4）、依赖安装与测试、工具包自检（导入、MCP 探测）、有改动的功能演练一次（常驻功能启动后要在限定时间内写出心跳再优雅停止；做完退出的功能要在超时内结束）。
- **发布**：见 §6.2。`product.yaml` 沿用 0.43.0 的规则：只登记发布过的版本，直接修改不生效并提示一次。
- **git 简化记录**：地图不记历史；变更流水记录各仓库的发布提交号，文件清单取自发布提交（外部目录的改动也在其中）；变更流水只保留原因、预期效果、衡量方法、复核结论；提交信息带任务和目标（2026-10-19 修订：流水在提交之后才生成，改由流水记提交号）。
- **复盘材料**：每日汇总（各功能的成功率、耗时、事件、高频错误）、各 agent 与 MCP 工具的调用与错误、闲置的功能 / agent / 工具；`assistant-operation`、`assistant-evolution-review` 增加对应检查项。

## 8. 任务边界（D23）

- Supervisor 技能写明"完成"标准：改了文件的任务必须以发布结束；分析、调研类任务在最终文档交付且你看过摘要后提议完成；单纯提问不建任务（由分诊直接回答）。
- 维护节拍检查：处于"等待你"的任务超过 7 天没有你的任何消息或操作，自动结束（取消，原因"超时无回应"），并告诉你如何继续；项目的下一个任务随即开始。

## 9. 技能（每期与相应机制同时改）

| 技能 | 归属 | 内容 |
|---|---|---|
| assistant-product-structure（重写） | PM（design、build） | 基础目录、`entries/` 约定、`product.yaml` 登记表、地图登记、工具包用法 |
| assistant-workflow-design（重写） | PM（design、build） | 功能分类规则、四种 `run`、功能内部的事件与定时、状态与幂等、优雅停止、演练、测试；设计文档的"功能清单"一节 |
| assistant-agent-design（重写） | PM（design、build） | 产物 agent 的角色、核心技能与按需技能、在功能代码中的调用方式 |
| assistant-mcp-design（重写） | PM（design、build） | server / 工具划分、effect 分级、哪个 agent 在什么情况下用、预期结果、验证 |
| assistant-build-resources（新，D18） | PM（build） | 依赖只写进 `requirements.txt`；外来大文件放 `data/` 并登记到 `assets`（来源、校验值）；小型资源放 `assets/`；需要系统包时向你提出，由你加入 Dockerfile；外部目录的改动先停或先演练 |
| assistant-product-operation（新） | PM | 建造与修改、演练验证、修复任务、按登记表选择功能 |
| assistant-product-monitoring（新） | Supervisor | 状态、日志与汇总的阅读；告警解读；重试 / 演练 / 修复任务的判断；复盘事实 |
| assistant-credentials（新） | PM、Supervisor | 先查后问、key 命名、说明字段 |
| assistant-goal-supervisor（更新） | Supervisor | §8 的完成标准 |
| assistant-operation、assistant-evolution-review（更新） | PM（evolution） | §7 的复盘检查项 |
| 需求、设计、实施、Review 技能（小改） | PM | 需求与设计文档增加"功能清单"一节；Review 逐项核对 |

## 10. 迁移与兼容

**10.1 现有项目**（迁移前备份；先在 kernel-v12-validation 上做）
- 技能：项目根 `.agents/skills/<技能>` 按 `load.roles` 移到 `agents/pm/`、`agents/supervisor/`。
- 旧目录（handoffs、changes、capabilities、operations、reports、sources、artifacts、archive、project-bootstrap.json、source-map.yaml 等）移到 `archive/pre-r5/`；`project.yaml` 只保留 Kernel 读取的字段，原文件存档；其中仍有价值的内容（如 trading-risk-bot 的 boundaries、non_goals）列给你决定是否写进章程。
- 现有 `knowledge/map.md` 转为 `map.yaml`。
- 撤销已退役的 butler-maintenance 的目录授权。
- 退役：Kernel 的 `agent` 作业类型、`service_restart` 作业类型、`jobs.yaml`（由 `product.yaml` 取代；没有项目在用）。

**10.2 git**
- 在容器安装 `gh` 并写进 `Dockerfile.AI`；`gh` 配置与 git 凭据助手放在挂载目录 `/home/v/AI/.container-home/gh`；你在容器里执行一次 `gh auth login`。
- 每个项目目录初始化仓库并写 `.gitignore`，远程 `vado290/project-<项目 ID>`（私有）；新建项目时自动创建。
- 外部可写目录：远程改为 `vado290/<项目 ID>-<目录 key>`（私有），原远程保留为 `legacy-origin`。
- 外层 H:\AI 仓库停止跟踪 crypto、IB（文件不动）。
- trading-risk-bot 目录里的旧 `.git` 先存档再重新初始化。
- `gh` 不可用或推送失败时只告警，本地提交照常。

**10.3 kernel 项目（D17）**

| 项 | 普通项目 | kernel 项目 |
|---|---|---|
| 产物位置 | `<vault>/product/` | kernel 源码仓库（`project.yaml` 的 `repository`） |
| 交付 | 文件交付（候选 → 验证 → 发布） | 代码交付（任务 worktree → 提交 → 快进主分支），不变 |
| 运行 | 发布后按登记表启动功能 | 没有 `product.yaml`，不登记；由你用 `deploy.ps1` 部署，发布只提示"可以部署" |
| 项目目录仓库 | 自动创建 | 同样创建 `project-kernel`；源码仓库 `vado290/assistant-kernel` 不变 |
| 地图 | `knowledge/map.yaml` | 源码仓库的 `docs/map.yaml`（与代码一起提交和验证；项目目录的文件用 `project:` 前缀）；`ARCHITECTURE.md` 作为设计文档保留（2026-10-19 修订） |

通用规则：产物位置和交付方式由 `project.yaml` 决定；没有 `product.yaml` 时跳过环境、登记与运行。

## 11. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| R5.0 | 核实 §2 末尾三项 | 结论写进第 2 节 |
| R5.1 | PM / Supervisor 的技能目录与工作目录、关闭自带技能、模板按 agent 安装、目录规则校验、迁移脚本（技能与旧目录）；任务边界（§8） | PM 只看到自己的技能；三个现有项目迁移后主流程正常；超时任务自动结束 |
| R5.2 | 每个项目一个 git 仓库（`gh`、`.gitignore`、发布前后提交、远程创建与推送、外部目录仓库）；单一地图 `map.yaml` 与查询工具；凭据存储、工具与卡片字段 | 一次发布在本地和远程都留下提交；地图校验通过；凭据先查后问 |
| R5.3 | 产物工具包 1.0（§5.2）与产物基础模板；产物 Codex home 与登录同步 | 示例产物在演练模式下：常驻功能写心跳并优雅停止；定时功能调用 agent（带一个 stdio MCP）后退出；日志格式正确 |
| R5.4 | Kernel 与产物的桥：`product.yaml` 校验与登记表、四种 `run` 的执行、发布时环境 / 登记 / 整体重启 / 心跳检查 / 回滚、日志扫描（告警与通知）、每日汇总、`/product` 与 PM / Supervisor 工具、确认卡片、预览目录 | 示例产物经发布启动；崩溃自动重启；`incident` 触发告警；`notify` 到达频道；回滚可用 |
| R5.5 | §9 技能补全、复盘材料、端到端 | 在 kernel-v12-validation 里用真实任务从需求做到发布一个小产物并运行一周期复盘 |

每期与相关技能、合同一起改并测试、部署；主干流程始终可用；小概率情况不加重代码，后续按需打补丁。

**进度**：R5.0 完成（2026-10-18，结果见 §2）；R5.1 完成（0.44.0，三个项目已迁移，备份在 `backups/round5-r51-20261018/`）；
R5.2 完成（0.45.1：git、地图、凭据；五个仓库已建立首次提交；远程推送待 `gh auth login`）。
验收审计后修复（0.45.2，2026-10-19）：地图 `project:`/`grant:` 前缀与 kernel 地图补全（含覆盖测试）、技能模板按 Kernel 版本自动同步（三个项目已同步并提交）、发布提交号记入变更流水、外部目录在实施开始时快照；计划 D12/D13/D24、§7、§10.3 已按用户同意修订。
系统地图审查后修复（0.45.3，2026-10-19，`assistant-data/system-map/generations/gen-20261019-fef3d31` 的 F1–F3）：平台工具加入 `get_authorized_workspace` 与 `list_recent_history`，Supervisor、分诊和产品 agent 的工具从此可用；agent 配置只保留加载器读取的字段、工具只用当前名称（模板、发布校验与迁移脚本 `scripts/r0453_agent_config_migrate.py`），PM 补上 `vault_list`、`vault_read_text`、`read_project_document`；技能 assistant-goal-supervisor 6.3.0、assistant-capability-maintenance 2.3.0 写明这些规则；新增守卫测试：技能和提示里点名的工具，加载它的角色必须用得了。
系统地图第二轮后修复（0.45.4，2026-10-19，`gen-20261019-69b5264` 的 F8、F4、F6）：任务卡片等用户处理时（周度复盘最常见），用户在 PM 频道提出与当前任务无关的新工作，Supervisor 复核时提议新任务，批准后排在当前任务之后、它结束后自动开始（复用咨询的"创建新任务"卡片；技能 assistant-goal-supervisor 6.4.0）；`/version` 的 schema 字段改为最新迁移（含测试）；trading-risk-bot 护栏技能更正旧术语（1.0.1，项目自己的副本）。
记忆机制改进（0.45.5，2026-10-19，用户逐项确认）：任务未获批结束时也保存需求文档的用户画像观察（背景事实不存）；Supervisor 在咨询和复核用户补充时用 `profile_observations` 记下用户明说的协作偏好与纠正，Kernel 写入画像并告知用户；Supervisor 的 prompt 增加任务状态簿，分诊增加用户画像；其他项目标“公共”的背景事实由 Kernel 列成候选，进入 kernel 的复盘材料和咨询提示；画像、存档、行业速写和公共背景的自动写入各自单独提交 git；技能 assistant-user-insight 1.1.0、assistant-goal-supervisor 6.5.0、assistant-requirements 3.2.2、assistant-domain-research 1.0.2、assistant-shared-context 1.1.0、assistant-evolution-review 2.1.0；trading 的旧记忆文件 interaction-memory.jsonl 迁移后归档。

## 12. 风险与对策

- **Codex 行为假设**：R5.0 先核实，不符时调整 §5.3 的做法。
- **整体重启影响实盘**：发布卡片列出将要重启的常驻功能，批准即同意；工具包的优雅停止是硬性要求，由验证中的演练检查。
- **常驻功能反复崩溃**：supervisord 的重试上限用尽后标记失败，Kernel 告警，Supervisor 分析，可一键回滚。
- **迁移影响现有项目**：先在 kernel-v12-validation 验证；旧内容全部存档，可还原。
- **外部目录被直接修改**：发布前快照、发布后提交，可从 git 回滚；修改运行中的功能前先停或先演练（技能规定）。
- **复杂度**：Kernel 侧新增的是桥（登记、启停、日志扫描与汇总）和 git 提交，并复用已有的 supervisord 登记与作业调度；产物内的复杂度由工具包承担并独立版本化。
