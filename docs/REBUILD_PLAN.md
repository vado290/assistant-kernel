# Kernel 控制核心重建方案（方案 B）

状态：**总体方案已确认（2026-10-15）**，阶段 0 细节待确认　｜　起草：2026-10-15　｜　基线源码：0.32.3 / core5.v11

本文是可落地的实施方案。每个阶段写明目标、内容、交付物、退出标准和"不做什么"。后续阶段在细化设计时如果要推翻前面阶段的内容，必须在第 10 节"修订记录"登记，并经用户确认后再改。

---

## 0. 目标与设计原则

**目标**：用一个小而自洽的控制核心替换现有的约 1.5 万行控制代码（`repositories/core.py` 6727 行、`run_processor.py`、`goal_supervisor.py`、`supervisor_processor.py`、`question_groups.py`、`prompt_protocol.py`、`task_queue.py` 等），使任务从创建到结束的整条链路在多项目下闭环，并且每个环节只在一个地方实现。

保留并复用的部分（只调整接口，不重写）：
- Codex 调用层：`harness/`
- 文档存储：`document_runtime`
- 授权目录：`workspace_scope`、`project_grants`
- 候选、验证与发布：`capability_bundle`、`validation_runner`
- 项目地图与变更账本：`semantic_map*`、`project_changes`
- 凭据：`project_credentials`
- 系统配置：`system_config`
- 项目 Agent 包：`published_agents`、`agent_files`
- 角色与技能模板：`prompts/v12`
- MCP 工具服务：`mcp_server`
- Python 执行进程管理：`worker/python_process`

**七条原则**（每条都有对应的测试来守住）：

| # | 原则 | 如何保证 |
|---|---|---|
| P1 | 每类事实只有一个主人：任务归 `tasks`，执行归 `runs`，待用户处理的卡片归 `interactions`，原始消息和发件箱归 `messages` | 表结构 |
| P2 | 状态只能经过所属服务修改，其他模块不允许直接写 `UPDATE tasks/runs/interactions` | 测试扫描源码，发现违规即失败 |
| P3 | 模型输出永远不直接写控制字段，只由 Kernel 校验后触发状态转移 | 状态机是唯一入口 |
| P4 | 每次转移在一个事务内完成，并用版本号做并发检查（`state_revision`、`revision`） | 服务层统一实现 |
| P5 | 任何外部来源的写入都带幂等键 | 唯一索引 |
| P6 | 能领取就一定能执行：所有执行条件都写进同一条领取 SQL，不再"先领取再拒绝" | `RunService.claim` |
| P7 | 每个活动任务在任一时刻**只有一个"游标"**：要么一个未结束的控制类执行，要么一张待处理卡片里属于它的条目 | 一致性检查和测试 |

---

## 1. 总体架构

```
          Discord / 飞书 / API
                 │  (只负责收消息、送消息)
          ┌──────▼───────┐
          │ IngressService│  入库(幂等) → 分类 → 交给下面四个入口之一
          └──┬───┬───┬───┘
   卡片动作  │   │   │ 命令(/pending /cancel /status ...)
 Interaction │   │   └──────────► CommandService
   Service ◄─┘   │ PM频道文字 ──► TaskService.user_input
                 │ Supervisor频道文字 ► SupervisorService.consult
                 ▼
     TaskStateMachine（唯一能改任务状态的地方，内含阶段转移表）
        │  入队执行                    │ 打开/升级卡片
        ▼                              ▼
     RunService ── claim ──► Dispatcher（按需并发）
                                  │
          PMExecutor / SupervisorExecutor / ValidationExecutor /
          PublishExecutor / PythonExecutor / BootstrapExecutor
                                  │ 只返回结构化结果
                                  ▼
                 TaskStateMachine.on_run_result（在同一事务内结算）
```

执行器（Executor）从不修改任务状态，只返回结果；结算统一由状态机完成。

---

## 2. 数据模型（新 schema `core6`）

### 2.1 新表

```sql
-- 项目：用于行锁和外键，同时保存项目级运营基线（原 kernel_meta 的 project-runtime:*）
projects(
  project_id text PK, status text,              -- active|archived
  kind text,                                    -- kernel|business
  settings jsonb,                               -- 提醒间隔、模型并发阀门等
  operational_baseline jsonb,                   -- 最近一次已发布的需求/设计引用
  created_at, updated_at)

-- 业务任务
tasks(
  task_id uuid PK, project_id FK,
  kind text,        -- delivery|operation|evolution|bootstrap
  origin text,      -- user|supervisor|schedule|handoff|evolution|system
  priority text,    -- normal|urgent
  goal text, source_message_id FK messages, parent_task_id FK tasks NULL,
  stage text,       -- requirements|design|build|review|release|operate
  state text,       -- queued|pm|supervising|awaiting_user|validating|publishing|suspended|completed|cancelled|failed
  state_revision int, turn_count int, max_turns int,
  documents jsonb,  -- {task_contract, solution_design, implementation_report, review_report, last_turn, ...}，每项含 ref 和 sha256
  approvals jsonb,  -- {requirements:{doc_sha,message_id,at}, design:{doc_sha,based_on_requirements_sha,...}, release:{bundle_sha,review_sha,...}}
  pm_session jsonb, -- {stage, native_session_id}，用于同一阶段内续接 PM 会话
  cancel_requested_at, closed_at, close_reason,
  idempotency_key text UNIQUE, created_at, updated_at)
CREATE UNIQUE INDEX one_active_task ON tasks(project_id)
  WHERE state IN ('pm','supervising','awaiting_user','validating','publishing');

-- 执行记录：每个 PM 回合、每次 Supervisor 评估都是新的一行，重试只增加 attempt
runs(
  run_id uuid PK, project_id FK, task_id FK NULL,
  run_type text,   -- pm_turn|supervisor|validation|publish|python|bootstrap
  purpose text,    -- supervisor 用：evaluate|review_input|consult|fallback
  status text,     -- queued|running|succeeded|failed|cancelled|reconciling
  attempt int, max_attempts int, available_at,
  input jsonb,     -- 入队时冻结的输入引用（答案、草稿、补充消息 id 列表等）
  base jsonb,      -- 入队时的 task.state_revision / interaction.revision，结算时用来判断是否过期
  result jsonb, error jsonb, effects jsonb,  -- effects：已发生的外部效果回执
  mutex_key text, resource_keys text[],
  worker_id, claim_token, lease_until, process_pid, process_start_ticks,
  idempotency_key text UNIQUE, created_at, started_at, ended_at)

-- 交互卡片：每个项目同一时刻只有一张待处理卡片，由若干"条目"组成
interactions(
  interaction_id uuid PK, project_id FK,
  revision int, status text,          -- open|submitted|superseded|cancelled
  items jsonb,       -- [{item_id, target:{kind:task|consultation|service, id}, kind:questions|next_step|decision|credential,
                     --   origin:pm|supervisor|kernel|runtime, content, required, deliver_to:pm|supervisor|runtime|kernel}]
  drafts jsonb,      -- {item_id:{question_id: value}}，升级为新版本时按 id 保留
  supplements jsonb, -- 卡片打开期间收到的补充消息引用（按条目归属）
  display jsonb,     -- 各渠道已发出的消息 id，用于编辑或撤下
  remind jsonb, created_at, submitted_at)
CREATE UNIQUE INDEX one_open_card ON interactions(project_id) WHERE status='open';

-- 事件流水：只追加。用于审计，也是第 6 阶段自我进化的数据源
task_events(event_id, project_id, task_id NULL, run_id NULL, interaction_id NULL,
            kind text, payload jsonb, created_at)
```

### 2.2 保留的表及调整

