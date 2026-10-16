# 第二轮改进计划：技能体系、公共背景、分诊路由、以任务创建项目、代码交付

> 状态：方案已确认（2026-10-16，第 3 版），R1 实施中。
> 前置：第一轮重建（`REBUILD_PLAN.md` 阶段 0–6、4b）已完成并在生产运行（0.33.0）。
> 原则：顾全局、闭环、无副作用；主线优先（MVP），不为小概率事件写重代码，不追求一步到位，长期运行中再打补丁；
> 发现与既有决定冲突时先问用户。

## 1. 目标

1. **技能是持续改进的核心**：agent 的表现主要由技能话术决定。统一技能的格式和加载规则；公共技能作为模板在创建项目时
   交给项目，项目在运营中自行迭代；kernel 汇总各项目的运营情况打磨模板。
2. **需求阶段充分收集信息**：先定位组织、行业和项目现状，用多问题卡片分轮收集、化解矛盾，背景信息自动存档复用。
3. **子 agent 用得上、用得对**：开启原生子 agent；任务说明和问题回流写进主 agent 的技能。
4. **公共背景**：kernel 维护所有 agent 都可能用到的公共事实（组织与用户、技术环境、工作约定），按场合注入各项目。
5. **分诊与路由**：闲聊不再变成任务；kernel 能把请求送到已有项目的 Supervisor，或与用户商量后新建项目。
6. **以任务方式创建项目**：PM 在需求阶段与用户商定章程，批准后由 Kernel 创建，并交付模板技能。
7. **kernel 自我迭代**：与普通项目同样的复盘和任务流程；代码用 git 交付、独立测试环境验证，部署由用户执行。
8. **每次迭代都记录原由**。

## 2. 用户已确认的决定

| # | 决定（2026-10-16） |
|---|---|
| 1 | 所有项目都加分诊，闲聊在任何项目都不再建任务 |
| 2 | 路由时在目标项目的 Supervisor 频道发交接说明，并让目标 Supervisor 先回应一次；原频道给出跳转链接 |
| 3 | 新项目改为：PM 在需求阶段产出章程，用户批准后创建 |
| 4 | kernel 的自我升级与普通项目一样：每周复盘和手动触发，汇总工件和运行数据交给模型提出建议，批准后执行，记录原由 |
| 5 | kernel 用 git worktree 加提交编号交付 |
| 6 | main 是唯一的整合分支，所有改动（包括人工开发）走分支、快进合并 |
| 7 | kernel 任务使用独立的 `kernel-venvs/task` 测试环境 |
| 8 | kernel 发布批准后合并到 main 并通知"可部署"，由用户执行 `deploy.ps1`（方案 A） |
| 9 | 需求阶段用多问题卡片收集组织、行业、项目背景并存档；子 agent 的任务说明和问题回流写进主 agent 的技能 |
| 10 | kernel 维护公共背景（组织与用户、技术环境、工作约定），所有 agent 按场合只读使用；路由依据各项目章程，不写在公共背景里 |
| 11 | 技能就地升级（保留原名称和版本历史），不另起一套 |
| 12 | R2 简化：kernel 有任务进行时，PM 频道的消息仍作为当前任务的补充；要转去别的项目，在 Supervisor 频道说 |
| 13 | 实施顺序 R1 → R2 → R3 → R4a → R4b，每期部署后用户在 Discord 走一遍主线 |
| 14 | 公共技能是 kernel 创建项目时交付的基础模板，各项目自行迭代、可以推翻；kernel 的自我升级包括打磨模板，需要汇总各项目的运营信息 |
| 15 | 现有项目的技能副本直接替换为新版本（旧副本归档）；默认共享级别 `improvements` |
| 16 | 公共背景分三份：组织与用户、技术环境、工作约定；由技能加更新卡片维护（用户批准后 Kernel 写入），不提供写入工具 |

## 3. 概念与命名

