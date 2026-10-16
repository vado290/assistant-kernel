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

## 6. S3 通用运行时（定时作业与长驻服务）

### 6.1 唯一来源：`runtime/manifest.yaml`（格式 `runtime-manifest.v1`）
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
| 实施 | 候选文件 `runtime/manifest.yaml` 加脚本、agent 角色和技能、测试 | PM |
| 验证 | 字段齐全、时间规则合法、脚本和 agent 文件存在、ID 唯一 | Kernel |
| 发布批准 | 批准发布即批准登记 | 用户 |
| 登记 | 发布后同步到 `core.jobs`（新增启用、修改更新、删除退役）；服务生成 supervisord 程序配置并 `update`（只影响该程序） | Kernel |
| 运行 | 到点建 `job` 运行（作业 ID + 计划时间去重）→ 执行 → 记录 → 失败重试 → 用尽告警；错过的时间点只补最近一次；手动运行不改变下次时间 | Kernel |
| 运维 | `/jobs` 查看、暂停、恢复、立即运行；维护节拍检查服务状态，异常告警 | 用户 / Kernel |
| 复盘 | 作业统计进入周度复盘；改进成为新任务，修改 manifest 后重新发布，回到"登记" | PM、Supervisor |

- Kernel 只对单个程序执行 `update` / `restart`，代码中禁止 `reload` 或重启 supervisord 本身。
- 不在 AI 容器内的现有 docker-compose 服务（equity-volume-alert 等）不在本期范围。
- 配套：按 manifest 重写 `assistant-workflow-design`、`assistant-agent-design`；实施技能要求交付清单包含 manifest、测试和回退方式。

## 7. 修订记录

| 日期 | 原结论 | 修订 | 原因 |
|---|---|---|---|
