# 统一设计候选稿 v1.0 落地实施方案

> 编制日期：2026-10-06。基线：src 0.25.0 / core5.v7 工作树。
> 规范来源：《Kernel 基础版本统一设计报告候选稿 v1.0》（A–H 卷）及其 2026-10-06 审计结论。本文件只做实施映射、排序与验收，不构成第二套规范；条款冲突时以候选稿正文为准。
> 性质：计划文档，不代表运行授权。遵循候选稿 B.5 原则：**每一子阶段先向用户展示具体差异、影响、验证与回退，获准后实施**；不长期只建基础设施。
> 本文件为新增文档，编制过程未修改任何现有文件。

## 已拍板决策（2026-10-06，实施全程有效）

| # | 决策 | 内容与推论 |
| --- | --- | --- |
| D1 | n8n 回调不设独立鉴权 | 不新增 HMAC / per-schedule secret。回调端点挂 v2 router，自然继承现有 Bearer `kernel_api_token` 保护（对 n8n 只是凭据引用携带一个 header，零新增机制）。功能正确性靠三道身份核验：①`schedule_id` 必须命中已注册且 enabled 的定义；②Kernel 有界 GET n8n 执行元数据核 workflow/version/deduplicationKey 计划时间，缺 metadata 停派；③受理短事务与开进程前核启动窗口/控制状态。伪造回调因查不到对应 n8n 执行记录而停派。`settings.py:39` 的 `n8n_callback_secret` 从未使用，标注退役（保留字段防配置报错，注释说明不生效） |
| D2 | 允许按需新增数据库表 | 候选稿第 9 行"不是要求新增数据库表"按审计结论改读为"不新增控制服务/第二套审批状态"。本方案新增三张最小表：`assistant.question_groups`（按题单次接受的原子约束）、`assistant.schedules` + `assistant.schedule_triggers`（tick 跨 revision 唯一索引）。迁移走 `migrations/upgrade_*.sql` 既有模式，schema core5.v7 → v8 |
| D3 | 版本切分/发布节奏暂不决策 | 本方案只按依赖排序到子阶段粒度；每个子阶段独立可验收、可回退。版本号、打包与上线节奏由用户在首个子阶段验收后另行决定 |
| D4 | 公共背景跨项目保存的执行写者 | 普通项目会话只能提交申请（附可信原消息），写入由 Kernel Project 维护流程/Kernel 控制层执行；普通 PM 无 kernel vault 写权（候选稿 A.5 缺口的补全，审计确认） |
| D5 | 双渠道双入口能力边界 | 飞书为完整交互镜像（草稿共享、按题置灰、卡片刷新）；Discord 副本首版降级为只读提示 + 入口指引，不做交互镜像（审计确认的渠道能力差异） |

## 总体排序逻辑与依赖图

排序原则（用户指定）：**先被依赖的模块；先显而易见且不影响其他模块的独立改动**。据此把 B.5 的包1–4+收口细分为 6 个阶段、22 个子阶段；每个子阶段是独立可合入、可回退的最小单元。

```text
阶段〇 独立修复（无前置，立即可做，回归面小）
  0.1 raw 行 DTO 化（安全修复）
  0.2 Python attempt 输出隔离
  0.3 进程组回收 + 清理回执落库
        │
阶段一 权限与上下文基座（=B.5 包1；权限模型被阶段二/三/四全部执行者共用）
  1.1 ctx/usage_unknown 超限续接链退出 ←────────┐
  1.2 grant pin-at-creation 截断移除            │无代码依赖，
  1.3 读向技术降权拆除（Supervisor sandbox 等）  │可并行推进，
  1.4 PermissionPolicyCompiler 摘除 + 物化善后   │按风险从低到高排序
  1.5 intake 创建/扩展一次目录确认               │
        │
阶段二 交互与前台主线（=B.5 包2）
  2.1 QuestionGroup 状态模型（新表+控制函数）← 被 2.2/2.3/2.4 依赖
  2.2 Prompt 确认门槛（prepare→confirm→dispatch 链）
  2.3 渠道投影与双入口镜像（飞书完整 / Discord 只读）
  2.4 interaction-followup skill + Supervisor 子代理链
  2.5 Snapshot 槽位扩展 + Map JSON 化 + Review 需求优先
        │
阶段三 受管 Python 与定时（=B.5 包3）
  3.1 python-script-manifest.v1 + submit_project_python（依赖 0.2/0.3 的 attempt 与回收）
  3.2 环境引用（environment_ref 最小实现）
  3.3 schedules / schedule_triggers 迁移（被 3.4 依赖；被 4.3 周期汇总依赖）
  3.4 n8n adapter + 回调端点 + 启停状态机
  3.5 定时语义校验 + 专项场景测试
        │
阶段四 公共背景与运营（=B.5 包4）
  4.1 公共背景单源转换（无前置，可与阶段二/三并行）
  4.2 Project 范围文档服务
  4.3 观察/候选/规则引擎 + 通知包装（周期汇总部分依赖 3.4；事件触发部分不依赖）
  4.4 通知路由三层 + 发送前重核
  4.5 体系迭代闭环（采用记录/效果观察）
        │
阶段五 收口（存量迁移与 E2E，全部前置完成）
```

