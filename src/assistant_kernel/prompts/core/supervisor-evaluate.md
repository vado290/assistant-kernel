## 本次任务：决定下一步，并写进用户的卡片
根据"评估原因"选择一个决定：

- relay_pm_questions：PM 提了问题（评估原因为 pm_questions 时必须用这个）。把 PM 的问题整理成给用户的问题；每个问题用 covers 列出它覆盖的 PM 问题 id，PM 的每个必答问题都必须被覆盖，或者在 suggested 里给出建议答案和依据。
- ask_user：你自己需要用户澄清，才能安排下一步。
- propose_next：安排下一轮 PM 工作。instruction 写给 PM 的完整指令；target_stage 填目标阶段（必须在"可去的阶段"里且门槛满足）。target_stage 为 release 时表示申请发布，不需要 instruction。
  可选 track 选择流程：quick（轻量：需求文档里写简要方案，批准后直接实施，跳过设计和 Review）或 full（完整五个阶段）。
  任务开始时判断工作量：改动小、方案明显、风险低的用 quick，其余用 full；做的过程中发现复杂了，可以提议改回 full。"可去的阶段"按当前流程计算；卡片上会提示用户流程的变化，用户点执行即确认。
- propose_complete：任务目标已经达成，建议结束。
- propose_cancel：任务已无继续的意义，建议取消。
- propose_grant：PM 需要访问本项目还没有授权的外部目录时，提议授权（directory 写路径、read/write、用途；只能在 H:\AI 下）。
  Kernel 先检查目录，再出卡片由用户批准；批准或拒绝后会再交给你安排下一步。
- propose_followups：把值得做的工作排成新任务（复盘中采纳的建议，ref 写它的编号如 P2，任务完成后才能复核效果；tasks 列出 1–5 个新任务，每个写 goal：一句话、能独立执行的目标；
  reason：为什么做，例如引用改进建议的编号和现象；原因会记入迭代记录）。用户批准后这些任务
  依次排队，当前任务随之结束。复盘任务（PM 提交了改进建议）结束时用它：只挑你认同、值得做的建议；都不值得做就用
  propose_complete。普通任务完成时如果发现明确的后续工作，也可以用它代替 propose_complete。

## 输出要求（必须遵守）
只输出一个 JSON 对象，不要加代码块标记或其他文字：

{
  "decision": "relay_pm_questions | ask_user | propose_next | propose_complete | propose_cancel | propose_followups | propose_grant",
  "message": "给用户看的说明",
  "questions": [{"id": "c1", "text": "问题", "required": true, "options": ["A", "B"], "recommended": "A", "covers": ["q1"]}],
  "suggested": {"q2": {"answer": "建议答案", "basis": "依据"}},
  "instruction": "给 PM 的完整指令",
  "target_stage": "requirements | design | build | review | release",
  "track": "quick | full",
  "tasks": [{"goal": "新任务目标", "reason": "为什么做", "ref": "P2"}],
  "directory": {"path": "/home/v/AI/crypto", "access": "read", "purpose": "用途"}
}

不用的字段可以省略。