| 名称 | 是什么 | 存放位置 | 谁维护 |
|---|---|---|---|
| **合同** | 输出 JSON 的格式、可选的决定，以及 Kernel 代码依赖的少数章节标题；Kernel 用代码校验 | 包内 `prompts/core/` | 只能随代码修改 |
| **公共模板** | 一套基础技能和项目文件模板 | 包内 `prompts/skills/`、`core/project_template/` | kernel 项目（打磨模板） |
| **项目技能** | 项目实际使用的技能，创建时从模板复制，之后归项目所有 | 项目 vault `.agents/skills/` | 项目自己（复盘提出、任务实施） |
| **工件** | 各阶段的产出文档，是 agent 之间唯一的交接依据 | 任务文档存储；章程在项目 vault | PM 编写，用户批准 |
| **项目章程** | 用途、目标、背景、范围、约束、路由信息、背景存档（取代 business-profile） | vault `knowledge/charter.md` | 创建时商定；之后由任务修改，背景存档由 Kernel 追加 |
| **公共背景** | 所有 agent 都可能用到的公共事实：组织与用户、技术环境、工作约定 | kernel vault `knowledge/shared/` | kernel（更新卡片，用户批准） |
| **角色** | 某个项目对 PM 或 Supervisor 的个性化要求，几行即可 | vault `agents/pm/role.md`、`agents/supervisor/role.md` | 创建时商定，复盘调整 |
| **能力** | 项目开启的额外功能：`routing`、`project_management`、`shared_context`、`kernel_maintenance`、`git_delivery` | `project.yaml` 的 `capabilities:` | 创建时确定 |
| **交付方式** | 文件交付（`vault-files`）或代码交付（`git-repo`） | `project.yaml` 的 `delivery:` | 创建时确定 |

区分合同和技能的原因：技能可以频繁改进，甚至被项目推翻；合同一改就要改代码。所以凡是 Kernel 代码依赖的东西
（输出字段、决定类型、Kernel 要截取的章节标题"背景信息存档"）都放在合同里，技能不能改变它们。这样项目怎么改技能都不会弄坏系统。

## 4. 技能体系

### 4.1 模板与项目副本
- **模板**：包内 `prompts/skills/` 是公共技能模板，随 kernel 的代码版本发布。
- **交付**：创建项目时，Kernel 把与该项目能力匹配的模板技能复制到项目的 `.agents/skills/`，在每个副本的元数据中写入
  `template: {name, version}`。
- **项目迭代**：之后 Kernel 只加载项目自己的副本。项目在复盘中提出修改，经批准后作为任务实施，可以大改甚至推翻模板。
- **模板更新**：模板出新版本时，Kernel 在各项目的周度复盘材料中列出"模板有更新：名称、版本、变更记录"，由项目自己判断
  是否采用、采用哪些部分；**从不自动覆盖项目副本**。
- **打磨模板**：kernel 的复盘拿到各项目的运营汇总（第 6 节），据此修改模板，属于 kernel 的任务（代码交付）。

### 4.2 格式与加载规则
每个技能一个目录，`SKILL.md` 带开头元数据（与 Codex 技能格式兼容）。完整规范见模板技能 `assistant-capability-maintenance`。

```yaml
metadata:
  version: "3.0.0"
  load: {roles: [pm], stages: [requirements]}   # Kernel 按此选择
  template: {name: assistant-requirements, version: "3.0.0"}   # 仅项目副本有
  changelog: ["3.0.0 2026-10-16 改了什么、为什么"]
```

- `roles`、`capabilities`、`task_kinds` 必须满足；`stages` 匹配 PM 的阶段回合（复盘记为 evolution），`purposes` 匹配其他场合
  （Supervisor 的 evaluate / review_input / consult 和分诊 triage），两者同时写时满足其一即可；没写的键不限制。
- **常驻**（默认）：匹配时每轮加载全文。**按需**（`on_demand: true`）：只在提示中列出名称、用途和路径，agent 需要时用
  `vault_read_text` 读全文。不常用的领域方法用按需方式，节省上下文。
- 每轮的拼接顺序：常驻技能 → 章程和角色 → 按需技能索引 → Kernel 提供的上下文（工件、指令、运行记录）→ 合同。
- 每次运行记录实际加载的技能及版本（`名称@版本`），供复盘对比"改技能前后"的效果。

### 4.3 每个 agent、每个场合加载什么

