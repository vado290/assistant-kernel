# 第三轮改进计划：状态交接、技能补全、通用运行时

> 状态：方案已确认（2026-10-16），S1 实施中。
> 前置：第二轮 R1、R2 已部署（0.35.0），PM 续轮增量提示和文档草稿编辑已提交（`4ca7952`）。
> 原则：顾全局、闭环、无副作用；主线优先（MVP），不为小概率事件写重代码，不追求一步到位，长期运行中再打补丁；
> 发现与既有决定冲突时先问用户。

## 1. 目标

1. **交接不依赖长期上下文**：PM 可以在一个会话里连续工作，但任何时候换新会话（换阶段、会话过长），只靠 Kernel 的状态簿、
   PM 的工作笔记和工件就能无损接续。
2. **子 agent 可预期**：何时派、带什么上下文、产出什么，事先写在技能里；派发情况有记录、进复盘。
3. **技能周全**：需求阶段先调研行业再提问；揣摩用户并按项目存档用户画像；设计、实施、运维技能覆盖到产物上线运行。
4. **产物运行闭环**：所有项目共用一套定时与服务管控组件；从需求、设计、实施登记，到验证、发布即登记、运行、告警、运维、复盘，
   格式只定义一次，全程共用。

## 2. 用户已确认的决定（2026-10-16）

| # | 决定 |
|---|---|
| 1 | "PM 尽量无状态"指交接资料齐全、不依赖长期上下文；会话可以连续，续轮增量提示保留 |
| 2 | 子 agent：先用"技能中的子 agent 定义 + Codex 原生子 agent + Kernel 记录"，不做 Kernel 级子运行 |
| 3 | 用户画像在每个项目各存一份（按用户分节，便于以后多人使用）；公共背景只保留组织层面的事实 |
| 4 | 揣摩用户的技能要尽量周全：超级助理的目的是减轻人在项目开发中的负担 |
| 5 | Kernel 增加通用运行时：定时作业和长驻服务（服务由容器内 supervisord 托管；"每月重启"即间隔较长的定时作业）；所有项目共用，带所属项目、状态登记和运维功能 |
| 6 | 运行时从技能开始闭环：build 时登记，格式字段统一自洽 |
| 7 | 顺序：S1 → S2 → R3 → S3 → R4a → R4b（R3、R4a、R4b 见 `ROUND2_PLAN.md`） |

## 3. 已核实的事实（2026-10-16，真实 Codex 回合，临时数据库）

- Codex 0.157.1 的联网搜索在只读沙箱下可用（服务器端执行，返回来源网址）。
- 原生子 agent 可以调用 Kernel 的 MCP 工具，调用记在父回合名下。
- 派发参数只有 `task_name`、`message`、`fork_turns`、`model`、`reasoning_effort`：不能预先注册子 agent 类型；
  `fork_turns` 不设时子 agent 从空上下文开始，设了则继承主 agent 的对话。
- 子 agent 的派发和内部工具调用不出现在主回合的事件流中：Kernel 只能通过 PM 自报得知。
- 旧调度器（`SCHEDULER_0310.md`）生产上从未启用（相关表 0 行），已在 4b 删除；保留了纯时间计算 `services/schedule_time.py`。
- 重启或更新单个 supervisord 程序不影响其他程序（docling 在多次部署中持续运行 2 天以上）；只有重启 supervisord 本身才会全部重启。

## 4. S1 状态与交接

### 4.1 Kernel 状态簿（确定性生成，不经过模型）
- 内容：任务目标、原因、流程、阶段和状态；批准情况（是否仍然有效）；当前工件及 sha；按回合排列的讨论记录：
  每个 PM 回合的输入（用户确认的指令、问题与回答、附言、用户补充消息）和输出（小结、提出的问题），
  以及卡片上的其他操作（修改意见、拒绝、停止）。较早的回合只保留一行小结。
- 来源：`core.runs`（pm_turn 的输入和结果）、`core.messages`（补充消息）、`core.task_events`（卡片操作），都已存在。
- 每个 PM 回合开始前写入 vault `runs/<任务>/state.md`（子 agent 可读）；**新会话**的提示中包含全文，续轮不重复发送。

### 4.2 PM 工作笔记（新文档类型 `working_notes`）
- 只有 PM 能写的语义状态，固定章节：当前理解 · 已定事项 · 开放问题 · 假设与默认 · 下一步。
- 任何阶段都可用 `edit_task_document` 修改（不受"本阶段文档"限制），回合结束时 Kernel 有变化就存为修订版；不涉及批准。
- 合同（`pm-output.md`）要求：每轮结束前更新工作笔记。这是 Kernel 依赖的流程事实，写在合同里，不只写在技能里。

