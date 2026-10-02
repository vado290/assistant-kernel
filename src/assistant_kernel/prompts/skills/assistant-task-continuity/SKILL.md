---
name: assistant-task-continuity
description: Kernel保护的单Agent Task续接协议；规定Task Contract、Turn Record、needs_input、Session轮换、timeout恢复和证据优先级。
metadata:
  version: "1.0.0"
---

# Task Continuity Protocol

## 任务目标

保证一个Project Agent在多个短生命周期Codex Turn、compact和物理Session轮换之间继续同一个Task，不依赖模型隐式记忆，不重复外部副作用。

## 权威事实顺序

1. 用户原始消息与动作审批；
2. Kernel Task状态、Execution Plan和工具/外部回执；
3. 正式基线、candidate bundle和Artifact SHA；
4. accepted Task Contract与Turn Record；
5. 模型摘要与原生Session记忆。

低层事实不能覆盖高层事实。

## 每Turn必做

- 开始时核对task_id、accepted document refs、candidate manifest和pending。
- 只处理当前Turn可闭合的工作。
- 结束时返回结构化task_checkpoint。
- continue表示安全结束本Turn并继续同一Task，不代表新任务或重试。
- needs_input必须先保存已完成工作和完整问题。
- timeout恢复必须先读取trace、candidate和external_effects，禁止从头盲重试。

## 禁止

不得创建平行Task、隐式扩大目标、把未执行项写成完成、丢弃原始trace、仅凭compact摘要恢复、重复不可逆或外部动作。

## Session轮换

新物理Session必须读取accepted Task Contract、最后Turn Record、必要原文、candidate和未决问题，并保留parent lineage。required文档不完整或SHA无效时blocked，不允许模型猜测。