| agent / 场合 | 常驻技能 | 按需技能 |
|---|---|---|
| PM · 需求 | project-agent、task-continuity、user-questions、source-scope、**requirements**、subagent-orchestration | history-retrieval、volcengine-models |
| PM · 设计 | 公共四项、**solution-design**、subagent-orchestration | history-retrieval、agent-design、mcp-design、workflow-design、project-structure、volcengine-models |
| PM · 实施 | 公共四项、**implementation**、subagent-orchestration、capability-maintenance；代码交付项目加 git-delivery | 同设计 |
| PM · Review | 公共四项、**release-audit**、subagent-orchestration；代码交付项目加 git-delivery | history-retrieval、volcengine-models |
| PM · 运营 | 公共四项、**operation** | history-retrieval、volcengine-models |
| PM · 复盘 | 公共四项、**evolution-review**、subagent-orchestration、capability-maintenance | history-retrieval |
| PM · 创建项目任务（kernel） | 需求阶段各项 + project-chartering | 同需求 |
| PM · 分诊（PM 频道，R2） | task-continuity、user-questions、source-scope、**triage-routing** | history-retrieval |
| Supervisor · 评估 / 新消息 | **goal-supervisor**、task-continuity、user-questions | history-retrieval |
| Supervisor · 咨询（Supervisor 频道） | 同上 + triage-routing（R2） | history-retrieval |
| kernel 项目额外 | PM 和 Supervisor 加 shared-context（R1）；设计、实施、Review、复盘和 Supervisor 评估加 kernel-maintenance（R4b） | — |
| 所有场合 | 公共背景按其 `load` 注入（例如技术环境注入设计、实施、Review、运营；组织与用户注入需求和 Supervisor） | — |
| 子 agent | 不加载技能，只接收 PM 按 subagent-orchestration 写的任务说明 | — |

（表中名称省略了前缀 `assistant-`。"公共四项"＝ project-agent、task-continuity、user-questions、source-scope。）
估算每轮常驻技能约 8–12 千字，比现在每轮注入的旧技能少一半以上。

### 4.4 技能清单与处理方式（已在 `prompts/skills/` 中完成）

**就地升级**（保留名称和版本历史，内容按核心 v6 重写，版本号进一位）：

| 技能 | 新版本 | 新内容 | 原由 |
|---|---|---|---|
| assistant-project-agent | 2.0.0 | PM 工作规则：一轮一件事、事实优先级、工件是交接依据 | 旧版是已废弃的续接协议和 checkpoint 字段 |
| assistant-task-continuity | 2.0.0 | 工件约定：各工件固定章节、R/A/D/P 编号、来源标记 | 连续性现在靠工件实现；统一结构让 agent 之间衔接严谨 |
| assistant-requirements | 3.0.0 | 需求发现：先查后问、三轮（定位、细化、取舍补充）、化解矛盾、背景存档 | 用户要求：充分收集组织、行业、项目背景，用户常说不清或自相矛盾 |
| assistant-solution-design | 2.0.0 | 需求覆盖表、文件变更清单、测试方案、可并行子任务 | 让实施和 Review 可以逐条对照 |
| assistant-implementation | 2.0.0 | 严格按设计实施、真实自测、子任务汇总 | 去掉旧字段 |
| assistant-release-audit | 2.0.0 | 独立 Review，逐条核对验收标准 | 让结论有证据 |
| assistant-operation | 2.0.0 | 按章程成功标准评估运营效果 | 去掉旧流程 |
| assistant-goal-supervisor | 6.0.0 | 揣摩意图、给 PM 的指令模板、三种场合的做法 | 旧版是已废弃的 Action Plan 协议 |
| assistant-subagent-orchestration | 2.0.0 | 子 agent 不能与用户交互；任务说明模板；问题回流由主 agent 向用户提问，下一轮重新派 | 用户要求；旧版要求的输出字段已不存在 |
| assistant-capability-maintenance | 2.0.0 | 技能编写规范、加载规则、模板与项目副本、修改与采用流程 | 技能是长期改进的核心，需要统一规范 |
| assistant-source-scope | 4.0.0 | 按当前工具由粗到细查找文件、只碰需要的文件 | 旧版描述的授权申请工具已不存在 |
| assistant-history-retrieval | 1.0.0（按需） | 何时、怎样查历史对话 | 工具行为已变化 |

