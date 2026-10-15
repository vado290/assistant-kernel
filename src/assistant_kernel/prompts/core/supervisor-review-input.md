## 本次任务：卡片还没处理时，用户又说了话，判断卡片要不要改
- 如果新消息只是补充细节，不改变卡片要用户决定的事情：输出 keep。补充内容会随卡片一起交给 PM。
- 如果新消息改变了方向、推翻了卡片内容，或者已经回答了卡片上的问题、需要换一种问法：输出 upgrade，并给出新的卡片内容（item 中的字段与"决定下一步"的输出相同）。
- 卡片来自 PM 的问题时，新卡片仍须覆盖 PM 的每个必答问题（decision 用 relay_pm_questions）。

## 输出要求（必须遵守）
只输出一个 JSON 对象，不要加代码块标记或其他文字：

{"decision": "keep"}
或
{"decision": "upgrade", "item": {"decision": "...", "message": "...", "questions": [...], "instruction": "...", "target_stage": "..."}}
