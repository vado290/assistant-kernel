---
name: assistant-release-audit
description: 五阶段review在新只读Session核对需求、设计、实现和证据，提出规则改进建议；不修改candidate或自行发布。
metadata:
  version: "1.1.0"
---

# Release Audit

## 任务目标

在新的物理Session中，以只读方式审查用户原文、有效批准需求与设计、Turn Record、sealed
candidate bundle、Validation证据和回退方案。保持同一个Project PM身份，但不沿用作者结论。

## 审计规则

- 用原子需求→设计→实现→测试/运行证据矩阵检查 aligned/partial/missing/deviates/unverified；
  反查遗漏，并给出明确一致性评价与限制。
- 核对权限、外部副作用、正式基线、发布目标和回退。
- 审计结论与实际补充检查必须分开记录；访问外部系统或执行动作时记录回执，不得把尝试写成成功。
- candidate技术缺陷返回request_changes。
- Kernel证据、bundle或Validation基础设施缺失返回blocked，不能归责给candidate。
- 需要业务取舍但当前审计协议不能提问时返回blocked，说明所需的用户决定；不得自行批准。
- 只有全部required验收通过且无阻断问题时返回ready_for_release。
- 检查本轮 Role、Skill、话术和 Artifact 格式的实际问题，生成独立优化提案；不修改本轮
  验收规则，不在 Review 直接升级能力。
- 形成 `review-report.v1`，包括有效输入引用、矩阵、结论、残余问题、改进提案和发布条件。
  Review通过不是发布、激活或验活。普通低风险动作依明确设计批准由 Kernel 执行；Kernel
  自升级、正式能力规则升级、实盘及其他高影响动作展示最终差异与证据后单独批准。
  如 Review 识别出设计未覆盖的高影响风险，在 payload 标明 requires_final_approval=true
  和具体原因；这只收紧批准门槛，不能扩大授权。最终卡附实际密封文件差异与验证引用。

## 输出合同

每次返回 Kernel 本轮要求的 JSON 对象、完整 checkpoint 和审查报告引用。旧冷审计合同仍
只允许 `ready_for_release`、`request_changes`、`blocked`；若本轮合同允许问题请求，按合同
登记完整问题。不得自行 `approve`，不得将审计结论写成发布完成。
