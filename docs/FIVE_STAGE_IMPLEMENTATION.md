> 历史版本记录：其中 Butler/intake 的入口描述已失效。当前退役规则见 [KERNEL_MANAGEMENT_MERGE.md](KERNEL_MANAGEMENT_MERGE.md)；本次未部署生产。

# 五阶段项目框架实施报告 · 0.25.0

基线：用户确认的“五阶段项目框架：主干优先实施版”，以及本轮开始时的 **0.24.9 工作区快照**。本轮不把 Git HEAD 当成现网基线，也不覆盖此前已存在的未提交改动。

交付状态：代码、角色、Skills、接口、迁移及回退已实现，401 项完整回归通过；待发布安装包的 101 个源文件逐一 SHA 核对一致。生产部署、正式能力规则切换和真人 Discord 验收属于最后批准后的动作。未增加数据库表、容器、常驻模型、Operator、恢复 Agent、事件总线或调度服务。

## 业务主线与权威

~~~mermaid
flowchart LR
  R[需求分析] --> RA[用户批准需求]
  RA --> D[方案设计]
  D --> DA[用户批准设计]
  DA --> B[实施与自测]
  B --> V[独立只读 Review]
  V --> P[发布与激活]
  P --> O[运营任务与周期报告]
  D -->|需求变化| R
  V -->|实现问题| B
  V -->|设计变化| D
~~~

每个完整 PM 回合，包括提问及失败结算，都先经过 Supervisor 再允许下一 PM 回合。问题立即投递，答案与问卷草稿先保存；监督屏障和待答问卷可以同时存在。监督失效时暂停，保留实际效果与恢复入口。

Task 的 request 是阶段和批准权威；Project 元数据只保存 active_task_id、operational_baseline_ref 和报告窗口。发布成功的原 Task 冻结，运营工作使用独立 Task，继承经验证的已发布需求与设计。后台 Python 程序继续使用原 runtime 队列，不占据 PM 主线。

管家新入口使用 kernel Project 的 intake，项目 PM 和 Supervisor 保持同级；旧 system 身份、历史路径和相关回复保持读取兼容。Kernel 自升级也走项目主线，最后发布始终单独批准。

## 角色、阶段能力与输出

公共 PM Role 配合 requirements、design、build、audit、operate 五个 profile；repair 只作为 build 的读取别名。profile 分别绑定 required/available Skills、具体工具和 MCP server。Loader 校验选择是否位于已发布基础配置的能力上限内；Supervisor 提示词不能创建权限。

阶段对应产物：

| 阶段 | 文档 schema | 阶段约束 |
|---|---|---|
| 需求 | task-contract.v1 | 关键未知关闭或明确批准其处置，保存验收要求 |
| 设计 | solution-design.v1 | 绑定已批准需求 SHA，列出实施、测试、恢复、发布目标和运营动作 |
| 实施 | implementation-report.v1 | 只实施当前批准内容；候选与正式文件分离；提交真实自测证据 |
| Review | review-report.v1 | 新只读物理会话；核验需求、设计、实现与测试一致性 |
| 运营 | operation-report.v1 | 只执行已注册、批准且脚本 SHA 固定的动作，汇总实际效果与反馈 |

Review 的规则改进提案另存 review-improvement-proposal.v1，本轮验收规则不随建议变化。返回需求或设计阶段会撤销受影响的批准及旧验证、Review、发布准备结论，保留候选和历史证据。

最终发布批准卡附实际密封文件 diff、旧/新 SHA、验证回执和 Review 报告。Review 发现高影响风险可收紧为 requires_final_approval，无法放宽已发布配置或原设计门槛。

统一输出为 agent-result.v1，公共字段为 outcome、summary、questions、approval_requests、artifact_refs、evidence_refs、checkpoint、payload，可附 delegation_results。阶段内容在 payload。Kernel 绑定实际身份、阶段和版本，拒绝模型输出覆盖控制字段。

用户收到摘要、独立交互卡和 Markdown 附件；JSON 留作归档与校验。多问题问卷沿用现有问卷翻页、草稿、提交和相关性机制。无活动 Task 时 Supervisor 可只读咨询；忙碌或发布中也只能只读咨询，不能改变在执行的主线。

## 监督、失败和并行