横切依赖说明：2.3 的 Renderer `kernel_notice` 分支会被 4.4 复用（一次实现两处受益）；1.1 完成前阶段二的会话连续性行为有旧 rotation 链在跑，无阻塞但验收口径以 1.1 合入后为准。

---

## 阶段〇：独立修复（显而易见、不触碰其他模块）

### 0.1 run_status / project_task_status / queue 返回 DTO 化

- **问题**：`run_status`（scoped_tools.py:724-745）把 `SELECT *` 的 runs 整行原样返回给模型，含 `claim_token`、`executor_json`、`worker_instance_id`、`process_pid`（core.py:2133-2136）；`project_task_status`（task_queue.py:440-459）同样返回 runs 整行 + queued messages 整行；`queue_project_task` / `request_operation_report` 返回 messages 整行（含 worker 内部字段）。
- **变更**：新增白名单视图 DTO（`run_id、status、attempt、outcome、created_at/updated_at、blocked_by、error 摘要、队列位置`），敏感字段不进模型可见面；按候选稿 F.6"受限审计"原则，敏感字段保留在 Kernel 内部查询路径。检查 roles/skills 提示词中对上述工具返回字段的引用并同步措辞。
- **新增**：无（scoped_tools 内视图函数 + 对应 schema 类型）。
- **验收**：新增负例测试"DTO 不含 claim_token/executor_json"；test_runtime_v2 / test_core4 投递系列回归绿。
- **回退**：纯视图层变更，revert 即可，无数据影响。

### 0.2 Python attempt 输出隔离

- **问题**：业务脚本 stdout/stderr 以 `"wb"` 重开（worker/main.py:169-173），重试覆盖旧 attempt 日志；对比 Codex 侧已有按 attempt 归档（invocation_id `f"{run_id}-a{attempt}-{claim_token}"`，run_processor.py:1857）。
- **变更**：输出目录改 `runs/<run_id>/attempts/<n>/{stdout.log,stderr.log,result.json}`；旧路径保留兼容读。
- **验收**：同一 Run 两次 attempt 后旧日志仍可读；test_slimming 回归。
- **回退**：revert；新旧路径双读窗口内无破坏。

### 0.3 进程组回收 + 清理回执

- **问题**：`terminate_process_group` 只等直接子进程（process_control.py:20-21），父退出即当组空；无按 pgid 的剩余成员盘点；维护循环丢弃清理返回值（worker/main.py:276-280）。已有 PID/start ticks 复用防护（process_control.py:40-60）可复用。
- **变更**：TERM 后按 pgid 扫描 `/proc` 盘点剩余成员（复用 start_ticks 校验防 PID 复用误杀），有界等待→KILL→复查；返回结构化 cleanup receipt（members_before/after、信号、耗时）；worker 将 receipt 附到 attempt 结算记录（0.2 的 attempt 目录内 `cleanup.json`）。
- **验收**：新增用例——孙进程（进程池 worker）残留、TERM 被忽略、PID 复用不误杀（test_process_control 现未覆盖这三类，审计确认）。
- **回退**：revert；行为回退为现状（父退出即视为清理完成）。
- **不做**：不引入 cgroup/rlimit 硬限制（候选稿明确"预算不是 OS 硬限制"，资源声明属 3.1 的 manifest）。

---

## 阶段一：权限与上下文基座（B.5 包1）

### 1.1 ctx/usage_unknown 超限续接链退出（I02，双点一链）

