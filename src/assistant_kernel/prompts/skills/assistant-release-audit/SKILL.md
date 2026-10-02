---
name: assistant-release-audit
description: 高风险Task发布前的同Agent冷启动只读审计；不继承作者结论，不修改candidate，只判断证据与发布条件。
metadata:
  version: "1.0.1"
---

# Release Audit

## 任务目标

在新的物理Session中，以只读方式审查高风险Task的用户原文、accepted Task Contract、Turn Record、sealed candidate bundle、Validation证据和回退方案。保持同一个Project Agent身份，但不沿用作者结论。

## 审计规则

- 逐项追踪需求、candidate文件、Validation target和证据。
- 核对权限、外部副作用、正式基线、发布目标和回退。
- 审计结论与实际补充检查必须分开记录；访问外部系统或执行动作时记录回执，不得把尝试写成成功。
- candidate技术缺陷返回request_changes。
- Kernel证据、bundle或Validation基础设施缺失返回blocked，不能归责给candidate。
- 需要业务取舍但当前审计协议不能提问时返回blocked，说明所需的用户决定；不得自行批准。
- 只有全部required验收通过且无阻断问题时返回ready_for_release。

## 输出合同

每次返回一个JSON对象，必须包含完整task_checkpoint。审计模式只允许
`ready_for_release`、`request_changes`、`blocked` 三种outcome；不得返回
`approve`、`needs_input`，也不得将审计结论写成发布完成。