| 表 | 处理 |
|---|---|
| `messages` | 保留，作为原始对话记录和发件箱。新增列 `task_id`、`consumed_by_run_id`、`interaction_id`，取代散落在 `content_json` 里的 `task_input_status` 等标记 |
| `sessions`、`model_invocations` | 保留，外键改为指向新的 `runs` |
| `project_grants`（7 行，4 行有效） | 迁移保留。对已归档 runs/messages 的引用置空，原 id 写入 `metadata` |
| `project_file_changes`（264 行） | 保留；`legacy_message_id` 若指向已归档消息则置空 |
| `project_runtime_units`、`project_schedules`、`schedule_ticks`、`service_instances` | 当前都是 0 行，保留结构。外键改指新 runs，第 6 阶段再按需调整 |
| `kernel_meta` | 只保留系统配置、项目 Agent 包、公共背景等键；`project-runtime:*` 移入 `projects`；`session-boundary:*`、`v12-*`、`upgrade-*` 一律删除 |

### 2.3 旧数据处理
当前 `runs` 142 行、`messages` 417 行。整体移到 `archive_core5` schema，**不删除**；新核心不读取这些历史。当前 kernel 的 v1.2 任务在切换时取消。

### 2.4 批准有效性靠推导，不靠手动清空
批准是否有效完全由当前文档推导出来，不再存在"手动把批准置空"的代码：

- `approvals.requirements` 有效 ⇔ 它记录的 `doc_sha` 等于当前 `documents.task_contract.sha256`
- `approvals.design` 有效 ⇔ `doc_sha` 等于当前设计文档的 sha，**并且**它记录的 `based_on_requirements_sha` 等于当前需求文档的 sha
- `approvals.release` 有效 ⇔ 发布包 sha 和 Review 报告 sha 都与当前一致

这样，需求一旦改版，设计批准和发布批准会自动失效。现有代码里几十处 `requirements_approval=None` 之类的补丁就不再需要。

---

## 3. 任务状态机

### 3.1 状态与"游标"的一一对应（原则 P7）

| state | 唯一游标 | 含义 |
|---|---|---|
| queued | 无 | 已创建，等待激活 |
| pm | 1 个未结束的 `pm_turn` | PM 在执行或排队 |
| supervising | 1 个未结束的 `supervisor(evaluate 或 fallback)` | Supervisor 在评估或组装下一轮 |
| awaiting_user | 当前卡片中有属于本任务的条目 | 等待用户 |
| validating | 1 个未结束的 `validation` | Kernel 验证候选文件 |
| publishing | 1 个未结束的 `publish` | 发布中，或发布结果不确定需要对账 |
| suspended | 无 | 被紧急任务挂起 |
| completed / cancelled / failed | 无 | 终态 |

在 `awaiting_user` 时，允许额外存在一个 `supervisor(review_input)` 执行。它只用来判断"新输入是否需要升级卡片"，结算时只能升级卡片，或什么都不做。

### 3.2 事件与转移表

| 事件 | 前置状态 | 结果 |
|---|---|---|
| E1 创建 `TaskService.create` | — | queued；如果项目当前没有活动任务，在同一事务内执行 E2 |
| E2 激活 | queued | stage 取初始值（delivery 为 requirements，operation 和 evolution 为 operate），进入 supervising（目的：start） |
| E3 Supervisor 结果：`ask_user` | supervising | awaiting_user，打开或升级卡片，加入问题条目（提交后回到 Supervisor） |
| E3 `propose_next`（草稿 + 目标阶段） | supervising | awaiting_user，加入 next_step 条目；如果要跨阶段，同时加入 decision 条目（批准文档 + 进入下一阶段，合在同一个条目里） |
| E3 `relay_pm_questions`（PM 提问后的评估） | supervising | awaiting_user，问题条目 origin=pm、deliver_to=pm，**需通过覆盖率校验**（见 4.4） |
| E3 `propose_complete` / `propose_new_task` / `propose_cancel` | supervising | awaiting_user，加入 decision 条目 |
| E3 Supervisor 重试耗尽 | supervising | awaiting_user，加入 Kernel 兜底条目：把 PM 原问题原样展示，或提供"重试 / 直接按原指令继续 / 停止" |
| E4 卡片提交：问题条目 deliver_to=pm | awaiting_user | pm（新建 pm_turn，输入为原问题、答案和补充消息，续接 PM 会话） |
| E4 问题条目 deliver_to=supervisor | awaiting_user | supervising（evaluate，输入为答案） |
| E4 next_step「执行」 | awaiting_user | 检查阶段转移表和门槛 → 必要时更新 stage → pm |
| E4 next_step「修改」（附文字） | awaiting_user | supervising（让 Supervisor 重新起草） |
| E4 decision「批准」 | awaiting_user | 写入 approvals，然后执行同条目绑定的下一步：发布批准进入 publishing，完成确认进入 completed |
| E4 decision「拒绝 / 取消任务」 | awaiting_user | 拒绝：supervising（Supervisor 提出返工）；取消任务：E10 |
| E5 PM 结果 `needs_input` | pm | supervising（目的：relay_pm_questions） |
| E5 PM 结果 `completed` 或带阶段报告 | pm | 更新 documents → supervising |
| E5 PM 结果 `ready_for_validation`（build 阶段） | pm | validating |
| E5 PM 结果 `return_to_*` | pm | supervising（Supervisor 提出回退建议，用户确认后才真正改 stage） |
| E5 PM 重试耗尽 | pm | awaiting_user，加入 decision 条目"重试 / 修改指令 / 停止" |
| E6 验证结果（通过或失败） | validating | supervising（Supervisor 提出进入 Review 或修复方案） |
| E7 发布成功 | publishing | completed；更新 `projects.operational_baseline`；在同一事务内激活下一个任务 |
| E7 发布结果不确定 | publishing | 保持 publishing，执行状态为 reconciling，加入"对账"decision 条目 |
| E8 PM 频道收到新文字 | 见 5.2 | 不改变状态，只挂为补充；在 awaiting_user 时触发 review_input |
| E10 取消 | 任一非终态 | cancelled：未结束的控制类执行设置取消标记，属于本任务的卡片条目移除，在同一事务内激活下一个任务 |
| E11 挂起（紧急任务获批） | 无执行在运行时立即生效；有执行在运行时等它结算后生效 | suspended：属于本任务的卡片条目移除，激活紧急任务 |
| E12 恢复 | suspended | supervising（目的：resume），由 Supervisor 重新组织卡片，不恢复旧卡片 |
| E13 回合数超过上限 | 结算时 | awaiting_user，加入"继续 / 停止"decision 条目 |

**为什么闭环**：
- 每个非终态都有"产生下一状态"的事件。
- 每个执行都有成功、失败、重试耗尽三条出口，并且都映射到上表的某一行。
- 每张卡片条目都有提交或取消两条出口。
- 不存在没有游标却停在活动状态的任务；万一出现，由一致性检查补上一个游标（见 5.5）。

### 3.3 阶段转移表（Kernel 规定，Supervisor 只能在表内选择）

| 从 | 可以去 | 门槛（由 Kernel 校验） |
|---|---|---|
| requirements | design | 需求文档存在，且需求批准有效 |
| requirements | completed | 交付义务仅为 research，且研究成果已提交 |
| design | build | 设计批准有效（含基于当前需求的校验） |
| design | requirements | 无（回退） |
| build | review | 最近一次验证通过，且发布包 sha 与验证结果一致 |
| build | design、requirements | 无（回退） |
| review | release | Review 结论为 pass，且覆盖当前发布包和设计；设计中声明了发布目标 |
| review | completed | Review 通过，且设计中没有发布目标 |
| review | build、design、requirements | 无（回退） |
| release | （通过 E7 进入）completed | 发布批准有效 |
| operate | operate、completed | operation 和 evolution 类任务专用 |

