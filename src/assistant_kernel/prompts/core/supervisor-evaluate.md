## 本次任务：决定下一步，并写进用户的卡片
根据"评估原因"选择一个决定：

- relay_pm_questions：PM 提了问题（评估原因为 pm_questions 时必须用这个）。把 PM 的问题整理成给用户的问题；每个问题用 covers 列出它覆盖的 PM 问题 id，PM 的每个必答问题都必须被覆盖，或者在 suggested 里给出建议答案和依据。
- ask_user：你自己需要用户澄清，才能安排下一步。
- propose_next：安排下一轮 PM 工作。instruction 写给 PM 的完整指令；target_stage 填目标阶段（必须在"可去的阶段"里且门槛满足）。target_stage 为 release 时表示申请发布，不需要 instruction。
- propose_complete：任务目标已经达成，建议结束。
- propose_cancel：任务已无继续的意义，建议取消。

## 输出要求（必须遵守）
只输出一个 JSON 对象，不要加代码块标记或其他文字：

{
  "decision": "relay_pm_questions | ask_user | propose_next | propose_complete | propose_cancel",
  "message": "给用户看的说明",
  "questions": [{"id": "c1", "text": "问题", "required": true, "options": ["A", "B"], "recommended": "A", "covers": ["q1"]}],
  "suggested": {"q2": {"answer": "建议答案", "basis": "依据"}},
  "instruction": "给 PM 的完整指令",
  "target_stage": "requirements | design | build | review | release"
}

不用的字段可以省略。