- **前置**：CLI 现场验证门禁——隔离环境用生产同版 codex（报告实测 0.157.1）跑长上下文压测：`model_auto_compact_token_limit` 生效（透传代码已在 harness/codex_cli.py:137-151）、同 native_session_id 的 `exec resume`（228-246）连续性正常。**未通过本门禁不上线本步**。
- **变更**：
  1. `_session_needs_rotation`（run_processor.py:824-834）：删除 `usage_known is False`（829-830）与 ctx 比例（831-834）两类触发；仅保留 `status=='rotation_required'` 旧行兼容读与上层显式请求。
  2. 触发链（1905-1937）：`context_rotation` 原因退出；`public_background_changed`、`interrupted_native_turn`、`config_changed/config_compatibility_unknown`、`supervisor_requested` 保留。
  3. 摘要调用（1138-1157）：保留原因的 handoff 一律走确定性 fallback（1159-1176 已存在），**全链不再为会话接续调 LLM**（候选稿 A.4"必要新会话从有效业务 Artifact 与来源重建"）；checkpoint packet（lineage/coverage/content_sha256，1195-1244）继续生成，供 core 校验。
  4. `core.ensure_session` 第二份判断（core.py:5723-5737）同步删 ctx/usage_unknown 分支；5745 的"无 checkpoint 抛错"防线保留给显式 rotation 路径。
  5. `assistant-session-rotation-handoff` Skill 标记退役为历史（不再作为摘要 system_prompt）。
  6. settings.py:80-82 的 `context_soft_ratio/hard_ratio` 保留为观测字段（ContextBudgetManager decision 仅 ratio 入 usage_snapshot；api/main.py:256-260 诊断端点保留），文档标注"不驱动任何换会话动作"。
  7. sessions 表 `status='rotation_required'` 旧行兼容读保留。
- **验收**：Codex / PM / Supervisor 三链路回归（候选稿 L1 §5.3 明确要求"共同进程控制修改还需回归三链路"）；构造超软阈值会话不再换会话、原生 compact 接续；usage_unknown 会话下次 exec 正常 resume；既有 test_core4 恢复/投递系列绿。
- **回退**：revert 代码即恢复旧行为；handoff 产物为纯增量文件，无数据回滚。

### 1.2 grant pin-at-creation 截断移除

- **问题**：`active_file_roots` 按传入 run 的 created_at 截断 `activated_at <= cutoff`（project_grants.py:50-64），长生命周期根 PM Run（承载逻辑 Task）永远看不到创建后扩展的目录授权。
- **变更**：去掉 cutoff 过滤，每次调用读当前 active 授权；`pinned_at_creation` 链（workspace_scope.py:204-206）改为仅记录审计快照（当时可见 roots 留档），不再作权限过滤。执行中包的 Prompt/绑定不变（"包不回写"），授权变化下次安全边界生效。
- **验收**：fixture——根 Run 存活期间新增 grant，下一调用可见；旧 Run 的 executor_json 快照不变。
- **回退**：revert 即恢复截断语义。

### 1.3 读向技术降权拆除（本包风险最高步，最后做）

- **变更**：
  1. `goal_supervisor.py:908` 的 `sandbox="read-only"` → 按 Project 实际 CLI 配置装配（与 PM 同源）。
  2. `execution_plan.py:310-313` `formal_read_only`（candidate_only or manager kind or requirements/review 阶段）停止驱动 scope 强制，降为 `stage_permissions` 内的信息性标签（365-378 保留）。
  3. `workspace_scope.py:232-250` candidate_only 时强制全 root `access="read"`+`write_mode="gated"` 的逻辑收窄：仅 implementation/candidate 流程保留 gated 写；requirements/review/manager 的强制读移除。
  4. **明确不动**：`write_candidate` / `delete_candidate`（workspace_scope.py:618-696）、candidate 发布链（发布 SHA/版本/批准校验）完整保留——拆的是技术降权，不是 candidate 业务流程（候选稿 L3 公共角色正文仍要求"正式修改走 candidate、验证、Review 和 Publisher"）。
- **验收**：Supervisor 会话可整理文件草稿；requirements/review 阶段文件读写不被 scope 拦截；test_five_stage_* 全绿；行为约束转由 Role 正文承担（0.25.0 的 butler/goal-supervisor/project-agent.md 已按候选稿更新，本步一起复核措辞）。
- **回退**：分两小步合入（先 Supervisor sandbox，后阶段降权），各自可独立 revert。

### 1.4 PermissionPolicyCompiler 摘除 + 物化善后