回退时**不需要清空任何字段**。文档改版后，相关批准按 2.4 自动失效。

---

## 4. 交互卡片：每个项目一张卡

### 4.1 规则
1. 每个项目同一时刻**最多一张待处理卡片**（数据库唯一索引保证）。卡片里可以有多个条目，例如当前任务的问题，加上运行中业务程序的提问。
2. 卡片只出现在 Supervisor 频道。PM 频道只发一条指引，例如"有 N 项待你在 Supervisor 频道处理（链接）"。
3. 任何修改都生成新版本（revision+1）：旧消息被编辑为"已更新，见最新卡片"并撤下按钮，已填写的草稿按问题 id 带入新版本。
4. 提交时，卡片上所有填写完整的条目各自送往目标；未填完的条目留在新版本里。
5. 点击旧版本卡片只回复"已更新"，**永远不会创建任务或执行**。
6. 卡片不会自动过期，在 1 小时、24 小时各提醒一次（可配置）；`/pending` 可随时查看。

### 4.2 条目类型

| 条目 | 展示 | 提交后 |
|---|---|---|
| questions | 分页显示、给出推荐选项、每题都可以自由填写、最后一页可写补充说明 | 按 deliver_to 送到 PM、Supervisor 或业务程序 |
| next_step | 下一轮要做什么的摘要，完整指令放在附件里 | 执行 / 修改（附文字）/ 停止 |
| decision | 要批准的对象（文档或发布包的 sha）、影响说明，可以和 next_step 合并成一个条目 | 批准 / 拒绝 / 取消任务 |
| credential | 一个按钮，点击后弹出输入框 | 凭据由 Kernel 直接写入凭据库，模型和日志都看不到，只记录"已提供" |

### 4.3 新输入与卡片的关系

| 情况 | 处理 |
|---|---|
| 卡片待处理时，用户在 PM 频道发文字 | 挂到对应任务条目的补充区（卡片上可见）。触发一次 `supervisor(review_input)`，同一项目 60 秒内多条只触发一次。Supervisor 判断有方向性变化就升级卡片，并保留草稿；否则卡片不变。提交时补充内容一并送出 |
| 卡片待处理时，用户在 Supervisor 频道发文字 | 走 `supervisor(consult)`：回答用户，必要时升级卡片或加入"创建新任务？"条目 |
| PM 正在执行时用户发文字 | 记为该任务的补充，下一轮 PM 读取（写入 `messages.task_id`，读取后记录 `consumed_by_run_id`） |
| Supervisor 结算时，卡片版本或任务版本已变 | 本次结果作废，按最新状态重新排一次 Supervisor（合并去重） |
| 用户提交卡片时，review_input 正在运行 | 以用户提交为准；review_input 结算时版本不符，结果直接丢弃 |

同一项目同时最多运行一个 Supervisor 执行（锁键 `supervisor:{project}`）。所有对卡片的修改因此天然串行，版本冲突只可能来自用户提交，而用户提交总是优先。

### 4.4 PM 问题的覆盖率校验
Supervisor 转述 PM 的问题时，必须给出映射：PM 的每个**必答**问题 id，要么对应卡片上的某个问题，要么附上"建议答案 + 依据"（以预填草稿的形式展示，仍需用户提交确认）。Kernel 校验映射完整，不完整则使用 Kernel 兜底卡（PM 原问题原样展示）。PM 原问题始终作为条目附件保留。

### 4.5 答案回到 PM 会话
PM 回合的会话策略：同一任务、同一阶段内续接原会话；阶段变化时新开会话；Review 阶段总是新开（保证独立性）。每轮开始时都读取**当前**已发布的项目 Agent 配置，版本变化从下一轮起生效，不做冻结或失效处理（用户决定 #2）。所用的配置版本记入 `runs.result`，只用于审计。

---

## 5. 执行与调度

### 5.1 领取（一条 SQL 包含全部条件）
```
status='queued' AND available_at<=now()
AND 所属任务未取消、不在终态、不在 suspended（task_id 为空的执行除外）
AND base.state_revision = 任务当前 state_revision        -- 已过期的执行直接标记 cancelled
AND 同一任务没有正在运行的控制类执行（pm_turn/supervisor/validation/publish）
AND 锁键（mutex_key）没有被占用：pm:{project}、supervisor:{project}、publish:{project}
AND resource_keys 与正在运行的执行没有重叠（只对 python 和 publish 生效）
AND （可选）模型并发阀门：正在运行的 pm_turn+supervisor 数量 < 上限（默认关闭）
AND python 执行的 CPU/内存准入（沿用 runtime_admission）
ORDER BY 紧急任务优先, created_at  FOR UPDATE SKIP LOCKED LIMIT 1
```
定时任务许可（`authorize_claim`）改为在入队时检查，不再放在领取时，也不再在领取过程中取消执行。

### 5.2 Dispatcher（按需并发）
- 单个进程循环执行：能领取就领取，每领到一个就启动一个并发任务，**不限制数量**。可配置的模型并发阀门默认关闭（用户决定 #3）。
- 停止时：先停止领取，再等待手头的执行完成（有超时）；超时仍未完成的，留给租约过期后的恢复流程处理。
- 执行失败分两类：
  - 基础设施故障（进程崩溃、超时）：执行本身重试（attempt+1，带退避），重试耗尽后交给状态机（E3 / E5）；
  - 业务结果（模型返回 failed）：直接交给状态机。

### 5.3 执行器
| 执行器 | 来源 | 产出 |
|---|---|---|
| PMExecutor | `run_processor` 中 v1.2 路径的上下文组装、调用、结果解析和文件效果（去掉旧分支和一致性约束） | `agent-result.v1` |
| SupervisorExecutor | `supervisor_v12` 和 `goal_supervisor` 的模型调用部分 | 新的 `supervisor-decision.v2`：ask_user / propose_next / relay_pm_questions / propose_complete / propose_new_task / propose_cancel / review_input(keep\|upgrade) / consult_reply |
| ValidationExecutor | `phase_control_v12.validate_build` 和 `validation_runner` | 验证回执 |
| PublishExecutor | `capability_bundle.publish` | 发布回执，或标记为不确定 |
| PythonExecutor | `worker/main.execute_python` | 执行回执；结束后作为补充消息挂到所属任务 |
| BootstrapExecutor（第 5 阶段） | 新写 | 项目创建回执 |

### 5.4 外部效果
被作废或被取消的回合，如果已经产生外部效果（候选文件、登记的定时任务等），**照样记录到 `runs.effects`，并在下一张卡片或通知中列出，不回滚，也不重放**。正式文件只能通过 PublishExecutor 写入。

### 5.5 control 进程（原 maintenance 和 scheduler）
只负责四件事：
1. 回收过期租约：pm_turn 和 supervisor 重新排队；publish 和 python 转入对账；
2. 卡片提醒；
3. 定时触发（第 6 阶段的每周进化）；
4. 一致性检查：发现"活动任务没有游标"时，按状态补入对应的执行（幂等），并记录 `task_events`。

**不再承担任务激活**：激活由状态机在同一事务内完成。

---

## 6. 入口路由（取代 1000 行的 `ingest_message`）

```
IngressService.receive(event):
  1. 幂等写入 messages（渠道 + 外部事件 id）
  2. 分类：
     - 卡片按钮或弹窗  → InteractionService.act(interaction_id, revision, item_id, action, values)
     - 回复卡片的文字  → 若回复的是当前版本，作为该卡片的文字输入（显式写 "Qx: 答案" 或作为补充），否则按普通文字处理
     - 斜杠命令        → CommandService
     - PM 频道文字     → TaskService.user_input(project, message)
                          有活动任务：作为补充（规则见 4.3）；没有：E1 创建任务
     - Supervisor 频道文字 → SupervisorService.consult(project, message)，排一个 supervisor(consult)
  3. 只在同一事务内调用一个服务方法，然后返回
```
跨项目交接、MCP 工具 `queue_project_task`、运营报告、每周进化、项目创建，全部改为调用 `TaskService.create`，没有任何特殊入口。

