---
name: assistant-schedule-management
description: 为当前 Project 设计、构建、校验及管理 Kernel 一次性、固定间隔和日历定时任务；适用于定时报表、业务工作流和服务启停维护。
---

## 状态与职责
RuntimeUnit 固定执行内容，Schedule 固定何时触发。使用 Kernel 工具与真实回执；不建立 n8n 工作流、shell 计时循环、后台 sleep 或占用普通 Run 的常驻计时器。

Requirements 明确目标、时间、时区、输入和验收要求；日期、时区、夏令时有歧义先确认。默认 Asia/Shanghai、停机错过全部跳过、同规则上一轮排队/运行/待答/恢复未结清时跳过、每轮结果通知。

Design 固定已批准目标、输入与时间规则。调用 preview_schedule 展示未来五次执行时间；秒级支持一次性和固定间隔，日历使用五字段 cron。秒级提交不承诺同秒开跑。

Build 交付程序及 Manifest、kernel-schedule-definition.v1、预览和相称自测。用 write_runtime_document 写候选；报告保留固定定义 Ref、用途、权限、测试和待登记义务。生成候选不等于登记、启用或业务成功。

Review 独立核对定义、目标版本、授权、时间边界与实际证据；只读查询，不登记、不启用、不改变验收标准。

Operate 在目标已实际发布登记后调用 register_schedule；新登记默认 disabled。用户已明确授权时接着调用 control_schedule 启用，不重复请求同一授权。纯时间配置变更直接在已确认 Operate 管理；执行代码或权限变化回完整构建审查。

Supervisor 查询 Schedule/Tick/Run 的真实状态，报告缺失证据、失败及待答；不自行计时、不创建执行授权。

## 管理与恢复
纯状态/列表查询可由 Supervisor 直接调用只读工具并回答，引用当前工具返回和已有调用记录，不额外生成查询快照；不为查询建立 Operate 确认包。任何写操作仍按下述 Operate 门槛执行。

结构化阶段产物：需求用 task-contract 的 schedule_requirements，设计用 solution-design 的 schedule_plans，记录 schedule_id、runtime_unit_id、action、inputs、rule、timezone、acceptance_ids 和 requires_first_run。Build 在 implementation-report 的 schedule_handoffs 返回固定 ScheduleDefinition Ref。是否必须等待首次真实定时成功，由 requires_first_run 和批准验收项决定；手动试运行不能替代该证据。

使用 list_schedules、schedule_status、list_schedule_ticks 查询。control_schedule 支持 update、enable、pause、resume、retire、run_now、cancel_current；传入当前 control_revision、稳定 idempotency_key 和本次真实用户确认 Ref。冲突先重新查询，不自行猜版本。

暂停或退役默认取消未开始工作，当前轮继续完成；cancel_current 才明确取消当前轮。人工停止/暂停 Unit 及故障保护会暂停关联规则，恢复 Unit 不自动恢复规则。run_now 不消费下一次时间、不恢复暂停规则。业务失败按原 Run 恢复，不创建第二个 Tick 重做未知效果。

## 对用户准确汇报
分别说明“已生成候选”“已登记未启用”“已启用及下次时间”“已到点提交正在排队”“运行中/待答”“本轮成功/失败/取消/跳过”。exhausted 只表示没有未来触发，不表示业务成功。返回 Schedule ID、时间及时区、Tick/Run 或服务回执；通知失败单独报告，不重执行业务。

日常只管理已登记的定时规则时，PromptDraft 使用 schedule_actions 明确 schedule_id、definition_ref、action、expected_control_revision、changes（仅 rule/timezone/inputs，非 update 时为空）。使用 Operate 并保留具体 Prompt 确认；原任务结束可建立独立日常操作，不重新编造需求/设计批准。只允许确认的规则与改动，代码、RuntimeUnit、执行权限变化仍回完整阶段流程。返回真实控制回执；run_now 已受理与业务最终结果分别报告。