Supervisor 是无状态短调用，通过 DB、有效文档、已结算 Turn、跨 PM/Supervisor 入口的新语义消息及 Brief 重建上下文。取消单聊天框过滤，沿用消息 ID、pending/consumed、state_revision、context_epoch 和问卷 revision。

每轮 supervisor-brief.v1 区分项目评估与用户意图：确认事实、证据、阻塞、决策、下一步；推测保留原始来源、置信度及默认七天期限。推测不构成授权，不进入公共背景，也不作医学或心理诊断。失败 Brief 明确标记评估失败。

格式纠正最多一次；Supervisor 暂时性失败使用原 Run 的初次调用加三次退避重试（30/120/600 秒），并受原项目预算与超时约束。仍失败给确定性恢复卡。恢复只重新评估与补投已有消息，不重做已完成业务动作。重启补齐仍在既有 maintenance loop 内。

一个 Project 同时只有一个激活 PM Task。任务排队或在安全边界挂起，保存 checkpoint、候选、批准与原因；恢复核对受影响基线。仅保留一次中断恢复关系，不建立嵌套抢占。严重事件先执行已批准保护规则，再排队/挂起，不能凭模型判断创造保护授权。

阶段内部最多三个临时原生子 agent，并行写集必须分开，必需结果汇总后推进。任务说明属于行为约束；原生子 agent 的继承权限不会被描述为强隔离。

## 项目导航、事实和背景

沿用 Project Map 的完整不可变代次。逐文件字段包含 URI/路径、用途、SHA、最后变化/观察时间、增加/删除行数、diff 引用、最近 change_id。正式变化在结算后更新；候选仅记录 Task manifest 和候选 change，不伪装正式效果。明确注册的 Kernel 语义文档进入 Map 最新文档头，旧 revision 保留在 Vault 与变更账本。

按进入项目、读取相关文件、发布前和手动刷新核对 SHA，不增加文件监听器。外部人工改动原因未知就记录未知；共享文件沿用共享写锁和旧 SHA 检查。Map 失败保留完整旧代次并标记 stale/pending，维护补齐只更新账本和导航，不重跑发布。

变更原因使用现有 messages 的 system/structured 记录，保存时间、Project、Task/Run、文件、操作、原因、来源与实际效果校验；diff 存 Vault。MCP 支持最近 N 天及 diff 读取，Supervisor 可有界读取最近 30 天；长期有效需求与批准不因时间窗口失效。Map 覆盖已管理来源和明确登记的文档，不递归遍历所有父目录 Grant。

公共文档位于 kernel/background/user-background.json 和 development-environment.json，只含用户确认且带日期、来源的事实。自动上下文由 Project.background_fields 指定需要的字段；MCP 支持按需检索。撤销后过滤对应来源，更新后续上下文并更换旧原生会话；历史保留为受限审计，彻底删除另开具体任务。

新项目复制 five-stage.v1 固定基础模板并记录来源；公共模板更新不自动覆盖现有项目。模型配置、Grant 和项目业务 Skills 在迁移中保留。

## 运营和接口

run_project_action 只能在 operate 阶段派发已发布 runbook_actions 中的固定 Vault 脚本。执行前重新核对真实批准消息、脚本 SHA、输入 schema、当前 Scope、阶段和运营基线。高影响动作批准卡绑定 action、baseline、inputs 的哈希，实际批准消息一次使用。相同运营工作回合的同一动作及输入换 token 重试仍返回原 Run。

周期报告由既有调度工具触发受保护的 POST /v2/projects/{project_id}/operations/reports，使用明确 UTC 窗口及 IANA 时区去重并排入 PM 主线。动作接口为 /operations/actions。系统不擅自设置用户未确认的周期、操作脚本或外部服务。实施阶段按批准设计生成 schedule/workflow 定义，原调度设施负责运行。

关键实现位置：

- 控制与批准：repositories/core.py、services/task_queue.py、services/run_scheduling.py
- 模型与能力：services/run_processor.py、agent_results.py、agent_files.py、project_directory.py
- 监督和交互：goal_supervisor.py、supervisor_processor.py、discord.py、worker/main.py
- 文档与导航：document_runtime.py、semantic_map.py、project_changes.py、file_effects.py
- 公共事实与运营：public_background.py、operations.py、api/operations.py、mcp_server/scoped_tools.py
- 迁移：scripts/migrate_five_stage.py；部署复用 scripts/Deploy-AssistantKernel.ps1