---

## 7. 进程与部署

### 7.1 进程（4 个 Kernel 程序，加上不变的 docling）
| 程序 | 内容 | 依赖 |
|---|---|---|
| kernel-api | HTTP API，同一应用内挂载 HTTP MCP | PostgreSQL |
| kernel-dispatcher | 领取与执行，按需并发 | PostgreSQL |
| kernel-control | 租约回收、提醒、定时、一致性检查 | PostgreSQL |
| kernel-gateway | 只接 Discord，只负责收消息和送发件箱 | PostgreSQL |

飞书不再参与 Kernel 的对话和业务逻辑（用户决定，10-15）。`kernel-feishu` 进程在阶段 4 下线；飞书的凭据和底层客户端保留，供以后做 webhook、多维表格等独立功能时使用。

- 不依赖启动顺序：每个进程启动时自行等待 PostgreSQL 就绪（退避重试），并写入心跳；`/health/ready` 汇总所有进程心跳。
- supervisord 只负责拉起和自动重启。
- 重启时 dispatcher 先停止领取、等手头执行完成（`stopwaitsecs` 与最长回合时长对齐）。

### 7.2 test 和 prod 两套环境（取代按版本号建 venv）
- 固定两个目录：`kernel-venvs/test` 和 `kernel-venvs/prod`。
- **不能用改名来切换**：venv 中脚本的 shebang 写死了绝对路径，改名后会失效。所以推广到 prod 的做法是：把**同一个 wheel** 按锁定的依赖重新安装到 prod venv。
- 保留最近 2 个 wheel 用于回退（每个只有几 MB）。
- 测试数据库：沿用现有测试的做法，每次测试在同一个 PostgreSQL 中临时建库、跑完即删（`rag_service` 用户有建库权限），不常设 `assistant_test` 库。
- 部署流程：
  1. 构建 wheel；
  2. 安装到 test；
  3. 运行单元测试和端到端冒烟测试（连 test 库）；
  4. prod 停止领取并等待执行结束；
  5. 迁移 prod 数据库；
  6. 把 wheel 安装到 prod；
  7. 启动；
  8. 健康检查失败就用上一个 wheel 回退。
- `kernel-current.txt` 不再需要。

---

## 8. 分阶段实施计划

> 顺序调整说明：不再"先删旧协议、再重写"。新核心在新目录 `assistant_kernel/core/` 中并行开发，等到切换时一次性删除整批旧模块，避免对马上要被替换的代码做精细删改。

### 阶段 0：安全网（小）

本阶段**不改变任何业务行为**。唯一会碰到线上的动作，是把正在运行的 0.32.3 换到固定的 `prod` 目录（代码完全相同）。

| 步骤 | 内容 | 产出文件 | 验证 |
|---|---|---|---|
| 0.1 | 旧 venv 移入回收站，符号链接写入清单 | `assistant-data/backups/venv-symlinks-20261015.tsv` | 只剩 0.32.3 |
| 0.2 | 新部署脚本，取代 `Deploy-AssistantKernel.ps1`（它只接受 core5.v6/v7，已与生产的 core5.v11 不符）、`Deploy-SchedulerRelease.ps1`、`deploy_*_release.py`、`adopt_*.py`。旧脚本在阶段 4 移入 `scripts/archive` | `scripts/deploy.ps1` | 见 0.4 |
| 0.3 | 迁移执行器：按序号执行 `migrations/` 下的 SQL，每个脚本只执行一次（记录在 `kernel_meta.schema_migrations`），不需要版本号判断。阶段 0 暂无新迁移，只建立机制 | `scripts/migrate.py` | 对临时库连跑两次，第二次无动作 |
| 0.4 | 部署流程：①容器内构建 wheel，放入 `runtime/wheels/`（保留 2 个）②同步安装到 `kernel-venvs/test` ③用 test 环境跑测试（临时库）④prod 停止领取、等待执行结束 ⑤`migrate.py` ⑥同一个 wheel 安装到 `kernel-venvs/prod` ⑦重启 ⑧检查 `/health/ready` 和 `/version`；失败就装回上一个 wheel 并重启 | `scripts/deploy.ps1` | 用当前源码完整跑一次 |
| 0.5 | 线上切换：`run-kernel-service.sh` 改为固定使用 `kernel-venvs/prod`，去掉对 `kernel-current.txt` 的依赖；首次部署后把 `kernel-venvs/0.32.3` 移入回收站 | `scripts/run-kernel-service.sh` | 所有服务就绪，版本一致 |
| 0.6 | 新核心的测试框架：`tests/core/conftest.py`（每次测试临时建库、临时数据目录）；`FakeHarness`（实现 `HarnessAdapter`，按"执行类型、目的、阶段、第几轮"返回预设结果）；`FakeGateway`（读取发件箱、模拟按钮点击）；场景辅助函数：`send_pm_text`、`send_supervisor_text`、`drain()`（运行到没有可执行的为止）、`card()`、`click()`、`assert_invariants()` | `tests/core/` | 框架自检测试通过 |
| 0.7 | 把第 9 节的 22 个场景写成测试骨架，逐步写明操作和断言，先标记为跳过（原因写"阶段 1/2/3 实现"） | `tests/core/test_scenarios.py` | 收集到 22 个，全部跳过 |
| 0.8 | 开发分支：新核心在 `rebuild/core6` 分支开发，阶段 4 切换时合并 | — | — |

- **退出标准**：`deploy.ps1` 用当前源码完成一次 test → prod 部署；线上服务运行在 `kernel-venvs/prod`；新测试框架可以运行。
- **完成记录（2026-10-15，分支 `rebuild/core6`）**：
  - `deploy.ps1` 已在线上完整执行两次，均成功：新测试 4 项通过、22 个场景跳过；迁移无待执行项。
  - 12 个 Kernel 进程全部运行在 `kernel-venvs/prod`，`/health/ready` 为 ready，版本 0.32.3 / core5.v11。
  - `kernel-venvs` 下只剩 `prod` 和 `test`；0.32.3 及之前全部 82 个版本都已在 Windows 回收站，符号链接清单见 `assistant-data/backups/venv-symlinks-20261015.tsv`；`runtime/wheels` 保留最新 2 个。
  - 已知情况：venv 放在 Windows 挂载盘上，首次建 venv、安装依赖约需 10–15 分钟；之后依赖不变时，一次部署约 2 分钟。
  - 回滚路径（装回上一个 wheel）已写好，但尚未实际演练。
  - `kernel-current.txt` 现在只被 `run-kernel-service.sh` 的过渡回退逻辑使用，阶段 4 一并删除。
- **不做**：不修改旧核心逻辑；暂不下线飞书进程（阶段 4 再处理）。旧版本在重建期间如果还在实际使用，再单独打"点击旧卡不建任务"的小补丁（可选）。

