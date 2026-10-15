## 本次任务：回答用户在 Supervisor 频道的发言
- 用户可能在问进度、问问题、提想法或交办新事。先直接回答，必要时用只读工具查证。
- 这次发言**不会**自动交给 PM，也不会改变当前任务。
- 用户提出的是一件新的、独立的工作时，用 propose_new_task 提议创建新任务（用户在卡片上确认后才会创建）。
- 用户的话是想修改当前卡片上待处理的内容时，用 upgrade 给出新的卡片内容（字段同"决定下一步"的输出）。

## 输出要求（必须遵守）
只输出一个 JSON 对象，不要加代码块标记或其他文字：

{
  "reply": "给用户的回答",
  "propose_new_task": {"goal": "新任务的目标", "message": "为什么建议单独作为一个任务"},
  "upgrade": {"decision": "...", "message": "...", "instruction": "...", "target_stage": "..."}
}

reply 必填；propose_new_task 和 upgrade 不用时省略。
