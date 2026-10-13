---
name: assistant-task-continuity
description: 保存固定回合依据、checkpoint和真实效果，支持安全续办、挂起和结束。
---

## 任务目标
继续同一业务目标而不重做已完成动作、不丢批准、不复活取消任务。

## 输入
当前Task/咨询、固定Prompt、有效文档、上一Turn、pending输入、批准、运行回执和checkpoint。

## 步骤
先核owner、当前模式及未处理输入，区分新业务回合与未结算技术恢复。恢复前核已发生效果和受影响基线；技术恢复使用原包，新回合确认具体目标。

普通新增排队，明确B优先时安全挂A；恢复A先核依据，不自动抢回。停止与取消及时提交Kernel。取消终止旧包资格，但不撤长期目录Grant，不自动停止旧已验证独立运营对象。

## 输出
checkpoint包含completed、pending、constraints、decisions、artifact_refs、external_effect_refs、unresolved_question_ids和next_action。不要每轮复制全历史。

## 失败与验收
未知效果先对账，投递失败只重投。完成前处理相关输入并核全部适用义务。咨询只关咨询；交付完成后引用历史Task继续Project观察，不让原Task永久占前台。

## v1.2执行规则（承接v1.1）
同阶段resume，跨阶段fresh_from_artifacts；保业务checkpoint及答案/批准，不自动搬原聊天。不使用ctx_ratio/usage_unknown创建额外LLM摘要；业务图checkpoint为业务状态用途，分别管理。

## v1.2运行与配置联动
恢复沿原logical_execution_id、定义/图版本、checkpoint和累计预算；常驻升级先完成或明确取消旧工作项。全局变化只在适用安全边界采用，旧包不热替。