### 4.3 会话轮换
- 下一轮开新会话的条件：换阶段（已有），或当前会话已连续 8 轮。新会话只带状态簿、工作笔记、工件和本轮上下文。
- Codex 自身的自动压缩继续兜底防止超限；轮换保证交接质量不依赖压缩后的上下文。

### 4.4 技能
- `assistant-task-continuity`：增加工作笔记的章节规范，以及"状态簿是事实、工作笔记是你的理解"的分工。

退出标准：同一任务跨阶段、跨会话轮换后，PM 不需要用户重复任何已答内容；测试覆盖状态簿内容、工作笔记保存、轮换条件。

**S1 完成记录（2026-10-16，已部署 0.36.0）**：
- 代码：`Kernel.task_ledger` 与 `core/executors/state.py`（状态簿生成并写入 `runs/<任务>/state.md`，只在新会话的提示中给全文，
  续轮提示中给出文件路径）；`working_notes` 文档类型（`drafts.editable`：本阶段文档 + 工作笔记，任何阶段可编辑；回合结束有变化即存修订版，
  状态机记入任务文档）；会话轮换（`pm_session.turns`，`machine.SESSION_TURNS = 8`）；合同 `pm-output.md` 要求每轮更新笔记；
  技能 assistant-task-continuity 2.2.0（三个项目中未修改过的副本已更新）。
- 同时上线了此前已提交、尚未部署的"续轮增量提示 + 文档草稿编辑"（`4ca7952`）。
- 测试：全部 162 项通过、3 项跳过（新增 `tests/core/test_state.py`）。
- 简化说明：状态簿在每个 PM 回合开始时生成，不单独维护；"用户补充"中会出现创建任务的那条原始消息，无害，暂不区分。
- 真实模型冒烟（kernel-v12-validation，临时数据库，自动驾驶答卡）：
  - 第一次：工作笔记质量良好（五节齐全，3 个修订版）；但轻量流程的需求阶段 PM 把 outcome 写成 `ready_for_validation`，
    Kernel 拒绝时 PM 看不到原因，3 次重试重复同一错误，任务失败。属于 R2 遗留问题。
  - 修复（0.36.1）：outcome 不属于当前阶段时，Kernel 在同一会话里告诉 PM 原因并让它重出一次 JSON（与修正非法 JSON 的做法相同）；
    合同写明 `ready_for_validation` 只用于实施阶段。
  - 第二次：提问 → 回答 → 需求文档 → 换阶段开新会话实施 → 验证 → 发布批准 → 完成，主线走通。冒烟在该测试项目 vault 中
    发布了 `docs/weekly-checklist.md`（与以往冒烟的 hello.md 同类）。

## 5. S2 技能补全

| 技能 | 处理 | 要点 |
|---|---|---|
| assistant-domain-research | 新建 | 固定调研维度：行业现状与趋势、典型业务流程、指标与术语、按地区的监管合规、主流工具与竞品、常见痛点、不同企业规模的做法、语言与本地化。产出行业速写、候选问题库、候选需求清单（标`[建议]`），每条带来源和日期，有效期半年 |
| assistant-requirements | 修改 | 第 0 步：没有有效的行业速写就先派调研子 agent；第 1 轮问题从候选问题库中挑选，按地区、语言、规模定制 |
| assistant-user-insight | 新建 | 推断维度：专业程度、决策风格、时间压力、风险偏好、表达习惯、常见顾虑、偏好的提问方式；依据卡片操作模式；推断标`[推断]`附证据；据画像调整提问数量、术语和推荐力度；用户纠正优先 |
| assistant-subagent-orchestration | 修改 | 子 agent 定义目录（行业调研、现有系统盘点、反例审查、并行实施）：触发条件（技能要求必须派的 / 由 PM 判断的）、必读输入（state.md、章程章节、草稿路径）、工具范围、产出格式；默认不继承上下文 |
| assistant-task-continuity | 修改 | 子 agent 结果如何写进工件 |
| assistant-solution-design | 修改 | 设计文档增加"运行方式"一节（与 S3 的 manifest 字段对应） |

- **用户画像**：项目 vault `knowledge/user-profile.md`，按用户分节。需求文档增加"用户画像观察"一节，批准时 Kernel 追加到画像文件
  （与背景信息存档同一机制）；周度复盘可以提议修订。
- **子 agent 记录**：PM 输出增加可选字段 `delegations: [{name, goal, result}]`，Kernel 记入运行结果和复盘材料。
- **行业速写存档**：随需求批准写入项目 `knowledge/`，有效期内不重复调研。