### 阶段 1：数据模型与状态机（纯控制逻辑，不调用模型）
- **内容**：`core6` 迁移脚本（只新增表，不动旧表）；`TaskService`、`RunService`（含领取 SQL）、`InteractionService`、`TaskStateMachine`（第 3 节两张表）、批准有效性推导、`task_events`、一致性检查函数；源码扫描测试（原则 P2）。
- **交付物**：`assistant_kernel/core/` 包，以及对应的单元测试和场景测试。场景测试直接注入执行结果，不经过模型。
- **退出标准**：第 9 节所有场景在"直接注入执行结果"的方式下全部通过；在多项目并发压力测试中，"每项目一个活动任务""每任务一个游标""每项目一张卡"三个不变式始终成立。
- **不做**：不接入渠道、不调用模型、不碰生产。
- **完成记录（2026-10-15，分支 `rebuild/core6`，未部署生产）**：
  - 代码：`core/sql/0001_core6.sql`（独立的 `core` schema）、`core/rules.py`（阶段、门槛、批准推导，纯函数）、`core/cards.py`（每项目一张卡）、`core/machine.py`（状态机，唯一修改任务状态的地方）、`core/kernel.py`（对外入口，每个方法一个事务；领取、完成、失败、租约回收）、`core/invariants.py`（三条不变式检查），共约 1100 行。
  - 测试：`tests/core` 共 25 项通过、5 项跳过。第 1 阶段的 15 个场景（S1–S6、S8–S17、S19）都在"注入执行结果"的方式下通过，并且每一步之后都自动检查三条不变式。另有 6 个 worker 并发领取的测试（同一项目只有一个能领到），以及源码扫描测试（只有所属模块能写各自的表）。
  - 跳过的 5 项：S7 紧急挂起、S18 业务程序提问（按 MVP 原则推迟，见修订记录）；S20 凭据（阶段 3）；S21 每周进化（阶段 6）；S22 配置切换（阶段 2）。
  - 打包后的 wheel 在 test 环境中也验证通过（`deploy.ps1 -TestOnly`）。

### 阶段 2：执行器与 Dispatcher
- **内容**：按 5.3 把执行器移植到 `core/executors/`；新写 `supervisor-decision.v2` 输出格式，并改写 Supervisor 的角色说明和技能（`supervisor.md`、`assistant-goal-supervisor`、`assistant-prompt-assembly`）；PM 的 `agent-result.v1` 只做必要调整；Dispatcher；把 MCP 工具（`scoped_tools`）改为通过 `runs.task_id` 定位任务，并删除已退役的工具（stage 相关、propose/create_project 的旧实现等）。
- **退出标准**：用假模型跑完全部场景；在 test 环境用**真实模型**跑通 S1 主线一次（需求 → 设计 → 实施 → Review → 发布）。
- **不做**：不接入渠道（用 API 或测试框架驱动）。

#### 阶段 2 细化设计（2026-10-15，待用户确认 D1–D4）

**现状**：今天一个 PM 回合要经过以下环节：
- 固定版本的 Agent 包和配置快照；
- 调用配置解析；
- 执行包确认和能力预检；
- 输入哈希头；
- 433 个 schema 定义组成的 `agent-result.v1` 输出合同；
- 结算到 `pm_turns_v12`。

`run_processor.process` 一个函数约 1800 行，其中 v1.2 路径只用到前约 700 行。

**新设计：一个执行器只做三件事——组装输入、调用模型、把输出翻译成第 1 阶段定义的结果字典。** 状态一律交给状态机，执行器不碰。

| # | 部件 | 做法 | 复用 |
|---|---|---|---|
| 2.1 | 执行器接口 | `async execute(run, ctx) -> dict`；基础设施故障直接抛异常，由 Dispatcher 调用 `fail()` 重试；模型输出格式不对则返回原文，由状态机判为 `InvalidResult` 重试 | — |
| 2.2 | Dispatcher | `assistant-kernel-dispatcher`：循环领取，每个执行起一个并发任务，按租约三分之一的间隔续租；每 30 秒回收一次过期租约；可配置模型并发阀门（默认关闭）；停机时先停止领取、等待手头执行完成。**阶段 4 之前不在生产启动** | — |
| 2.3 | 配置加载 | 每轮直接读取项目当前的 Agent 文件（`AgentFileLoader.load(project, agent, phase)`），**不再固定 Agent 包版本**；使用的配置 sha 记入 `runs.result` 备查（用户决定 #2，见 D2） | `agent_files` 加载器、`harness.resolve_model_profile` |
| 2.4 | PM 输入 | 角色说明（公共 + 当前阶段）+ 技能 + Kernel 生成的"本轮上下文"：目标、阶段、已生效文档的**正文**、本轮指令或问答结果、补充消息、上一轮摘要、输出要求 | 文档存储 `document_runtime` |
| 2.5 | PM 输出 `pm-turn.v2`（见 D1） | `{"outcome", "summary", "questions":[{id,text,required,options}], "document":{"kind","markdown","verdict"}}`。文档由 Kernel 写入 vault 并计算 sha。implementation_report 的 `bundle_sha` **由 Kernel 封存候选文件后填写**，不采信模型自报；Review 的 verdict 绑定到当前已验证的 bundle | `CandidateBundleService.seal(project, task_id)` |
| 2.6 | PM 会话 | 同一任务、同一阶段内续接 `pm_session.native_session_id`；阶段变化时新开 | harness 现有续接能力 |
| 2.7 | Supervisor 输入 | 角色和技能 + 上下文：任务（目标、阶段、状态）、文档正文、上一轮 PM 结果、两个频道最近 N 条用户消息、当前卡片、本次评估原因，以及 **Kernel 计算好的"当前可去的阶段和门槛状态"** | — |
| 2.8 | Supervisor 输出 `supervisor-decision.v2`（见 D1） | evaluate：`{"decision": ask_user/propose_next/relay_pm_questions/propose_complete/propose_cancel, "message", "questions"[...covers], "suggested", "instruction", "target_stage"}`；review_input：`{"decision":"keep"}` 或 `{"decision":"upgrade","item":{同上}}`；consult：`{"reply", "propose_new_task", "upgrade"}`。字段与第 1 阶段状态机现有的读取逻辑一一对应 | — |
| 2.9 | Supervisor 会话 | 每次新开会话、不续接（保留原 C17 决定）；只给只读工具；允许原生子代理 | `_codex_ephemeral` 中的模型调用部分 |
| 2.10 | 验证 | 封存候选文件，再用 `ValidationRunner.validate` 做结构检查，并运行项目配置的测试，产出 `{passed, report}`；bundle sha 由 Kernel 计算 | `candidate_bundle`、`validation_runner` |
| 2.11 | 发布 | `CapabilityBundleService.publish`（文件发布路径），输入是已批准发布的 bundle | `capability_bundle` |
| 2.12 | MCP 工具（见 D3） | `scoped_tools` 改为向 `core.runs` 核对租约；新增环境变量 `KERNEL_SCOPE_TASK_ID`，候选文件区按任务归属，让多轮实施的候选文件累积在一起 | `scoped_tools`、`workspace_scope` |
| 2.13 | 角色和技能文本 | 新写一套精简角色说明放在 `prompts/core/`（pm-common、各阶段 pm、supervisor），去掉 v1.2 协议术语，写明新的输出格式；技能沿用 `prompts/v12/skills` 中的需求、设计、实施、Review、Supervisor、Prompt 组装，只改其中涉及旧协议的段落 | `prompts/v12/skills` |

**子步骤**：
1. 2a：执行器接口、Dispatcher，以及用假模型跑通端到端；
2. 2b：PM 执行器和 PM 角色说明；
3. 2c：Supervisor 执行器和 Supervisor 角色说明；
4. 2d：验证与发布；
5. 2e：MCP 工具改接新核心；
6. 2f：用真实模型跑一次冒烟测试。

每一步都以第 9 节的场景用假模型重跑一遍作为退出标准；2f 用真实模型跑通 S1。