- **前置**：1.3 已合入（否则编译产物仍被消费）。
- **变更**：
  1. `ProjectResourceResolver.materialize`（execution_plan.py:344-353）不再调用编译器；plan.bindings 停止写入新的 `native_permission_profile(_sha256)`。
  2. 历史 Run executor_json 中该字段的不可变校验（core.py:512/2741）保留兼容读。
  3. per-project CODEX_HOME 已物化的 `{profile}.config.toml`：保留不删，写入 legacy 标记（materialize 处生成名单），与"保历史配置/读取解释"一致。
  4. `get_authorized_workspace` 返回真实完整权限包与加载状态（不再静默把 Supervisor 根读化；scoped_tools.py:226,247 的 native_mode 消费点同步为兼容读）。
  5. permission_policy.py 整体标记 deprecated（仅历史配置解释）。
- **验收**：全仓 grep 无编译器新调用路径；verify_core4.py 通过；一条真实 PM 会话端到端正常。
- **回退**：revert 恢复编译（物化文件未删，无破坏）。

### 1.5 intake 创建/扩展一次目录确认

- **变更**：project_directory.py / source_scope.py 的创建提案扩展为一次展示：自有目录默认完整包（`directory-full.v1` 语义）、外部目录与父目录递归范围、主要文件操作整体包、实际执行环境；无外部目录为有效选择。落库复用 `assistant.project_grants`（grant_kind='file'，subject_kind='project'，长期 active）。后续扩展一次申请、全员生效、主动撤销；阶段/Task 结束不撤（1.2 已保证读取侧）。`ProjectDirectoryAgreement` 作为读取/审计视图（不新增许可表）。
- **验收**：fixture 全链——创建确认→全员共享→一次扩展→撤销阻断新工作；`migrate_project_grants.py` 语义核对（旧导入不受影响）。
- **回退**：intake 流程层变更，revert 无数据影响。

---

## 阶段二：交互与前台主线（B.5 包2）

### 2.1 QuestionGroup 状态模型（被 2.2/2.3/2.4 依赖，先做）

- **选型**：新表 `assistant.question_groups`（group_id PK、project_id、owner_kind/owner_id、task_id nullable、status、content_revision、questions jsonb、shared_draft jsonb、answers jsonb、supersedes_group、时间戳），答案的"按题单次接受"用 `UPDATE ... WHERE status='open' AND content_revision=$expected` 原子完成；投影（飞书/双入口副本）复用 messages 既有投递链（message_type 扩展 + group_ref/view_revision），不建第二投递状态。**理由**：按题原子接受与版本拒收需要 DB 层约束，kernel_meta JSON 做不到并发安全；D2 已允许加表。凭据专用输入不入公开镜像（交互专项规则）。
- **控制函数**（候选稿 F.6 签名照抄）：`publish_question_group`、`record_question_control`、`record_semantic_text`、`commit_interaction_followup`——落 services/question_groups.py + repositories/core.py 方法，v2 API（Bearer 保护）暴露给渠道回调，不暴露给模型。
- **验收**：候选稿 L2 §6.2 十条逐条测试——按题单次接受、部分答只解对应题、独立组不互废、重复/旧版本拒收不产生第二答案、更正保替代关系、按源及提交顺序迟到不覆盖、监督在途可答不直接排 PM。
- **回退**：新表 + 新服务，旧 questionnaire.py 路径保留期间可灰度切换。

### 2.2 Prompt 确认门槛（prepare→confirm→dispatch）

- **问题**：现状 Supervisor"安全计划"存在直接排队 PM 的路径（候选稿 L3 §1 已核差距）。
- **变更**：新增 `prepare_pm_prompt / record_prompt_confirmation / dispatch_pm_turn / settle_pm_turn / commit_supervisor_assessment`（F.6 签名）；Supervisor 建议一律走"固定并展示具体包 → 绑定真实确认（明确交办可确认本轮，实质新增须展示）→ 派发前重核 owner/输入 head/批准/能力 → 同包 queued"（候选稿 L3 §7.3 顺序，I03 修正后的规范）；`queue_project_task` 排队不再自动授权新 PM 回合；ActionPlan 固定版本、不读 latest 重试。
- **验收**：supervisor 建议不自动派发；明确原指令确认本轮；混合输入分部处理；"普通答案不批准未展示 Prompt"负例。
- **回退**：新门槛层包住旧入口，开关可先只对新建会话生效，验证后全量。

### 2.3 渠道投影与双入口镜像