**新增**：assistant-user-questions（多问题卡片出题规范，PM 和 Supervisor 共用）、assistant-evolution-review（周度复盘，
技能话术是主要改进对象）、assistant-shared-context（维护公共背景，kernel 项目开启 `shared_context` 能力时加载）；以及随后续各期启用的草稿 assistant-triage-routing（接替 kernel 和 trading-risk-bot 中旧的
assistant-butler-intake）、assistant-project-chartering、assistant-git-delivery、assistant-kernel-maintenance。

**改为按需**（内容暂不重写，旧用语以 project-agent 和合同为准，以后由复盘按需重写）：agent-design、mcp-design、
workflow-design、project-structure、volcengine-models。

**退役**（相应功能在新核心中已不存在，移到 `docs/archive/skills-retired/`，功能恢复时再按新格式重写）：
session-rotation-handoff（没有会话轮换）、project-credentials（没有凭据工具）、system-config（旧系统配置）、
schedule-management（没有定时任务功能）；以及各项目 vault 中的 butler-intake、schedule-design。

**由提示文件并入技能**：`pm-common.md`、`pm-<阶段>.md`、`pm-evolution.md`、`supervisor.md` 以及 `supervisor-*.md` 中的指导部分，
R1 删除；只保留合同部分。`profiles/*.yaml` 和 agent `config.yaml` 中的技能列表取消，由 `load` 取代。

### 4.5 角色和章程模板（已更新 `core/project_template/`）
- `agents/pm/role.md`、`agents/supervisor/role.md`：只写"本项目对该角色的特别要求"，业务内容指向章程。
- `knowledge/charter.md`：9 节固定结构（用途、目标与成功标准、使用者、组织与行业背景、范围、约束、路由信息、变更记录、背景信息存档）。

## 5. 公共背景（kernel vault `knowledge/shared/`）

### 5.1 内容
| 文件 | 内容 | 主要读者 |
|---|---|---|
| `organization.md` 组织与用户 | 组织概况、行业与细分、业务模式、用户本人的岗位和决策权、沟通偏好 | 需求、创建项目、Supervisor |
| `environment.md` 技术环境 | 开发机、Docker 容器和路径对应、数据库、模型服务、Discord、远程主机及用途、开发完成后部署到哪里和怎么部署 | 设计、实施、Review、运营 |
| `conventions.md` 工作约定 | 语言、代码和提交规范、部署流程、安全规则 | 实施、Review |

- 每份文件开头写 `load`（规则与技能相同），Kernel 从 kernel vault 直接读取并按场合注入各项目上下文；项目只读，不复制副本。
- 每份控制在 2 千字以内；每条事实带来源和核实日期。
- **绝不写密钥、密码、令牌**：主机用别名和用途描述，凭据只写存放位置和名称。公共背景会进入所有 agent 的上下文。
- 与项目章程的分工：所有项目通用的写公共背景，项目特有的写章程（例如某项目运行在哪台远程主机）；冲突时以章程为准。
- **不用于路由**：路由只依据各项目章程（见 R2）。

### 5.2 维护
- 技能 `assistant-shared-context`（加载给 kernel 的 PM 和 Supervisor）：什么算公共信息、写在哪里、怎么写、什么不能写、怎样识别过时信息。
- 决定 `propose_shared_update`：kernel 的 Supervisor（咨询、评估）提出"哪份文件、哪一节、改成什么、原因"，卡片显示改动前后对比；
  用户批准后由 Kernel 按章节标题替换内容并在变更记录追加一行。不走五个阶段；不提供 agent 直接写入的工具。
- 信息来源：用户直接告诉 kernel；在其他项目中提到的公共事实由分诊或咨询路由到 kernel；kernel 复盘时从各项目背景存档发现新事实，
  或发现超过 90 天未核实的条目；技术环境类事实可由 kernel 用只读工具核实。
- 事实优先级：用户原话 → 已批准工件 → 项目章程 → 公共背景 → 调查 → 推断。

