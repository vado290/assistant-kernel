---
name: assistant-capability-maintenance
description: 编写和修改技能（以及 agent 的 config.yaml）的规范：技能的固定结构、写法原则、模板与项目副本的关系和修改流程。提出或实施技能改进时使用。
metadata:
  version: "2.5.0"
  load: {roles: [pm], stages: [build, evolution]}
  io:
    reads: [improvement_proposal, skills]
    writes: [skills]  # 仅获批 build 的写项；evolution 只写改进建议
  changelog:
    - "2.5.0 2026-10-22 补仅 evolution 的自身 skills/roles/加载审查与获批变更交接"
    - "2.4.0 2026-10-21 技能采用按固定批次、完整目录与项目策略核对；受阻和效果沿既有任务/复盘交接"
    - "2.3.0 2026-10-19 模板更新改为按 ROUND5 D20 的实际做法描述；增加 agent 配置（config.yaml）的有效字段和工具名规则（0.45.3）"
    - "2.2.0 2026-10-18 技能改为每个 agent 一个平级目录 agents/<agent>/.agents/skills/（ROUND5 R5.1）"
    - "2.1.0 2026-10-17 固定结构增加 io 元数据和'交接'一节；技能之间只通过工件协作；修改原因写进迭代记录（ROUND4）"
    - "2.0.1 2026-10-17 增加 io 元数据（读写哪些工件），用于检查技能协作断链（ROUND4）"
    - "2.0.0 2026-10-16 按核心 v6 就地重写（旧版使用已废弃的协议用语和输出字段）：技能决定 agent 的表现，修改技能要有统一规范才能长期稳定迭代"
---

# 编写技能

## 技能是什么
技能是一段给 agent 的工作方法说明。Kernel 根据技能开头的 `load` 规则，在合适的角色、阶段和场合自动加载它。
技能决定了 agent 的表现，也决定了多个 agent 之间、agent 与工件之间能否严谨地衔接。

## 模板与项目副本
- **公共模板**：kernel 维护的一套基础技能。创建项目时，Kernel 按模板的 load.roles 把它们复制到对应 agent 自己的目录 `agents/<agent>/.agents/skills/`
  （PM、Supervisor 各一份，同一技能可以重复），并在每个副本中记下
  来源 `template: {name, version}`。打磨公共模板是 kernel 自我升级的一部分。
- **项目副本**：复制之后归项目所有。项目在自己的运营和复盘中修改副本，可以大改甚至推翻模板；Kernel 只加载项目自己的副本。
- **模板采用**：现有副本只在项目已批准的精确目标策略下，通过 Kernel 技能更新批次采用；默认关闭，不再因版本变化直接覆盖。
  比较可信旧模板、项目当前目录与目标模板，覆盖正文、元数据及配套文件。缺旧基线或有本地修改时保留并提出合并/补证任务；
  合并候选须保留项目约定并有对应批准，不能把定制合并结果伪装成纯模板。项目主动删掉的技能不会被自动补回。
- **交接与效果**：用 list_skill_updates 读取本项目 update_id、固定清单、受阻原因和回执；只有维护执行器能改变采用状态。
  需开发/合并时，需求仍写目标与必须保留的行为；设计写基线、兼容条件及回退；build 交固定候选和完整目录差异；Review 核对同一候选。
  这些内容写进既有工件，不新增阶段或审批。仅有模板发布不代表项目允许采用；无配置路径/权限时交维护者，不让模型直接改策略或数据库。
  Operation 区分文件已应用、技能已加载/按需可发现、业务效果已复核；受阻项写原因、接手者和恢复条件，P/后续任务引用 update_id。
  回退/延期目标不盲目重试；修复后核对原问题。技能采用不搬迁业务目录，不删除运行数据，也不使旧任务批准自动失效。
- **加载方式**：`load` 写明场合的技能每轮自动加载全文；写 `on_demand: true` 的技能只在提示中列出名称、用途和路径，
  需要时用 `vault_read_text` 读取全文（适合不常用的领域方法，节省上下文）。

## agent 配置（config.yaml）
每个 agent 目录下的 `config.yaml` 只有这些字段生效：`schema_version: agent-config.v2`、`agent_id`、`agent_key`、`kind`、`status`、
`role`（默认 role.md）、`harness`（provider、model、reasoning_effort、context_policy、timeout_seconds）和 `tools`。
- `tools` 只能写当前的工具名（发布前的校验会拒绝未知名称，名称以校验报错里列出的为准）：它只能收窄执行器给的工具，不能扩大。
- 平台工具总会给，不用写：get_authorized_workspace、list_recent_history、list_iterations、map_lookup、list_credentials、
  get_credential，以及 PM 的任务文档工具。所以 Supervisor 写 `tools: []` 就是"只用平台工具"。
- 沙箱、能否写候选文件由 Kernel 按角色和阶段决定，不在配置里写；别的字段写了也不会生效，不要添加。