- **变更**：飞书侧——卡片草稿共享刷新（选择/填写/翻页同步共享 draft revision，另一边刷新）、按题双边结束置灰、旧按钮 `record_question_control` 校验 expected_revision 拒收且不转自然聊天；PM 普通问题完整镜像主界面、Supervisor 自有问题主界面为主 PM 仅位置提示。Discord 侧按 D5 只读降级。Renderer 增加 `kernel_notice` Ref/kind 分支（4.4 复用）。
- **验收**：候选稿 G"双边分页/部分文字回答"场景 + L2 §6.2 全表在真实飞书卡片上走查；"DB 立即拒旧操作、平台尽快同步、不承诺瞬时置灰"如实标注。
- **回退**：投影层变更，逻辑组状态不受影响。

### 2.4 interaction-followup skill + Supervisor 子代理链

- **变更**：新建 `assistant-interaction-followup` Skill（正文=候选稿 L3 §4.14 全文）；`assistant-prompt-assembly`（正文已存在于候选稿 L3 §5.1）作为 Supervisor 委派子代理的执行入口接入 goal_supervisor 委派机制；复核触发仅真实新输入/完整回合/关键事实，投影翻页投递不触发。
- **验收**：明答与倾向分开；建议不假答案；child 不发卡不正式接受；stale 结果不覆盖新答。
- **回退**：Skill 文件与委派入口均为增量。

### 2.5 Snapshot 槽位扩展 + Map JSON 化 + Review 需求优先

- **变更**：
  1. Snapshot 装配补显式槽位：最新输入/决定/授权动作/最近交互/在途/依赖/未确认需求 Refs；事实与评估时点、7 日假设期限分开（候选稿 L2 补清 6）。
  2. Map JSON 化：`knowledge/map/` 从 YAML 活动格式转 `generations/<gen>/{index,manifest}.json + latest.json + stale.json`（semantic-map.v2）；一次安全边界转换脚本 + 双读窗口；`relevance/criticality/kind`（type 兼容 alias）字段保留。
  3. Review 需求优先：`assistant-release-audit` Skill 正文对齐候选稿 L3 §4.7——以最终已确认需求+有效修改为首要基线，逐项正向覆盖并反查遗漏；缺实现回 Build、需求变化回 requirements、设计变化回 design、纯证据缺口补证再审。
- **验收**：B.5 包2 整行——"真实需求→方案批准→Build→Review→适用发布验活→同 Task 关闭；需求更改返工；监督失败停，不重业务"；Map 转换后旧 YAML 只读可查。
- **回退**：Map 保留旧 YAML head 直到 JSON 链路验证通过才切（转换脚本幂等）。

---

## 阶段三：受管 Python 与定时（B.5 包3）

### 3.1 python-script-manifest.v1 + submit_project_python（依赖 0.2/0.3）

- **变更**：manifest Schema（候选稿 F.1 定义，字段=代码包/入口模块闭包/环境 Ref/fixed_args/输入输出 schema/resources 三层并发（sequential|io|cpu，默认 IO 8、CPU 池 2、库线程 1）/children 责任/ownership/provenance）；新 MCP 工具 `submit_project_python(manifest_ref, inputs, idempotency_key, execution_kind=auxiliary|trial|formal)` 分别核本轮辅助边界/试跑批准/正式授权；内部函数 `admit_managed_python / validate_python_start / settle_python_attempt`（F.6 签名）。执行复用现有 runtime 队列与 shared-write 互斥（api/v2.py:51、run_scheduling.py:13-15）。输入物化 `input_bindings` 校验位置（防错非沙盒）。新建 Skill `assistant-python-execution`（正文=候选稿 L3 §4.12）。
- **验收**：候选稿 L2 §5.1 Python 行——简单脚本/IO 采集/CPU 父先失败（池 2/库 1、父退不假组空）；每 attempt 独立日志结果；`EFFECT_UNKNOWN` 错误不引导盲重试。
- **回退**：新工具新链路，不影响既有 run_project_action。

### 3.2 环境引用（最小实现）

- **变更**：`environment_ref` = 解释器指纹 + 依赖清单 + 版本指纹的记录与启动前核验；不匹配即停受影响执行；依赖变更生成新 Ref，旧 Ref 不指向新依赖（不在旧 Ref 下 pip 换版）。**不做**多环境并存管理器（anti-over-engineering；需要并存时另批环境版本，候选稿 L2 §4.1.2 原文）。
- **验收**：改依赖后旧 Ref Run 启动被拒并提示；新 Ref 正常。
- **回退**：校验层增量，可放宽。