**需要用户确认**：
- **D1 精简输出格式**：新核心不再使用 `agent-result.v1`（433 个 schema 定义），改用上面两个精简格式；文档正文用 Markdown，供用户直接阅读和批准，需求和设计文档的结构要求写在技能里，不做 schema 强制。理由：格式越复杂，模型出错和重试越多。
- **D2 取消 Agent 包版本固定**：每轮读取项目当前的 Agent 文件。Agent 包的"采用"流程（`published_agents` 的 adoption）在新核心中不再使用；以后的技能和角色改进（阶段 6）直接走"候选 → 验证 → 发布到项目 agents 目录"。
- **D3 MVP 工具范围**：PM 可用以下工具：工作区授权和读取、候选文件写入/删除、项目地图、项目文档、最近历史、变更记录和差异、vault 读取、公共背景、火山引擎模型（如已配置）。**暂缓**：提交 Python 运行、定时任务、运行单元、运营动作、系统配置、凭据（阶段 3 随凭据卡片加入）、项目创建（阶段 5）、跨项目交接。PM 自测通过沙箱内直接运行命令完成（现状即如此）。
- **D4 真实模型冒烟测试的环境**：使用现有的 `kernel-v12-validation` 项目（已有 codex 登录和工作区）加一个临时数据库，在 test 环境中运行 Dispatcher；测试目标设为一个很小的需求，例如"生成一份 hello.md 说明文档"；卡片点击由测试脚本自动完成。不影响生产数据库，也不影响其他项目。

**进度记录（2026-10-15，分支 `rebuild/core6`）**：
- 2a–2e 已完成，`tests/core` 共 44 项通过、5 项跳过。PM、Supervisor、验证、发布四个执行器都已接入；其中一项测试用真实候选文件走完了"封存 → 验证 → 复用现有发布器写入 vault"的全过程。
- 新增代码：`core/dispatch.py`、`core/executors/`（common、pm、supervisor、delivery），以及 `prompts/core/` 下 11 个精简角色和输出说明。
- `scoped_tools.py` 增加了核心模式（`KERNEL_SCOPE_CORE=1`）：租约向 `core.runs` 核对，候选文件区归任务所有，`list_recent_history` 改读 `core.messages`；旧模式保持不变。
- 旧测试中有 3 项（`test_v12_mcp` 1 项、`test_mcp_unification` 2 项）在改动前的代码上同样失败，原因是找不到已不存在的 `validation-envs/p4-business-20261011` 目录，与本次改动无关。
- 2f 真实模型冒烟（`scripts/core_smoke.py`：临时数据库 + `kernel-v12-validation` 项目，卡片由自动驾驶脚本点击）：
  - 前两次失败在第一个 PM 回合：Codex 启动 Kernel 工具服务失败。原因有两个：一是 `managed.py` 仍按旧表核对租约，已修复；二是我的测试脚本用了相对路径的 PYTHONPATH，导致工具服务加载了旧的已安装代码，只影响测试脚本，已改为绝对路径。
  - **冒烟结果（第三次运行，10-15 13:36–13:43，约 7 分钟）：通过。** 依次为：需求 →（批准）设计 →（批准）实施 → Kernel 验证通过 → 独立 Review 结论 pass → 发布批准 → 发布 → 任务完成。共 11 个执行（5 次 Supervisor 评估、4 个 PM 回合、1 次验证、1 次发布），**全部第一次就成功**。`docs/hello.md` 已实际发布到 `kernel-v12-validation` 的 vault 中。临时数据库已删除。
  - 结论：S1 主线在真实模型下跑通，满足阶段 2 的退出标准。

**遗留问题存档**（按用户 10-15 的要求：无法顺利通过的不硬做，存档后讨论）：
| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| I1 | 旧测试 3 项因 `validation-envs/p4-business-20261011` 目录已不存在而失败（改动前同样失败） | 无，属于旧代码 | 阶段 4 删除旧代码时一并移除 |
| I2 | 项目自带的角色说明和技能文本仍含旧协议用语（如"提交 Requirements revision"），模型偶尔会照抄 | 小，不影响流程 | 阶段 5 生成项目文件时改用新模板；必要时先手工精简现有项目的 role.md |
| I3 | 发布后的项目地图和变更账本记录暂未接入 | 地图需要手动刷新 | 阶段 4 补上 |
| I4 | 自动驾驶脚本会自动批准所有卡片，不能代表真人交互 | 真实交互尚未验证 | 阶段 3 用 Discord 验证 |
| I5 | **阶段 3 的前置条件**：现在只有一个 Discord 机器人令牌和一个服务器。如果新核心的网关用同一个令牌再连一次 Discord，新旧两套会同时收到每条消息，造成重复处理 | 阻塞阶段 3 的真实 Discord 测试 | 需要用户决定（见 11 节待确认） |

**阶段 2 中的细节决定**（均属小事项，按 MVP 原则自行决定）：
- 模型输出不是合法 JSON 时，在同一会话里请模型更正一次；仍不合法则按格式错误重试整个回合。
- 实施回合会记录当时的工作区快照（`runs/<task>/resolved/execution-plan-build.json`），发布器据此核对目标目录的授权在实施之后没有被改动（复用现有安全检查）。
- 发布执行器出错时一律不自动重试，直接转为"对账"决策卡片。
- Supervisor 咨询必须给出回复，否则按格式错误重试。
- 发布后的项目地图和变更账本记录暂不接入（地图仍可手动刷新），阶段 4 切换时补上。

### 阶段 3：入口与交互卡片
- **内容**：`IngressService`、`CommandService`；Discord 的卡片渲染器（4 类条目、分页、版本、撤下旧卡；须遵守 Discord 每条消息最多 5 行、25 个组件的限制）；不做飞书渲染器；提醒；PM 频道和 Supervisor 频道的路由规则；开始写入反馈事件（草稿与最终版本的差异、修改或拒绝原因、问题数量、返工记录、各阶段耗时和 token 用量）。
- **退出标准**：在 test 环境，使用真实 Discord 测试频道和真实模型，跑通 S1、S3、S4、S5、S7、S8。
- **进度（2026-10-15，分支 `rebuild/core6`；阶段 2 已提交 `af78a11`）**：
  - 已完成：`core/gateway/`。其中 `service.py` 处理全部渠道逻辑（入站消息、按钮、答题弹窗、卡片同步、发件箱、提醒、`/status` `/pending` `/cancel`）；`discord_app.py` 是 discord.py 的薄封装，按钮用 `DynamicItem` 实现，网关重启后旧按钮仍然可用；点击时先应答 Discord，再写数据库。另有 `core/dispatch_main.py` 和两个命令：`assistant-kernel-core-dispatcher`、`assistant-kernel-core-gateway`。
  - 卡片在 Discord 上的形式：每个条目在 Supervisor 频道单独发一条消息，PM 频道发一条指引。条目被处理或被替代后，原消息改为"已处理"并撤下按钮。答题弹窗预填 Supervisor 的建议答案或推荐选项；超过 5 题时分页填写并保存草稿。内容超过 1900 字时，全文作为附件。
  - 测试：`tests/core` 共 53 项通过（新增 8 项网关测试、1 项离线 Discord 组件测试）。
  - 测试环境已就绪但尚未启动：测试库 `assistant_core_test` 已建好并完成迁移；supervisord 中已登记 `core-test-dispatcher` 和 `core-test-gateway` 两个程序，均使用 test venv，与生产互不影响；管理脚本为 `scripts/core_test_env.ps1`。
  - 暂缓（按 MVP 原则）：凭据卡片（S20）留到有项目真正需要时再做；答题弹窗暂用文本框，不用下拉选择。
  - 已上线测试环境（共用机器人，`kernel-v12-validation` 的两个频道）。
  - **真人 Discord 主线验收（10-15 06:59–07:25）：通过。**
    - 用户在 #pm 发出需求后，Supervisor 发现缺少具体内容，向用户提问；用户回答"不清楚"，Supervisor 改为安排 PM 去查项目记录。
    - 之后依次为：需求 →（批准）设计 →（批准）实施 → Kernel 验证（1 个文件，0 个问题）→ 独立 Review → 用户批准发布 → 发布 → 完成。
    - 共 12 个执行，全部第一次就成功，两个程序的日志里没有错误。
    - `docs/notes.md` 的三条要点都引用了项目中的真实记录。
  - 验收中发现并修复：
    - 问题卡片只有"回答"一个按钮 → 增加「其他想法…」（不回答，改写自己的想法交给 Supervisor 重新安排）和「停止任务」；
    - 用户看不到 PM 每一轮做了什么，任务完成时也没有通知 → 每个 PM 回合的总结发到 #pm，任务完成或停止时发通知（包括已发布的文件）。
  - 尚未由真人验证的场景：S3（卡片待处理时改变方向）、S5（向 Supervisor 咨询）、S8（取消任务）。测试中均已覆盖，计划用一个小任务快速手动验证一遍。