## evolution：审查自身角色与技能
只针对实际问题读取相关 agent 的 config.yaml、其 role 指定文件（默认 role.md）、项目技能副本和必要的模板差异；不全量重写。
role 表达职责与项目特别要求，skill 表达工作方法，章程表达长期业务约定；检查责任遗漏/重叠、冲突、不可用输入工具及无人接手的产出。
io 断链只是元数据线索，须核对阶段、适用条件和真实消费者，不能把可选/平台提供的输入判为缺陷。

分别判断文件存在、加载规则选中、按需可发现、全文实际读取、实际遵循和业务效果。用已有运行记录中的 skills/config_sha256
及证据核对版本；哈希不是内容快照，没有可靠旧基线就标未知，不能用当前 role/skill 解释旧回合。
检查上下文是否遗漏关键输入或加载无关内容；不能为了评分更好而删验收条件、扩大权限或隐去失败。

建议写目标路径/章节、问题证据、具体新文本或差异、必须保留的定制、受影响角色/阶段、验证和恢复办法。
evolution 仅在 improvement_proposal 提议；项目 role/config 的修改走用户批准的正常任务，不属于技能更新表的自动覆盖对象。
项目副本与公共模板分开处理，不代其他项目批准。修改后分别核对文件采用、后续回合加载/按需读取、行为和效果；
尚未采用或已回退时说明实际版本。审批、候选验证和权限仍由现有流程控制；本节不改变 build 的修改规范。

## 固定结构
```
---
name: 英文短名（小写字母和连字符），与目录名相同
description: 一句话：什么时候用、产出什么
metadata:
  version: "主.次.修"
  load: {roles: [...], stages: [...], purposes: [...], capabilities: [...], task_kinds: [...], on_demand: false}
  # stages 匹配 PM 的阶段回合，purposes 匹配其他场合（Supervisor 的 evaluate / review_input / consult 和分诊 triage），
  # 两者同时写时满足其一即可；roles、capabilities、task_kinds 必须满足；没写的键不限制
  io: {reads: [工件名, ...], writes: [工件名, ...]}   # 可选，用下面的工件名；复盘据此检查协作断链
  template: {name: 模板名称, version: 复制时的模板版本}   # 仅项目副本有，由 Kernel 写入
  changelog: ["版本 日期 改了什么、为什么（引用改进建议编号）"]   # 公共模板必写；项目副本可省略（原因在迭代记录里）
---
# 标题
## 目的（为什么要这样做）
## 何时使用 / 不适用
## 输入（读哪些工件的哪一节）与事实优先级
## 做法（编号步骤，每步可检查）
## 产出（写到哪个工件的哪个章节）
## 交接（下一个读它的技能或角色需要什么）
## 自检清单
## 常见错误（反例）
```
不需要的章节可以省略，但顺序不变。

**io 的工件名**（与 assistant-task-continuity 一致）：task_contract · solution_design · implementation_report ·
review_report · improvement_proposal · domain_brief · working_notes · charter · map · jobs · skills；
平台提供的（只读）：digest（运行记录）· change_stream（变更流水）· state_ledger（状态簿）· user_profile ·
template_updates · shared_context · user_answers。
**技能之间只通过工件协作**：A 写出的东西，必须有 B 在"输入"里读；改了某个工件的章节名或格式，就检查所有读它的技能。
复盘材料里的"技能协作断链"列出有人写没人读、有人读没人写的工件，是检查交接问题的起点。

## 写法原则
1. **一个技能只管一类工作**；同一件事只在一个技能里规定，其他技能引用它的名字，不要复制。
2. **说明为什么**：每条要求后面用半句话写原因，agent 理解原因后才能处理没写到的情况。
3. **可检查**：用"每个 R 至少有一个 A"这类可核对的要求，不用"尽量完善"这类说法。
4. **与工件对齐**：产出部分写明工件和章节名称（以 assistant-task-continuity 为准），改了章节名称就要同步修改所有引用它的技能。
5. **给反例**："常见错误"写真实出现过的问题，最好来自复盘记录。
6. **控制长度**：一个技能通常不超过 3000 字；太长说明它管的事太多，应该拆分。
7. **用用户的语言**：中文、具体、少用术语；不要出现已废弃的旧流程用语。
8. **不和合同冲突**：输出 JSON 的格式和可选的决定由 Kernel 规定（"输出要求"），技能不能增加或改变字段。

## 修改流程
1. 复盘或用户反馈提出问题 → 改进建议写明要改的技能、章节和新写法，以及原因。
2. 用户批准后作为任务实施：改技能文件，版本号加一（模板更新的判断依赖版本号）；原因写进实施报告的迭代记录
   （公共模板另在 changelog 写一行）。
3. 项目只改自己的副本；公共模板的修改属于 kernel 项目的任务。kernel 打磨模板时参考各项目的复盘建议和它们对副本的改动：
   多个项目都做了同类修改，往往说明模板本身该改。
4. 采用模板更新时，逐段比较模板新版与本项目副本：本项目特有的修改保留，模板的改进合并进来，在 changelog 写明
   "采用模板 X.Y.Z 的哪些部分、保留了哪些本地修改"，并更新 `template.version`。
5. 修改后观察下一次复盘的记录，确认问题是否改善。