### 5.3 初稿
R1 根据已知情况写初稿（Windows 宿主机、Docker 容器 `AI`、路径对应、`rag-postgres`、Codex、Discord 等）；
远程主机和部署去向等未知内容标为"待补充"，由用户通过更新卡片补充。

## 6. 跨项目运营信息与访问授权

kernel 要打磨模板、判断路由，需要了解各项目的情况；但各项目的业务内容（例如交易数据）不应被 kernel 的 agent 随意读取。

### 6.1 原则
1. **由 Kernel 代码汇总，不给 agent 跨项目读取的工具**：kernel 的 PM 和 Supervisor 仍然只能访问 kernel 自己的目录。
   其他项目的信息只以 Kernel 生成的"运营汇总"形式进入 kernel 的复盘、分诊和咨询上下文。
2. **只读不写**：kernel 从不修改其他项目的文件；对其他项目的改进只能以"模板更新"的形式提供，由项目自己决定是否采用。
3. **分级共享，由项目决定**：每个项目在 `project.yaml` 中设置 `share_with_kernel`，创建时在章程中商定，之后可以通过任务修改。

### 6.2 共享级别

| 级别 | kernel 能看到的内容 | 用途 |
|---|---|---|
| 路由信息（始终提供） | 章程第 1、5、7 节，项目状态和频道 | 分诊和路由 |
| `metrics` | 运行指标：各阶段回合数、需求阶段的提问轮数和题数、卡片操作的类型和次数、退回和返工次数、失败和重试、完成时长、各次运行使用的技能版本 | 判断哪个技能话术需要改 |
| `improvements`（默认） | 上一级全部，加上：复盘的改进建议、项目对技能副本的修改及 changelog、章程背景存档中的事实 | 打磨模板、更新公共背景 |
| `full` | 上一级全部，加上：工件全文和对话 | 深度分析（项目明确同意时） |

### 6.3 汇总怎么产生
Kernel 已经在数据库中记录了所有项目的任务、运行、消息和卡片事件（`core` schema），第一轮的周度复盘已经在用其中的单项目部分
（`project_digest`）。跨项目汇总在此基础上按各项目的共享级别筛选、按项目分组，只在 kernel 的复盘任务中提供；
技能修改情况由 Kernel 比较项目副本与模板（版本号和 changelog）得到。

## 7. 分期实施

各期之间保持可用：每期结束时都能部署，旧行为在新行为接管之前继续工作。每期部署后请你在 Discord 走一遍主线。

### R0 分支整理（已完成）
main 已快进到最新提交；本轮在分支 `kernel/r1-skills` 上进行，完成后快进合并到 main。`rebuild/core6` 停用，确认无用后删除。

### R1 技能体系、章程、公共背景、子 agent、迭代原由（核心）
1. **技能加载器**：只读项目 vault `.agents/skills/` 中的技能，按 4.2 的规则选择常驻和按需技能；PM、Supervisor 执行器改用它；
   删除 profiles、`role_append` 和 agent 配置中技能列表的加载代码；运行记录写入实际加载的技能及版本。
2. **合同与技能分离**：删除已并入技能的提示文件；合同中增加"需求文档必须包含'背景信息存档'一节"。
3. **现有项目就地升级**（kernel、trading-risk-bot、kernel-v12-validation）：
   - 把各项目 `.agents/skills/` 中的旧副本移到 vault 的 `archive/skills-20261016/`，再从模板复制新版本，写入 `template` 元数据；
   - 项目自有技能（trading-risk-bot 的 `trading-risk-guardrails`）保留，补上 `load` 元数据；
   - 从 `project.yaml` 和 `business-profile.md` 生成章程（不调用模型）；`role.md` 精简为特别要求；kernel 中旧的 `intake` agent 归档。
4. **章程与背景存档闭环**：执行器读取章程代替业务背景；用户批准需求文档时，Kernel 把其"背景信息存档"一节追加到章程第 9 节。
5. **公共背景**：在 kernel vault 建立 `knowledge/shared/` 三份初稿；加载器按 `load` 注入各项目；启用技能 assistant-shared-context；
   新增决定 `propose_shared_update`（卡片显示改动前后，批准后 Kernel 按章节写入并记变更）。