### 阶段 4：切换与清理
- **内容**：
  1. 旧数据移到 `archive_core5`；
  2. 迁移 grants 等保留表；
  3. 取消当前 kernel 任务；
  4. prod 部署新核心；
  5. 删除旧模块（`repositories/core.py`、`run_processor.py`、`goal_supervisor.py`、`supervisor_processor.py`、`supervisor_v12.py`、`question_groups.py`、`questionnaire.py`、`prompt_protocol.py`、`pm_turns_v12.py`、`task_queue.py`、`task_proposals_v12.py`、`task_lifecycle.py`、`run_scheduling.py`、`stage_handoff.py`、`execution_plan.py` 中已不用的部分、旧的 `prompts/roles` 和 `prompts/skills`、对应的旧测试、一次性迁移脚本）；
  6. 进程按 7.1 合并，重写 `supervisord.conf`；下线 `kernel-feishu`，删除 `feishu.py` 中的对话接入部分，保留凭据和底层客户端；
  7. 重写 README 和文档（历史文档移入 `docs/archive`）。
- **退出标准**：prod 健康检查通过；用户在真实 Discord 中验收 S1，以及至少一个返工场景。

**阶段 4 进度（2026-10-15）**：
- **4a 切换：已完成。** 提交为 `ce636ba`、`6bdbd54`。
  - 生产进程从 13 个减为 5 个：`kernel-core-dispatcher`、`kernel-core-gateway`、`kernel-api`、`kernel-mcp`、`docling-serve`。旧的 worker、Discord、飞书、维护、调度进程已全部下线。
  - `kernel`、`trading-risk-bot`、`kernel-v12-validation` 三个项目的 PM 和 Supervisor 频道全部由新核心接管（`config/core-discord.yaml`）。
  - 旧系统里未完成的 kernel 任务已取消；测试程序已移除。
  - 切换前已核对：三个项目的 PM（五个阶段）和 Supervisor 配置都能被新核心正常加载。
  - 切换中发现并修复：Docker 对 AI 容器的健康检查访问旧的 `/health/ready`，旧 worker 下线后容器被标记为 unhealthy。现在 `/health/ready` 改为检查新核心（数据库和 core 表可用；运行积压和待发消息只作参考），容器已恢复 healthy。
  - 部署脚本的就绪判断改为：所有 `kernel-*` 程序处于 RUNNING，且版本号一致。
- **4b 删除旧代码：暂缓（按 MVP 原则）。**
  - 依赖分析表明，旧代码是"缠在一起"而不是单纯不再使用：`agent_files`、`capability_bundle`、`project_changes` 等被复用的模块在文件开头就引入了旧仓库和旧服务；PM 使用的 `scoped_tools.py` 几乎引入了整个旧系统。新核心的依赖闭包因此仍有约 3.7 万行。
  - 安全的做法是：先把新核心真正用到的少数函数抽出来，写一个精简的核心版 MCP 工具服务，再整批删除。这属于纯清理性重构，不增加功能，所以单独作为后续清理步骤；在此之前旧代码只是闲置，不会被生产执行。
  - 旧数据移入 `archive_core5` 一项同样暂缓：被复用的服务仍会读取 `project_grants` 等少数旧表；新核心不读取旧的 runs 和 messages。
  - `kernel-api`（访问几乎全部是健康检查和少量旧 `/v2` 查询）和 `kernel-mcp`（有外部客户端在用）暂时保持原样。

### 阶段 5：项目创建
- **内容**：`project-spec.v1`（用途、目录授权、频道、所用角色和技能、业务背景档案、每周进化时间）；kernel 项目的项目创建技能（需求和设计阶段产出这份 spec）；BootstrapExecutor：在 kernel 任务的 build 阶段、设计已批准的前提下，**按固定规则**完成以下动作，可重复执行（幂等）：写 `projects` 表、建立 vault、采用模板 Agent 包、授权目录、创建 PM 和 Supervisor 频道并绑定、登记每周进化；Review 阶段核对每一项是否到位。
- **退出标准**：从一句"帮我建一个 X 项目"出发，走完五阶段，生成一个可用的新项目。

### 阶段 6：自我进化
- **内容**：
  - 每周日 13:00（Asia/Shanghai，可按项目覆盖）自动创建 `kind=evolution` 的任务，按项目和周去重，项目忙时排队；也可以手动触发（`/evolve` 或对 Supervisor 说）；
  - 进化技能：读取 `task_events` 和运营报告，产出 `improvement-proposal.v1`，每一项包含目标（技能、角色、模板、需求待办）、证据、预期收益、风险；
  - 用户逐项批准：采纳的需求待办变成新的 delivery 任务；技能或角色的修改作为新任务，走"候选 → 验证 → 发布新 Agent 包"；
  - **任何内容都不会自动发布**；
  - 需求扩展技能：读取项目的业务背景档案（行业、企业、用户），生成候选需求清单，由 Supervisor 用问题条目让用户勾选；
  - Kernel 层面：公共模板的改进由各项目自行选择是否采用。
- **退出标准**：一次完整的每周进化循环，从产生提案、用户批准，到技能新版本被下一个任务实际使用。

**依赖关系**：0 → 1 → 2 → 3 → 4 → 5 → 6。阶段 4 中的进程合并可以和阶段 3 并行。

---

## 9. 验收场景清单（阶段 0 先写成测试，之后每个阶段逐步转为必须通过）

| # | 场景 | 关键断言 |
|---|---|---|
| S1 | 主线：新需求 → PM 提问 → 用户作答 → 需求文档 → 批准并进入设计 → 设计 → 批准并进入实施 → 验证通过 → Review → 发布批准 → 发布 → 完成 → 自动激活下一个排队任务 | 每一步只有一个游标；共两次批准加一次发布批准 |
| S2 | PM 运行中用户在 PM 频道补充 | 下一轮 PM 读到补充，并记录 consumed_by_run_id |
| S3 | 卡片待处理时，在 PM 频道说出方向性变化 | 卡片升级，草稿保留；点击旧版本只回复"已更新" |
| S4 | 卡片待处理时，在 PM 频道说普通补充 | 卡片不变；提交时补充一起送给 PM |
| S5 | 有活动任务时在 Supervisor 频道咨询 | 有回复，任务状态不变 |
| S6 | 在 Supervisor 频道提出新工作 | 卡片中出现"创建新任务？"条目，确认后新任务进入排队 |
| S7 | 紧急任务 | 在安全时点挂起原任务 → 执行紧急任务 → 完成 → 原任务经由 Supervisor 恢复 |
| S8 | PM 运行中取消任务 | 回合结果作废、已产生的效果有记录、激活下一个任务 |
| S9 | Supervisor 重试耗尽 | 出现 Kernel 兜底卡，主线不卡死 |
| S10 | PM 重试耗尽 | 出现"重试 / 修改 / 停止"条目 |
| S11 | 验证失败 | 经 Supervisor 提议回到 build |
| S12 | Review 要求修改设计 | 设计改版后设计批准自动失效，重新批准前无法进入 build |
| S13 | 设计批准后修改需求 | 设计批准按推导自动失效 |
| S14 | 回合数超过上限 | 出现"继续 / 停止"条目 |
| S15 | 渠道事件重复送达 | 只处理一次 |
| S16 | 两个项目同时运行 | 两边 PM 并行执行；各自一张卡；互不阻塞 |
| S17 | 发布中途崩溃 | 进入对账条目，不自动重放 |
| S18 | 卡片待处理时业务程序提问 | 同一张卡新增条目，单独作答后答案只送达该程序 |
| S19 | Dispatcher 在执行中重启 | pm_turn 重新排队；publish 转入对账 |
| S20 | 凭据输入 | 凭据不出现在模型上下文、日志或 messages 中 |
| S21 | 每周进化触发时项目正忙 | 进化任务排队；同一周只创建一次 |
| S22 | 两轮之间项目 Agent 配置更新 | 下一轮使用新配置，不产生任何失效或报错 |

