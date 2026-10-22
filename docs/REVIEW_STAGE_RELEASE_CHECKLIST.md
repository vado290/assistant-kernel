# Review 修复：升级与回退检查

本轮候选发布版本为 0.45.9，基于生产 0.45.8，尚未部署。
只增加 full 交付任务的审查基线和发布准入校验，不修改阶段转换表、SQL、数据库结构或 quick 流程。
本文件是发布操作条件，不是自动迁移脚本，也不授权执行取消任务或部署。

## 数据与技能兼容

- review_report 的既有载荷附加 review_baseline，包含 task_contract、solution_design、implementation_report
  的文档版本 SHA、bundle_sha、validation_sha。Kernel 根据审查输入生成，模型不填写。
- 基线进入不可变文档哈希；任务文档元数据中的同名字段由同一次生成结果投影，不能独立补写。
- 旧报告不回填、不删除。仍在 review 的旧任务重新审查；已完成/取消任务只保留历史。
- 仅四份模板需同步：release-audit 2.2.0、task-continuity 2.10.0、subagent-orchestration 2.4.0、
  goal-supervisor 6.9.0。部署前按实际加载角色清点副本，保留项目自定义内容和其他技能。
- requirements、所有 SQL、阶段转换表、历史 approval_valid 和发布执行器保持原有行为。

## 升级前必须满足

1. 固定源码、安装包与测试证据；核对安装包中的代码及四份技能，备份旧安装包、待更新技能副本和同步标记。
2. 停止接收新的控制流程操作，等待已有控制 Run 结束；发布结果不确定时先核对，不能将中断当作未发布。
3. 只读清点下列任务。结果非空时，先在旧版正常完成处理，或保留成果后经用户确认结束并另建任务承接。
   不能让已经进入 release 的旧任务依赖“返回 Review”继续，因为此阶段只允许 release → release。

```sql
SELECT task_id, project_id, stage, state
FROM core.tasks
WHERE kind = 'delivery' AND track = 'full' AND stage = 'release'
  AND state NOT IN ('completed', 'cancelled');

SELECT run_id, task_id, run_type, purpose, status
FROM core.runs
WHERE run_type IN ('pm_turn', 'supervisor', 'validation', 'publish', 'bootstrap', 'triage')
  AND status IN ('running', 'reconciling');
```

4. 排队的发布 Run 也须清点；不能只看进程健康或 running=0。确认无发布结果待核对及无人接手的失败任务。
5. review 阶段的旧发布卡可以保留，但新版不得放行：审批提示本次未批准 → 用户点“不同意” → Supervisor
   安排同阶段复审 → PM 明确提交新报告 → 新卡再次申请批准。报告没基线时不能仅复制正文补签。
6. requirements/design/build 旧任务按原流程继续，到 Review 再生成基线；不批量改变任务阶段或批准记录。

## 发布失败与卡片恢复

- 当前卡片因基线失效被拒绝：事务回滚后卡片保留，reject 必须可用。返回具体原因，不谎称已经有替换卡。
- 已替换或已处理的旧 item：沿用 StaleItem；重复点击不能重复发布。
- release 阶段的发布失败：基线有效且障碍消除时，重新申请用户批准同一交付对象；不复用旧按钮。
- 必须返工或缺少新基线时，不能提议 release → review/build。先保存文件快照、工作区未提交内容、报告和证据位置，
  用户确认后结束旧任务，新任务关联旧任务承接；取消可能触发工作区清理，不能依赖该目录永久保留。
- 发布结果不确定走已有 reconcile，核对事实后选择；不能用“确认已发布成功”绕过未完成的审查。

## 回退前必须满足

1. 停止新的控制流程操作并核对所有排队、运行、重试和待核对的控制 Run。
2. 下列结果必须为空，或先在新版通过上述恢复路径完成处置。等待用户的卡片也属于未完成任务，不能漏查。

```sql
SELECT task_id, project_id, stage, state
FROM core.tasks
WHERE kind = 'delivery' AND track = 'full' AND stage IN ('review', 'release')
  AND state NOT IN ('completed', 'cancelled');

SELECT run_id, task_id, run_type, purpose, status
FROM core.runs
WHERE run_type IN ('pm_turn', 'supervisor', 'validation', 'publish', 'bootstrap', 'triage')
  AND status IN ('queued', 'running', 'reconciling');
```

3. 旧版能读取带附加字段的文档，但不会执行新基线校验；这只是数据兼容，不能保证安全接管新版未完成任务。
   条件不满足时先保持相关执行停止并核对，不允许自动重放。回退恢复的是旧版能力，不能宣称保留新校验。
4. 恢复经核验的旧安装包和本轮受影响技能副本/同步标记；出现部署后的项目自定义编辑时先保留并比对，不能覆盖。
   保留新增业务数据和不可变文档，不整库恢复旧备份，不修改历史 SHA，不恢复已处理卡片。
5. 验证版本身份、技能副本、运行健康、卡片和任务状态；这些检查不代替真实业务验收。

## 验收证据边界

自动测试应覆盖真实临时数据库中的卡片事务、复审恢复、发布失败重试、reconcile 收尾、quick 及其他阶段；
模型可用脚本替身，文件/Git 操作使用临时目录。另用原 0.45.8 安装包验证文档可读及旧规则忽略新字段这一限制。
这些证据不表示已在生产执行升级、回退或真实模型端到端验收。发布时还须重新检查当时的任务和 Run 状态。