### 3.3 schedules / schedule_triggers 迁移（被 3.4 与 4.3 依赖）

- **DB 变更**（migrations/upgrade_core5_v8.sql，schema → core5.v8）：
  - `assistant.schedules`：schedule_id PK、project_id、definition_ref/content_sha、revision、control_state（registered|enabled|paused|retired）、expected_version、activation_receipt jsonb、readback jsonb、effective_boundary、时间戳。
  - `assistant.schedule_triggers`：trigger_id PK、schedule_id、scheduled_for_utc、revision、source_execution_id、decision（accepted|skipped|duplicate|rejected）、latest_start_at、run_id、received_at；**UNIQUE(schedule_id, scheduled_for_utc)** 承载 tick 跨 revision 唯一（候选稿 L2 §4.1.3 规范的数据库落地）。
- **验收**：迁移幂等；唯一索引并发插入选型测试；verify 脚本接受 v8。
- **回退**：标准迁移回退脚本（参照 v5 先例）。

### 3.4 n8n adapter + 回调端点 + 启停状态机

- **变更**：
  1. `services/n8n_adapter.py`：从定义派生两节点 Workflow（ScheduleTrigger→HTTP Request，不两边独立改规则）；create/update（停态注册）、publish 指定 versionId、pause/unpause + readback、有界 GET execution metadata（核 workflow/version/deduplicationKey 计划时间——**n8n 2.34.6 该字段未列 OpenAPI，trial 第一验证项**；缺失停派，不以 now 或随机 ID 伪去重）。
  2. 回调端点 `POST /v2/projects/{project_id}/schedules/{schedule_id}/triggers`：挂 v2 router（按 D1 继承现有 Bearer，不新增鉴权机制）；受理短事务核 enabled/revision/启动窗口 → 唯一索引插 Trigger → 建 Run；decision 分支 accepted/skipped(过窗)/duplicate(同 tick)/rejected；Worker 开进程前 `validate_python_start` 再核。
  3. 启停状态机 `apply_schedule_change`：先关 Kernel 派发门槛与旧排队，再同步后端；外部回执返回后短事务重核当前 revision/控制状态，迟到回执只留事实不覆盖新决定；半成功分步记录（注册/发布/读回分别取证）。
  4. `settings.py` 的 `n8n_callback_secret` 标注退役。
- **验收**：先隔离 PG fixture 全链（受理→去重→窗口→Run→结果），**再经用户批准的真实 n8n trial**；候选稿 L2 §5.1 定时场景全表——同 tick 重投回原 Trigger/Run、不同 tick 同参数新 Run、重叠/错过 skip、tick 跨版本/回退不重开、迟到/排队/重试过窗、启停期间新决定、长期批准遇高影响变更不扩权。
- **回退**：adapter 与端点独立；外部 n8n 对象保留墓碑与回执。

### 3.5 定时语义校验 + 专项测试

- **变更**：validation_runner.py:404-425 的 YAML 形状校验扩展为 `python-schedule-definition.v1` JSON 语义校验（max_start_delay_seconds 必须具体批准的正整数、null 不启用；timer_owner 仅 n8n；并发 1/overlap skip/misfire skip 等 L3 §12 条件）；旧 YAML 读兼容不全迁。
- **验收**：负例集（auxiliary 目的直连长期调度、窗口为零、并发为 2、catchup 分支——候选稿 G 负例清单）全部拒绝。
- **回退**：校验层增量。

---

## 阶段四：公共背景与运营（B.5 包4）

### 4.1 公共背景单源转换（可与阶段二/三并行）

- **变更**：
  1. `public_background.py`：两活动源（user-background.v1 / development-environment.v1，:15-18）转换为单逻辑源 `public-background.v1` facts；旧 `field_key` 逐字段映射（候选稿 E 示意表），`development-environment.verified_runtime_refs` → `verified_runtime_refs`；当前环境与迁移计划分开。
  2. 撤回粒度：整消息抑制（`revoked_source_message_ids`，:85-103）改为 fact+source_ref 级过滤——撤回一个事实不遮同消息其他事实或批准。
  3. selector 双读兼容：旧 `user.x` / `environment.x` 前缀映射到统一 facts；`project.yaml` 的 `background_fields` 列表继续工作（run_processor.py:1883 预取不变）。
  4. 上下文影响收敛：public_context 组合哈希（run_processor.py:1885-1904）改为实际选中字段摘要（BackgroundSelection.selection_sha256）；无关字段更新不再触发全部 Project 会话重建（`public_background_changed` 判定用 selection hash；该原因 rotation 本就免 LLM，1136-1137）。
  5. 一次性转换：备份两旧文件 → 生成 facts → 用户对照确认 → 切 current；旧文件只读保留，不双写两个可编辑 head。跨项目保存按 D4 执行。