**S2 完成记录（2026-10-16，已部署 0.37.0）**：
- 代码：
  - 新文档类型 `domain_brief`（只在需求阶段可编辑，调研子 agent 可直接写入；与工作笔记统称"自由文档"：有变化即存修订版，不需批准）；
  - 需求获批时，Kernel 除背景存档外，把需求文档的"用户画像观察"按用户（来源消息的用户 ID）追加到 `knowledge/user-profile.md`，
    把行业速写写成 `knowledge/domain-brief.md`（旧版移到 `knowledge/archive/`）；都可重复执行；
  - 用户画像注入 PM（每轮，续轮只在变化时）和 Supervisor 的上下文；行业速写注入需求、设计和复盘阶段的 PM；
  - PM 输出可选字段 `delegations`，记入运行结果、任务状态簿和复盘材料（按子 agent 名称计数）。
- 技能：新增 assistant-domain-research 1.0.0（按需读取）、assistant-user-insight 1.0.0（PM 与 Supervisor 常驻）；
  修改 assistant-requirements 3.1.0、assistant-subagent-orchestration 2.1.0（四个子 agent 定义）、assistant-task-continuity 2.3.0、
  assistant-evolution-review 1.1.0。三个项目都已安装新技能并更新副本（`scripts/round3_s2_vaults.py`；R1 时安装、没有指纹的副本
  用 `scripts/round2_r2_vaults.py` 的已知指纹识别后更新，现在所有副本都有指纹）。
- 测试：全部 167 项通过、3 项跳过（新增 `tests/core/test_s2.py`）。
- 调整：设计文档的"运行方式"一节推迟到 S3，与 manifest 字段一起定义，避免技能先引用尚不存在的运行时。
- 真实模型冒烟（kernel-v12-validation，任务"上海约 20 家连锁便利店的每日经营日报规范"）：主线走通（提问 7 项 → 需求 → 实施 → 验证 →
  发布 → 完成）。PM 在第 1 轮按技能派出 industry-research 子 agent，写成约 6900 字的行业速写（定位参数、有效期、带来源编号的事实、
  候选问题），7 个问题即取材于此；`delegations` 进入了状态簿；需求获批后 Kernel 把速写存为 `knowledge/domain-brief.md`。
  需求文档的"用户画像观察"为"无"（自动驾驶只答了一轮，未达到"至少两次"的证据要求），画像追加由单元测试覆盖。

## 6. S3 通用运行时（定时作业与长驻服务）

### 6.1 唯一来源：`jobs.yaml`（格式 `runtime-manifest.v1`）
格式只在 Kernel 中定义一次（代码校验 + 合同说明），技能只引用。时间规则与 `schedule_time.py` 的校验一致：
`{kind: once, at}`、`{kind: interval, anchor, seconds}`、`{kind: cron, expression}`，加 IANA 时区（默认 Asia/Shanghai）。

```yaml
schema_version: runtime-manifest.v1
jobs:
  - id: daily-report            # 项目内唯一
    type: script                # script | agent | service_restart
    entry: scripts/daily_report.py    # script：脚本路径；agent：agents/<名称>；service_restart：服务 id
    args: []
    schedule: {kind: cron, expression: "0 8 * * *"}
    timezone: Asia/Shanghai
    timeout_seconds: 600
    retries: 2
    notify: supervisor          # 失败告警发到本项目 Supervisor 频道
services:
  - id: price-listener
    command: python scripts/listener.py
    restart_schedule: {kind: cron, expression: "0 4 1 * *"}   # 可选：每月重启
```

### 6.2 闭环

| 环节 | 做什么 | 执行者 |
|---|---|---|
| 需求 | 需求文档写"运行要求"：频率、失败通知、能否补跑 | PM |
| 设计 | "运行方式"一节写作业表和服务表，字段与 manifest 一一对应 | PM |
| 实施 | 候选文件 `jobs.yaml` 加脚本、agent 角色和技能、测试 | PM |
| 验证 | 字段齐全、时间规则合法、脚本和 agent 文件存在、ID 唯一 | Kernel |
| 发布批准 | 批准发布即批准登记 | 用户 |
| 登记 | 发布后同步到 `core.jobs`（新增启用、修改更新、删除退役）；服务生成 supervisord 程序配置并 `update`（只影响该程序） | Kernel |
| 运行 | 到点建 `job` 运行（作业 ID + 计划时间去重）→ 执行 → 记录 → 失败重试 → 用尽告警；错过的时间点只补最近一次；手动运行不改变下次时间 | Kernel |
| 运维 | `/jobs` 查看、暂停、恢复、立即运行；维护节拍检查服务状态，异常告警 | 用户 / Kernel |
| 复盘 | 作业统计进入周度复盘；改进成为新任务，修改 manifest 后重新发布，回到"登记" | PM、Supervisor |

