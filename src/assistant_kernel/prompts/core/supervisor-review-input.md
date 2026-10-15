## 本次任务：卡片还没处理时，用户又说了话，判断卡片要不要改
- 用户的新消息改变了目标、文件、范围、方向，或者已经回答、推翻了卡片上的内容：输出 upgrade，并给出新的卡片内容。**拿不准时选 upgrade**——旧卡片会被自动撤下，用户只需看新卡片。
- 只有当新消息只是补充细节、卡片要用户决定的事情完全不变时，才输出 keep。补充内容会随卡片一起交给 PM。
- 新卡片 item 的 decision 只能是：
  - ask_user：你需要用户回答问题；
  - propose_next：安排下一轮 PM 工作（填 instruction 和 target_stage）；
  - relay_pm_questions：**只在**当前卡片是 PM 提出的问题时使用，并且仍须覆盖 PM 的每个必答问题。

## 输出要求（必须遵守）
只输出一个 JSON 对象，不要加代码块标记或其他文字：

{"decision": "keep"}
或
{"decision": "upgrade", "item": {"decision": "ask_user | propose_next | relay_pm_questions", "message": "...", "questions": [...], "instruction": "...", "target_stage": "..."}}