- **验收**：候选稿 A.5 全条——只读相关事实、当前/计划不混、撤回不顺撤批准、无关更新不重建会话；selector 旧格式回归。
- **回退**：转换前备份在位，单源可回切旧两源。

### 4.2 Project 范围文档服务

- **变更**：task_documents 扩展 Project 范围写入/读取（不伪造 Task ID）：`operations/documents/{operation-observation|improvement-candidate|operation-monitor-policy|kernel-notification}/<id>/revisions/<rev>.json`；`operations/current.json` 可重建导航（显示来源版本/时间/stale）；kernel_meta 存有效政策与控制 Ref；文件写入核 SHA 后由 DB 接受，未接受文件不参与统计。
- **验收**：候选稿 L2 §4.1.4 路径表逐条落位；Map 增相关引用。
- **回退**：纯增量服务。

### 4.3 观察/候选/规则引擎 + 通知包装

- **变更**：`record_operation_observation / evaluate_operation_rule / commit_operation_notice`（F.6 签名）；观察字段按候选稿 L2 §4.1.4 必要字段表（source_event_key/scene/window/影响/Task-Turn-Run/coverage）；候选带独立事件计数与已提示证据集（重投/镜像/retry 不重复计）。**关键实施点（候选稿 L2-0212 自己标出）**：`create_outbound` 自带事务提交（core.py:5821-5858），通知链需新增可参与外层事务的同事务 helper（写同一 messages 表），防"标已处理却漏入队"。规则触发：新事实/Run 结算轻量检查（无 LLM）；周期汇总复用 3.4 的 n8n Schedule（此部分依赖阶段三）；默认无规则不催。
- **验收**：候选稿 L2 §5.1 通知走查全表——无 Run 无新入站仍可投递、未批准规则不提醒、命中只提醒不调模型、镜像重试不重复累计、投递失败只重投。
- **回退**：规则引擎默认空转（无政策=无行为），回退安全。

### 4.4 通知路由三层 + 发送前重核

- **变更**：`resolve_project_notice_route`：Supervisor 主入口（interface registry/绑定补 primary 标记+有效性+配置指纹）→ 同 Project PM 主绑定 → 无则保存待路由原因（项目运营状态可查询，不伪称已通知）；Sender 发送前重核 Project/绑定/归档解绑/通知适用性（候选已解决或依据过期不发陈旧建议）；Renderer `kernel_notice` 分支（2.3 已建）；不回退其他 Project/任意最近聊天/未批准平台。
- **验收**：候选稿 G"运营提醒/用户主动迭代"行 + 路由负例（跨 Project 拒绝）。
- **回退**：路由层增量。

### 4.5 体系迭代闭环

- **变更**：`improvement-proposal.v1` 文档（Task 文档目录，复用 Task 版本服务；不建第二审批）；采用记录绑定项目实例/目标版本/批准引用/适用 Task 范围（既有 Task 下一包绑定确认才切换，运行包不热换）；效果 EffectComparison（before/after、实际包/条件/窗口分组、金额无来源 null）；发布/加载复用既有 capability_bundle publish 链。
- **验收**：候选稿 L2 §5.1 迭代行——旧候选重核现版本、效果按实际包归因、Task 关闭后观察继续、无收益不自动反复改。
- **回退**：流程层，复用既有发布链的回退机制。

---

## 阶段五：收口（存量迁移与 E2E，全部前置完成）

1. **存量授权对照确认**：旧明确只读/歧义来源保原语义；要转为完整长期 Project 约定的，逐 Project 一次对照展示并确认（候选稿 A.3），不静默扩大。
2. **部署切换在途状态走查**（审计补充场景，候选稿 G 未列）：重启瞬间在跑 Run 的租约/attempt 跨版本续接（quarantine_expired_runs 兜底验证）；旧格式待答卡/问题在 2.1 新模型下的兼容读；Map YAML 旧 head 只读。
3. **真实 E2E**：真实人类双入口全链（模糊需求→澄清→确认→PM→监督→关闭）；明确停止及时处理；停止/重启/回退演练；无孤儿计算核查；n8n 真实注册→触发→Run→业务验活（依赖 3.4 trial 批准）。
4. **部署机制**（节奏按 D3 另定）：per-version venv + kernel-current.txt 指针；supervisorctl 显式重启；DB 迁移 docker cp + psql；迁移前全量备份（含 DB dump）。

