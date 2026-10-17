# 多问题卡片交互 v1

## 范围

保留 PM、Supervisor、child Run 和原生子 agent 的职责与权限。普通 Discord
`needs_input` 问题使用一张可翻页卡片；审批、凭据、其他渠道及超长问题保持原路径。
不新增数据库表，不改变 Codex 短进程执行方式。

## 输入和状态

- 继续使用 `questions`、`question_id`、`question_text`、`required`、`options`。
- 可选 `topic`、`recommended_option_id`、`recommendation_reason` 由 PM 生成；推荐不等于选择。
- `(run_id, interaction_seq, message_id)` 绑定一组不可变问题。
- 卡片 `content_json.questionnaire` 保存页码、草稿答案、补充说明、revision、status。
- 选择、自由填写、上一问/下一问只更新草稿。末页展示摘要、自由补充和统一提交。
- 所有问题均提供自由填写。可选题允许跳过；必答题未答时不能提交。

## 数据流

1. PM 返回问题集合，exec 结束。Repository 一次事务保存问题并进入 `waiting_user`。
2. Discord 根据持久化状态显示当前问题，按钮携带 revision/page，打开文本输入弹窗。
3. Kernel 校验用户、频道、原消息、问答轮次、reset 边界和 revision；保存动作事件与草稿。
4. 最终提交在同一事务内保存完整问题/答案、设置 `answer_history`、`answer_state`，
   将原 Run 从 `waiting_user` 变为 `queued`。UI 更新在提交后执行，失败不回滚业务状态。
5. 下一轮 PM 获得问题原文和完整答案；翻页/草稿事件不进入模型语义历史。

## 恢复和兼容

- 持久化 View 在 Discord 重启后重建；原生会话不参与卡片等待。
- 重复事件幂等，过期页/旧表单拒绝覆盖；提交后不再次恢复 Run。
- `/new` 和新一轮提问使旧卡失效。点击有效新按钮可从数据库刷新卡片。
- 草稿按问答轮次隔离，不能继承上一轮答案。
- 普通文字回复在此模式仅作为当前题草稿；最终提交仍需明确操作。
- 超长问题/特殊输入回退现有逐题消息，不截断原问题。
- 首版单卡最多 10 题，每题问题与选项合计不超过 1200 字符；超限保留旧路径。
- 翻页状态写数据库后才刷新原 Discord 消息；刷新失败可重试，不重复接受答案。
- 回退到不支持此协议的旧版本前，需要先结算待答问卷或转换为逐题消息。

## 验收

验证选择与推荐、任意自由输入、导航修改、末页补充、必答校验、原文回传、重复提交、
并发旧页、错误用户/频道、reset、重启重建，以及单题/审批/凭据/Feishu 回归。
使用隔离 PostgreSQL 测试数据库和 Discord 测试对象；真实用户点击单独标注是否已验证。

## 2026-10-03 验证记录

- 回归：296 passed（排除 `test_project_runtime_v2_migration.py`，其依赖的历史迁移脚本已缺失）。
- 最终问卷专项：11 passed，覆盖事务提交、并发旧页、reset、恢复、自由输入、表单回调与 PM 续接。
- 发布前检查：无 running/reconciling Run，允许受控重启；现有旧问答卡保持原处理路径。
- 无数据库迁移；子任务机制与权限配置不改动。
- 已发布 `0.24.7/core5.v6`，旧版为 `0.24.6`，未重建 AI 容器。
- 发布后 `/health/ready` 为 ready，Discord/execution_ready 均为 true，`CORE5_VERIFY_OK`。
- 部署后的交互相关测试 16 passed；四个改动模块的部署/源码 SHA-256 全部一致。
- 真实 Discord 用户点击尚未验证，不将模拟回调或健康检查视为真实交互 E2E。
