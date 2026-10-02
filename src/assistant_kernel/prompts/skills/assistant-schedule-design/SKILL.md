---
name: assistant-schedule-design
description: 设计或审查Project的定时与周期触发，包括目标节点、时区、重叠、misfire、游标、幂等和Review监督；仅在任务涉及Schedule时使用。
metadata:
  version: "1.1.0"
---

# Schedule设计

## 任务目标

把业务中的“每分钟、每三小时、每天或特定时刻执行”表达为可注册、可恢复、不会机械重跑整条Project流程的Schedule。Schedule只负责在到期时为指定Agent、脚本、Workflow或健康检查创建Run，不在计时器进程中执行LLM业务。

## 设计原则

- Schedule是Project一级对象；Agent Config的 `triggers` 只声明可接受的Schedule来源或类型，不在多个Agent文件中复制完整计时定义。
- 高频采集默认目标是脚本或Service；只有需要语义判断的汇总窗口才调用Agent。
- 每个节点独立调度，只处理自上次成功后的增量。计时、next/last fire和租约属于Scheduler运行态；业务watermark属于目标节点自己的checkpoint或Artifact，不能混入Schedule定义。
- 定义 `timezone`、enabled、目标类型/ID、输入窗口、timeout、max_concurrency、overlap和misfire策略。
- 每次触发使用 `schedule:<schedule_id>:<scheduled_for>` 等稳定幂等键，重复扫描不得产生重复Run。
- overlap策略只能明确选择skip、queue-one或replace；涉及外部副作用时默认不replace。
- misfire策略明确skip、catch-up-once或bounded-catch-up；禁止进程恢复后无限补跑。
- Schedule定义存Project文件；数据库或运行状态只保存next/last fire、租约和关联Run。

## 设计过程

先画出各节点自己的频率、依赖和输入窗口。检查一个节点变慢时是否积压、上游无新数据时是否跳过、时区和夏令时影响、失败后的重试与下一周期关系，以及Review如何比较预期触发与实际Run。

## 实施与验证

Implementation生成Schedule文件、目标节点配置和测试；必须模拟重复触发、并发触发、错过周期、无新增输入和失败恢复。Review检查高频Schedule没有直接调用LLM，Schedule禁用和配置修改能在下一次扫描生效。