## 迁移、备份与发布

本轮开始备份位于 H:\AI\assistant-data\backups\five-stage-20261004-114405-0249，包含 0.24.9 工作区、配置、管理文档、PostgreSQL dump、现用版本指针和 SHA 清单。迁移脚本默认只预览；正式执行仅在 stopped/settled 安全边界，创建本次目标文件与 Task 的专用备份/回退 journal。

迁移保留 Task、需求问卷、草稿、答案和可验证批准；未完成任务缺设计批准就补门槛。过时发布卡保留为历史并失去控制效力，不能通过旧卡绕过设计。只转换一条活跃流程，不维护双写或影子执行。

安装包、源文件 SHA 和相对 0.24.9 工作区的差异保存在 H:\AI\assistant-data\runtime\releases\0.25.0。新版本 venv 可预装核对，但 kernel-current.txt 仍保持 0.24.9，部署脚本未获最终批准不会调用。

自动部署失败会停止新服务，回退迁移、版本指针及 Supervisor 配置，再启动旧版；如迁移后已经产生新业务效果而回退校验不通过，则保留事实、保持停止并提供明确人工恢复入口。

## 验收证据与范围

| 用户要求 | 验证 |
|---|---|
| 五阶段与两次批准完整流转 | test_five_stage_flow：隔离 PostgreSQL，真实候选写入、Python 自测、Publisher、运营报告 |
| 返工接续，审批和验证有效性 | flow、core_supervision、task_queue、context |
| 每轮监督、失败暂停、有限恢复 | supervision、core_supervision、worker_hooks、既有 Supervisor 回归 |
| 双入口、多问题、草稿及重启恢复 | questionnaire、supervisor_event_pipeline、原 core4 渠道回归 |
| 子任务与 PM 串行、独立后台脚本 | profiles、task_queue、slimming、runtime_v2；原生权限局限留证 |
| 低影响批准复用，高影响最终批准 | flow、context、operations、capability_lifecycle |
| Map、diff、原因、人工修改发现 | five_stage_map、semantic_map、document_runtime |
| 用户背景、撤销及原生会话更新 | public_background、five_stage_context |
| 固定模板与管家兼容 | profiles、migration、原 Project/Route 回归 |
| 运营动作、窗口去重、报告闭环 | operations、flow、task_queue、worker_hooks |
| 存量安全转换与回退 | migration 隔离数据库与文件树测试；生产仅预览 |

模型输出与用户点击使用确定性测试替身；数据库、文件、审批关联、哈希校验、候选发布及离线脚本是真的执行。不得把这些测试描述成真人 Discord 或真实外部业务验收。

最终登记（2026-10-04）：

- 完整回归：**401 passed in 400.49s**；日志 H:\AI\assistant-data\runtime\five-stage-tests-final.log。
- 包/源码一致性：**0.25.0 / core5.v7，101 个文件 SHA 全部一致**；installed-parity.json 位于发布目录。
- 依赖、Python 编译、PowerShell 部署脚本解析和 diff 空白检查通过。
- 存量预览：3 个项目、1 个待答任务，applied=false；隔离迁移/回退专项 4 项通过。
- 安装包 SHA-256：a5d9c20c44cd734832ca0dc21c3fd3acacc437bc40717ccbf076ba1ebf512634。
- 待发布 venv 已预装，源版本已升至 0.25.0；生产 API 与 kernel-current.txt 仍为 **0.24.9/core5.v7**。

剩余步骤是按用户原计划单独批准 Kernel 自升级及正式角色规则升级，再执行停服安全边界迁移、现有宿主部署脚本和生产核验。真人 Discord 问卷点击与真实业务运营验证仍待完成；本报告不以隔离模型替身代替这些结果。

## 2026-10-05 整合部署更新

0.25.0/core5.v7 已部署，大管家管理入口、公共模板与背景归 kernel；旧 butler-maintenance 归档为 merged。内核各服务正常，源码/安装包 102 文件一致。已有 trading-risk-bot 仅转换公共五阶段配置，业务文件与待答问题保留。原文 2026-10-04 的未发布状态是历史记录；以本节和部署回执为当前状态。
