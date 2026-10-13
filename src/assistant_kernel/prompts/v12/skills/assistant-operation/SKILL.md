---
name: assistant-operation
description: 验活、管理批准运营事实与有限报告，收集改进而不自动升级。
---

## 输入
初始验活用本Task设计和发布回执，允许没有基线；日常运营用已验证基线、运营授权和报告窗口。

## 步骤
核实际版本、场景、指标和回执，区分正常、退化、失败、未知。只执行已批准且实际加载的动作；正式改动回实施链。

反馈关联时间、对象版本和原证据，同一逻辑事件不重复累计。向Kernel提交观察、候选和策略请求。提醒由项目批准规则及Outbox产生，无规则不催，不新增模型巡检。

## 输出与交接
operation-report.v1列窗口、依据、指标、动作、事件、未知和提案。全部义务达成才建议接受基线、对象交接和Task完成。已批准的手动运营与事件观察继续，新PM报告回合仍确认。

## 失败与验收
未知效果先对账，严重事件只用已有保护授权。不把单次服务故障归因Skill；无价格依据不造成本金额。

## v1.2执行规则（承接v1.1）
监控RuntimeUnit/常驻实例/业务Agent图及方法包版本；源Task可关闭。规则命中只发通知，用户确认才正式分析；常驻按批准策略有限恢复，暂停停止优先，改规则代码仍走批准。

## v1.2运行与配置联动
手动维护前由Kernel核查忙闲和待答，有疑问交Supervisor，忙或待答保留并通知PM。解释器报错先通知，不等重试耗尽。参数变更不启用已暂停服务；定时维护仅经 Kernel Schedule 登记和授权，忙/待答跳过，人工停止服务同时暂停关联规则。

## 已接入的记录和提醒
用 write_operation_document 保存观察、候选、提醒策略、改进提案和效果比较。将返回 Ref 放入本轮 observation_refs、improvement_candidates、monitor_policy_refs、proposal_refs；效果比较放入 operation-report.effect_comparison_refs。Kernel 在当前 Turn 接受后登记 Project 记录和 Map，候选保存本身不授权修改或启用提醒。

提醒策略按实际需求显式声明规则，默认无规则。需要批准启用时，提出 topic=operation_monitor_policy 的问题，并在 source_refs 中只放对应的固定 operation-monitor-policy.v1 Ref；Kernel 展示该版本完整规则并记录批准。停用使用 topic=operation_monitor_policy_disable。Operate 用 set_operation_monitor_policy 提交该实际回答的消息 Ref，不能拿目录约定或旧泛泛同意代替。原策略继续生效直至新版本被批准，拒绝新候选不改变原策略。

query_project_operations 查询已接受事实；list_runtime_units/runtime_unit_status 查询运行状态。没有新证据不请求模型巡检。通知只建议用户发起分析；修改 Role、Skill、程序、图或输出仍走具体 Prompt 和原五阶段批准。效果比较缺少可比输入、版本或环境时标 unverified/incomparable，不按推测填 improved。

## 定时任务联动
涉及指定时间、周期业务或服务维护时读取 assistant-schedule-management。按本阶段能力使用候选写入、预览、查询或 Operate 管理工具；登记、启用、到点提交和业务成功不得混称。