- Kernel 只对单个程序执行 `update` / `restart`，代码中禁止 `reload` 或重启 supervisord 本身。
- 不在 AI 容器内的现有 docker-compose 服务（equity-volume-alert 等）不在本期范围。
- 配套：按 manifest 重写 `assistant-workflow-design`、`assistant-agent-design`；实施技能要求交付清单包含 manifest、测试和回退方式。

**S3 完成记录（2026-10-16，已部署 0.39.1，迁移 0006）**：
- 代码：
  - `core/runtime.py`：`jobs.yaml` 的格式校验（作业 script / agent / service_restart，服务及其定期重启；时间规则复用
    `schedule_time.py`）和 supervisord 控制（只写或删 `proj-<项目>-<服务>` 程序文件，然后只对该程序执行 `update`；代码中拒绝
    `reload`、`shutdown` 和 `all`）；
  - `core/jobs.py`：发布后的 `jobs.yaml` 同步到 `core.jobs`（新增、修改、退役，文件有误时在 Supervisor 频道提醒一次且不做任何改动）、
    到期作业排队（错过的时间点只跑一次；上一次未结束则跳过并记事件）、`/jobs` 的查看 / 暂停 / 恢复 / 立即运行、服务健康检查
    （离开 RUNNING 和恢复时各提醒一次）；
  - 状态机：作业运行的入队（`machine.enqueue_job`，遵守"只有 machine/kernel 写 core.runs"的原则）、成功记录、agent 作业结果发到频道、
    重试用尽后告警；
  - 执行器 `executors/job.py`：脚本在项目目录运行，环境只有 PATH、HOME、TZ 和作业信息，拿不到 Kernel 的数据库和密钥，超时整组杀掉，
    日志在 `runs/jobs/<id>/`；agent 作业加载项目内 `agents/<名称>/` 和 load.roles 含该名称的技能，只读运行一轮；
  - 验证执行器在发布前检查候选 `jobs.yaml`（入口文件按"本次交付 + 现有文件"判断）；复盘材料加入作业成功和失败次数；
  - 只有生产 dispatcher 运行这一切（`run_jobs=True`，`CORE_RUN_JOBS=0` 可关闭）；测试环境 dispatcher 已显式关闭。
- 合同 `runtime-manifest.md`（设计、实施、Review 阶段注入 PM）；技能：workflow-design、agent-design 重写（2.0.1），
  solution-design、implementation、task-continuity（设计文档第 9 节"运行方式"）、requirements（"运行要求"）、operation 更新；
  三个项目的副本已更新。
- 测试：全部 179 项通过（新增 `tests/core/test_runtime.py` 8 项）。
- 真实环境验证（kernel-v12-validation，直接写入 `jobs.yaml`，未经任务流程）：
  - 每分钟一次的脚本作业按时运行，环境中没有数据库变量；
  - 服务注册为 `proj-kernel-v12-validation-heartbeat` 并处于 RUNNING；
  - docling、kernel 进程不受影响；
  - 删除 `jobs.yaml` 后作业和服务都已退役，程序被移除；
  - 两条运行时通知都送达了 Discord；
  - 测试文件已清理。
- 未覆盖：一个真实任务从需求到发布 `jobs.yaml` 的完整流程（技能和合同已就绪，等真实需求时验证）；agent 作业只有单元测试，没有真实模型运行。

## 7. 修订记录

| 日期 | 原结论 | 修订 | 原因 |
|---|---|---|---|
| 10-16 | S2 中 assistant-solution-design 增加"运行方式"一节 | 移到 S3，与 `runtime-manifest.v1` 一起定义 | 运行时上线前写进技能，PM 会产出没有组件读取的声明 |
| 10-16 | 运行声明放在 `runtime/manifest.yaml` | 放在项目根目录的 `jobs.yaml` | vault 的 `runtime/` 目录存放 Kernel 内部数据（Codex 登录、凭据、快照），目录树工具也排除它；与 `/jobs` 命令同名，便于记忆 |
| 10-16 | R3 章程使用新文档类型 `project_charter` | 章程就是创建项目任务的 task_contract | 复用草稿编辑和"批准绑定 sha"的机制，代码更少；格式差异由合同 `pm-bootstrap.md` 规定 |
| 10-16 | 新项目可选 git_delivery / git-repo | R4a 上线前只接受 vault-files | 冒烟中 PM 选了尚未实现的代码交付，创建出来会名不副实 |
| 10-16 | 会话轮换按上下文占比（60%）判断 | 按会话已运行的回合数（8 轮） | Codex 报告的 input_tokens 是一轮内多次请求的累计值，不等于上下文长度；自动压缩继续兜底防止超限 |