6. **能力、交付方式、共享级别**：`project.yaml` 增加 `capabilities:`、`delivery:`、`share_with_kernel:`；代码中"是否为 kernel"的判断
   改为"是否开启某项能力"。
7. **开启子 agent**：PM 在需求、设计、实施、Review 和复盘中开启原生子 agent，最多 3 个。
8. **迭代原由**：后续任务提议的每一项改为 `{goal, reason}`（仍接受纯文本），原因写入新任务、显示在卡片上并进入 PM 上下文；
   任务结束时 Kernel 在项目的 `changes/iterations.md` 追加一行：日期、任务、来源、原因、结果、发布内容。
9. **创建项目时交付模板技能**：BootstrapExecutor 复制模板技能并写入 `template` 元数据（R3 前沿用现有的规格创建方式）。
10. **验证遗留问题 I6**：确认发布器可以写项目 vault 的 `knowledge/`、`agents/` 和 `.agents/skills/`；不行就加入允许范围。
11. **测试**：加载规则（常驻、按需、能力、任务类型）、项目副本优先、旧技能不再出现在提示中、背景存档追加、后续任务带原因、迭代记录。

退出标准：三个项目每轮只加载新技能；一次真实的需求对话能用多问题卡片收集背景，批准后章程第 9 节有存档；
复盘建议批准后，新任务带原因，`iterations.md` 有记录。

**R1 完成记录（2026-10-16，分支 `kernel/r1-skills`，提交 `077a472`）**：
- 代码：技能加载器（`core/skills.py`）、合同与技能分离、章程与背景存档、公共背景及更新卡片（`propose_shared_update`）、
  能力取代写死的 kernel 判断、PM 开启子 agent（最多 3 个）、后续任务带原因、迭代记录（`core/vault_sync.py`，在调度器维护节拍上执行，
  每步幂等）、创建项目时交付模板技能、迁移 0004（任务原因列）。
- I6 已解决：项目可以通过正常发布修改自己的章程、角色和技能。验证器原先要求技能含"## 任务目标"标题，与新规范冲突，
  改为检查 `metadata.load` 和 `metadata.version`。
- 后续各期的 4 个草稿技能标为 `disabled`，到对应一期再启用（避免在 R1 中被加载）。
- 测试：全部 138 项通过、4 项跳过。
- 生产 vault 已迁移（kernel、trading-risk-bot、kernel-v12-validation）：旧技能副本、profiles、旧角色和 kernel 的 intake agent 归档在
  各 vault 的 `archive/round2-r1/`；已确认当前运行的旧版本代码在新 vault 上仍能正常加载。
- 已部署 0.34.0（2026-10-16），迁移 0004 已执行，所有服务正常。部署时发现 `build/` 缓存会把源码中已删除的文件继续打进 wheel（生产包里一直带着旧技能和旧模块），`deploy.ps1` 已改为每次构建前清空 `build/`；新包已确认干净。
- **待办**：用户在 Discord 走一遍主线；通过后把 `kernel/r1-skills` 快进合并到 main。

### R2 分诊与路由
1. 项目空闲时，PM 频道的消息先由分诊处理（只读、全新会话、用 PM 的角色），输出 `chat` / `start_task` / `route` / `propose_project`；
   Supervisor 频道的咨询增加 `route`。
2. 项目名录：Kernel 每次分诊时汇总各项目章程第 1、5、7 节生成（agent 不能直接读其他项目），提供给开启 `routing` 能力的项目。
3. 路由：Kernel 校验目标项目 → 交接说明写入目标项目并发到其 Supervisor 频道 → 目标 Supervisor 针对它回应一次 →
   原频道回复跳转链接。不在目标项目建任务。
4. 启用技能 assistant-triage-routing。有任务进行时 PM 频道的消息仍作为补充（决定 12）。

退出标准：闲聊不再建任务；在 kernel 频道说出属于 trading-risk-bot 的请求，能收到跳转链接，trading-risk-bot 的 Supervisor 频道出现交接说明和回应。

### R3 以任务方式创建项目
1. `kind=bootstrap` 的任务只走需求阶段：PM 按 project-chartering 和 requirements 与用户商定章程，提交 `project_charter`
   （章程正文加"创建参数"：ID、名称、能力、交付方式、共享级别、角色要点）。