---

## 10. 修订记录（推翻或修正此前结论的地方）

| 日期 | 原结论 | 修订 | 原因 |
|---|---|---|---|
| 10-15 | 每个任务一个交互槽 | **每个项目一张卡，卡内多个条目** | 咨询提议和业务程序提问不属于当前任务，按任务分槽仍然会出现两张卡，与"Kernel 只协调一处"的要求不符 |
| 10-15 | venv 推广时把 test 改名为 prod | 把同一个 wheel 重新安装到 prod | venv 的 shebang 是绝对路径，改名会失效 |
| 10-15 | 阶段 1 先删除旧协议 | 新核心并行开发，切换时整批删除旧模块 | 避免在即将被替换的代码上做精细删改 |
| 10-15 | 阶段 0 修复"点击旧卡生成第二个任务" | 改为可选：仅在重建期间仍实际使用旧版本时才做 | 新核心从结构上消除了这个问题 |
| 10-15 | 定时许可在领取时检查（`authorize_claim`） | 改为入队时检查 | 领取时不应产生副作用（原则 P6） |
| 10-15 | 旧 venv 删除 | 81 个旧版本移入 Windows 回收站，容器内的符号链接记录在 `assistant-data/backups/venv-symlinks-20261015.tsv` | 用户要求可恢复；Windows 无法访问容器里创建的 Linux 符号链接 |

| 10-15 | 常设 `assistant_test` 测试库 | 每次测试临时建库、跑完即删 | 沿用现有测试的成熟做法，避免测试之间互相污染 |
| 10-15 | 阶段 0 同时提供 FakeGateway 和场景辅助函数 | 阶段 0 只提供 FakeHarness、临时库夹具和迁移执行器；FakeGateway 和 `send_pm_text`、`card()`、`click()` 等辅助函数随阶段 1 的服务接口一起实现 | 这些辅助函数依赖还不存在的接口，先写会是空转 |
| 10-15 | 迁移执行器放在 `scripts/migrate.py` | 放进包内：`assistant_kernel/core/migrate.py`，SQL 放在 `core/sql/`，命令为 `assistant-kernel-migrate` | 部署和测试使用同一份随 wheel 发布的迁移代码 |
| 10-15 | 新表与旧表同在 `assistant` schema，阶段 4 再迁走旧表 | 新表放在独立的 `core` schema；阶段 4 只需把旧的 `assistant.runs/messages` 等移入 `archive_core5` | 开发期间新旧表不会重名冲突；即使误把开发分支部署到生产，也只会多出一个空 schema |
| 10-15 | 卡片按钮提交时带卡片版本号，用于识别旧卡 | 改为按条目 id 识别：条目一旦被处理或升级就换新 id，点击不存在的条目即视为旧卡 | 同一张卡上逐项提交时，其余条目不会被误判为旧卡；草稿修改也不再增加版本号 |
| 10-15 | 卡片上的"补充说明"在提交时一并转交 | 补充消息挂在任务上，下一个 PM 回合领取时统一读取（`consumed_by_run_id`）；卡片上的补充区只做展示 | 补充内容只走一条路径，不会重复投递 |
| 10-15 | 咨询类 Supervisor 执行绑定当前任务 | 咨询执行不绑定任务（`task_id` 为空，只在输入中记录相关任务） | 咨询不会阻塞 PM 执行，也不会被当作任务的游标 |
| 10-15 | S7 紧急任务抢占挂起、S18 业务程序共用卡片 | 推迟到 MVP 之后：紧急任务暂时只在队列中排到最前，不抢占正在进行的任务；业务程序提问等运行单元启用后再做 | 依据 MVP 优先原则；运行单元相关表目前为空 |
| 10-15 | Supervisor 在任务评估中可以提出新任务 | 只有咨询可以提出新任务；任务评估只产出 ask_user / propose_next / relay_pm_questions / propose_complete / propose_cancel | 保证每个任务同一时刻只有一个卡片条目 |
| 10-15 | 回滚取"上一个 wheel" | 只有成功部署到生产的 wheel 才会被标记 `.deployed`，回滚和保留策略只看这些；`-TestOnly` 测完即删除构建出的 wheel | 避免回滚到从未上线过的版本 |
| 10-15 | 阶段 3 另建测试机器人（方案 A） | **共用现有机器人**：新核心只接管 `kernel-v12-validation` 的两个频道（`config/core-discord.yaml` 中 `shared_bot: true`）；旧网关新增几行代码，跳过该配置文件里列出的频道；共用模式下新网关不注册斜杠命令（否则会覆盖旧机器人的命令），`/status`、`/pending`、`/cancel 确认` 改为在频道里直接发送文字；令牌沿用旧网关的来源。按钮不冲突：旧网关只认按消息登记的按钮，新核心按钮的 id 以 `core:` 开头 | 用户不必再创建机器人；阶段 4 切换时把其余频道也列入配置即可 |
| 10-15 | 网关同时接 Discord 和飞书 | 只接 Discord；飞书退出 Kernel 业务逻辑，保留供以后的 webhook、多维表格使用 | 用户决定 |

## 11. 用户确认记录
| 日期 | 事项 | 结论 |
|---|---|---|
| 10-15 | 方案 B（新建 `tasks` 表，重写控制核心） | 同意 |
| 10-15 | PM 频道出现方向性新输入时，由 Supervisor 判断是否升级卡片，保留草稿 | 同意 |
| 10-15 | 飞书 | 不参与 Kernel 业务逻辑，以后用于 webhook、多维表格等 |
| 10-15 | 每项目一张卡 | 同意 |
| 10-15 | 每次发布都需要批准，暂时取消低风险自动发布 | 同意 |
| 10-15 | 旧数据移到 `archive_core5` 保留，新核心不读取 | 同意 |

| 10-15 | 阶段 0 细节和线上切换 | 同意并已执行 |
| 10-15 | 阶段 2 的 D1–D4 | 同意 |
| 10-15 | 验证无法顺利通过的事项先跳过、存档并汇报，必要时先讨论再处理 | 已记录 |

待确认：
- **阶段 3 的 Discord 测试方式**（问题 I5）：
  - A：另建一个测试用的 Discord 机器人，连同一个服务器里的测试频道；新核心只连这个测试机器人，旧系统继续用正式机器人。（推荐：互不干扰，阶段 4 切换时把正式机器人的令牌换给新核心即可。）
  - B：阶段 3 只用假渠道测试，真实 Discord 留到阶段 4 切换时一次性验证。
  - C：在旧网关里按频道分流，测试频道的消息转给新核心（改动旧代码，不推荐）。
