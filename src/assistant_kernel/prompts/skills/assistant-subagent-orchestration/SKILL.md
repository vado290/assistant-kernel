---
name: assistant-subagent-orchestration
description: 在同一Project Task内把独立、上下文密集的工作委派给最多三个子Agent，并用Delegation Result向PM返回紧凑证据。
metadata:
  version: "1.1.0"
---

# Subagent Orchestration

## 何时委派

- 两个以上模块、资料来源或故障假设可独立分析；
- 阅读量会明显污染PM主上下文；
- 需要独立Review、风险检查或测试分析。

短任务、严格串行步骤、目标不明确或无法独立验收时由PM直接完成。

## 委派合同

每个子Agent只有一个目标，明确输入引用、验收标准和声明写集。最多三个并行，第一版不再创建孙Agent。写集重叠时串行执行；Turn结束由PM核对candidate diff。完整权限继承自PM，外部效果必须进入回执，无法精确归属时诚实标记为父PM Run或unattributed native child。

## 返回

每个原生子Agent完成后，PM必须在本Turn最终JSON的`delegation_results`中返回稳定
`delegation_id`、status、summary、findings、changed_files、test_results、external_effect_refs、
evidence_refs、artifacts、uncertainties、residual_risks和recommended_next_action。原始输出留在
Trace，不复制进PM正文。

required Delegation缺少Result时不得宣称Turn完成；Kernel会拒绝提交。Kernel按Result写不可变
`delegation-result.v1`，从`active_delegations`移除Request并加入`child_results`。optional
Delegation可留待后续Turn，但必须在checkpoint中说明。
