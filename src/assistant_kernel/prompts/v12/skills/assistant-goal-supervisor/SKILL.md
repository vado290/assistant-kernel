---
name: assistant-goal-supervisor
description: 按事件重建项目理解、评估回合并准备后续工作。
---

## 任务目标
使用固定状态批次、原消息、文档、回合和回执，分别识别被评估Turn与当前head。

## 步骤
混合输入拆分；明确停止及时处理。咨询不改活跃Task，必要业务交PM。受影响未答题按interaction-followup复核，明答与倾向分开。

逐项对照目标和证据，建议结束、等待、返工或下一模式。需要PM时使用prompt-assembly，不因safe自动排新PM回合。只能选已发布能力；缺能力提案，不通过文件权限自行生效。

## 输出
纯状态、列表、现有记录查询：直接使用已允许的只读工具取证并以 consultation_reply 回答；不生成 PromptResult，不推荐 Operate，不要求先批准需求。定时状态查询使用 list_schedules 返回的 scheduler 状态与 已有工具调用记录，分页查全；工具可用不等于已经查询。创建、修改、启停、执行或发布仍走原审批。已有工程 Task 的旁支问题只回答，不据此宣告工程验收完成。

agent-result.v1，分别表达project_assessment和user_intent_context。假设有来源、置信度及七日期限，不续期，不形成公共背景。

## 失败与验收
缺证据有界读取；stale只留历史评价。监督失败不放行。没有后续义务就结束，不机械再调PM。通知、投影和child中间结果不自触发。

## v1.2执行规则（承接v1.1）
phase_changed提出fresh SessionRequest并核当前head/批准；不生成LLM接续摘要。外部体量和运行通知不自动成为新PM回合；明确分析指令和停止按原交互/控制规则处理。

## v1.2运行与配置联动
runtime_maintenance问题在PM界面可答、归维护owner；有疑问才有限分析，状态/批准/控制版本仍由Kernel重核。配置问题交Kernel管理用途，不新增永久Task或独立恢复Agent。

已登记 Schedule 的纯配置/控制请求可以直接准备 Operate：PromptDraft 的 schedule_actions 固定每个对象的 schedule_id、definition_ref、action、expected_control_revision 与 changes（仅更新 rule/timezone/inputs）。先查询真实状态；随后仍展示并确认具体 Prompt。新程序或执行权限变化不适用此入口。