2. 用户批准章程 → `bootstrapping` → BootstrapExecutor 读取已批准的章程，写入章程、角色、`project.yaml`、模板技能，
   创建频道。
3. Supervisor 不再直接提交项目规格；`propose_project` 改为提议创建一个"创建项目"任务。

退出标准：从 kernel 频道一句"帮我建一个……项目"出发，经过多问题卡片商定章程，批准后项目可用，新项目能被分诊匹配。

### R4a 代码交付（通用）
1. 进入实施阶段时，Kernel 准备 git worktree：`assistant-data/workspaces/<项目>/<任务>`，分支 `task/<任务编号>`，从 main 拉出。
2. 提交实施报告时，Kernel 读取最新提交编号作为 `bundle_sha`，后续批准都绑定它。
3. 验证：在 `kernel-venvs/task` 中从该提交构建、安装、运行 `tests/core`（临时数据库）。
4. 发布：批准后 main 快进到该提交；不能快进时视为验证失败，回到实施阶段。
5. kernel 项目发布后通知"0.x.y 可部署，请执行 `deploy.ps1`"。任务结束时删除 worktree。
6. 启用技能 assistant-git-delivery。

### R4b kernel 自我维护与模板打磨
1. kernel 代码仓库加入 `AGENTS.md`（Codex 在 worktree 中自动读取）和 `docs/ARCHITECTURE.md`。
2. kernel 项目开启 `kernel_maintenance` 能力和 `git-repo` 交付。
3. **跨项目运营汇总**（第 6 节）进入 kernel 的周度复盘；各项目复盘材料中加入"模板有更新"。
4. 启用技能 assistant-kernel-maintenance。

## 8. 迭代原由的记录（闭环）

| 记录在哪里 | 记什么 | 由谁写 |
|---|---|---|
| 改进建议（improvement_proposal） | 每条建议的现象、原因、建议 | PM（复盘） |
| 新任务的 `reason` | 被批准的建议的原因，或用户、Supervisor 给出的原因 | Kernel（来自后续任务提议） |
| 项目 `changes/iterations.md` | 每个结束的任务一行：日期、任务、来源、原因、结果、发布内容 | Kernel |
| 技能 changelog | 每次修改的版本、日期、原因、建议编号；采用模板时写明采用和保留了什么 | PM（实施） |
| git 提交说明（代码交付） | 做了什么、为什么、引用的需求和设计编号 | PM（实施） |
| 运行记录中的技能版本 | 每次运行加载了哪些技能的哪个版本 | Kernel |

## 9. 风险与已知限制

- 技能加载改变了每轮的提示内容，第一次部署后模型行为会有变化：先在 kernel-v12-validation 走一遍主线再推广。
- 背景存档由 Kernel 按标题"背景信息存档"截取：标题写在合同里，PM 必须使用；缺少时 Kernel 在批准卡片上提示。
- 按需技能依赖 agent 主动读取，可能该读时没读：复盘中观察，必要时改为常驻。
- 分诊多一次模型调用；子 agent 增加单轮耗时和用量（最多 3 个）。
- 代码交付依赖容器内的 git 和 worktree 目录权限：R4a 开始时先验证。
- 跨项目汇总只提供统计和摘要，kernel 对个别项目的问题看得不如项目自己清楚：模板改进以"多个项目共同的问题"为主。

## 10. 修订记录

| 日期 | 原结论 | 修订 | 原因 |
|---|---|---|---|
| 10-16 | 另起一套新技能，旧技能归档 | 在原名称上就地升级，保留版本历史 | 用户决定 |
| 10-16 | kernel 维护组织档案，含业务线与项目对照，用于路由 | 改为公共背景（组织与用户、技术环境、工作约定）；路由只依据各项目章程 | 用户澄清：公共背景是所有 agent 的公共事实，路由由技能检查各项目章程即可 |
| 10-16 | 实施阶段由 PM 更新章程 | 需求文档获批时由 Kernel 把背景存档追加到章程 | 代码交付的项目写不到 vault；没有实施阶段的任务也要存档 |