---

## 附录 A：关键现状锚点总表（2026-10-06 核对，0.25.0 工作树）

| 锚点 | 事实 | 涉及子阶段 |
| --- | --- | --- |
| run_processor.py:824-834 / 1905-1937 / 1138-1176 / 911-1258 | rotation 触发判定/触发链/LLM 摘要+确定性 fallback/checkpoint | 1.1 |
| core.py:5723-5795 | ensure_session 第二份判断 + 无 checkpoint 抛错 | 1.1 |
| harness/codex_cli.py:137-151 / 228-246 | auto-compact 配置透传 / exec resume | 1.1 门禁 |
| settings.py:80-82 / 39 | soft/hard ratio 观测 / n8n_callback_secret 未用 | 1.1 / 3.4 |
| project_grants.py:50-64 | run.created_at 截断 activated_at | 1.2 |
| workspace_scope.py:204-206 / 232-250 / 618-696 | pin 传参 / candidate 强制读 / candidate 写通道（保留） | 1.2 / 1.3 |
| execution_plan.py:310-313 / 344-353 / 365-378 | formal_read_only / 编译调用 / stage_permissions 标签 | 1.3 / 1.4 |
| goal_supervisor.py:908 | sandbox="read-only" | 1.3 |
| core.py:512 / 2741 | executor_json 不可变校验（兼容读） | 1.4 |
| scoped_tools.py:724-745；task_queue.py:440-459 | raw 行暴露 | 0.1 |
| worker/main.py:169-173 / 276-280 | attempt 覆盖 / 清理回执丢弃 | 0.2 / 0.3 |
| process_control.py:20-21 / 31-37 / 40-60 | 只等直接子进程 / start_ticks / 防复用 | 0.3 |
| public_background.py:15-18 / 85-103 | 两活动源 / 整消息抑制 | 4.1 |
| run_processor.py:1883-1904 | background_fields 预取 + 组合哈希 | 4.1 |
| core.py:5821-5858 / 6307-6545 | create_outbound 自带事务 / 重试租约 | 4.3 |
| validation_runner.py:404-425 | YAML 形状校验 | 3.5 |
| deploy/supervisord.conf:29-32 / 44-47 | interactive×2 / runtime×4 | 横切 |

## 附录 B：DB 变更清单（单次迁移 upgrade_core5_v8.sql）

| 表/索引 | 用途 | 子阶段 |
| --- | --- | --- |
| assistant.question_groups | 逻辑组/内容版/共享草稿/答案原子接受 | 2.1 |
| assistant.schedules | 注册/启停/生效边界/回执读回 | 3.3 |
| assistant.schedule_triggers + UNIQUE(schedule_id, scheduled_for_utc) | tick 跨 revision 唯一 | 3.3 |
| schema version core5.v7 → v8 | — | 3.3（首次合入时） |

## 附录 C：验收矩阵来源映射

- 候选稿 L1 §5.3（验证补充全表）→ 分配到 1.1/3.1/3.4/4.3 对应子阶段验收项。
- 候选稿 L2 §5.1（新增场景走查 18 行 + 通知走查 8 条）→ 2.1-2.3 / 3.4 / 4.3-4.4。
- 候选稿 G 卷 16 场景 + 负例集（31 项 schema 负例已离线通过，运行时等价物在各子阶段测试中建立）。
- 候选稿 B.5 各包验收行原样保留为阶段级验收。

## 附录 D：测试与实施纪律

- 测试沿用现有容器内执行约定（PYTHONPATH=src:repo，容器内 venv；Git Bash 路径转换注意）。每个子阶段：新增专项测试 + 全量回归（当前基线全绿）+ verify_core4.py。
- 每个子阶段实施前向用户展示：具体 diff、影响面、验证结果、回退方法（候选稿 B.5 总原则，本文不替代该确认步骤）。
- 本方案与候选稿冲突时以候选稿正文为准；候选稿五处引用漂移已按审计修正为上文锚点（codex_cli.py 在 harness/、native_mode 属 workspace_scope、compatibility_hash 为选定字段规范哈希、编译器无四操作掩码、grant 截断为 run.created_at 语义）。
