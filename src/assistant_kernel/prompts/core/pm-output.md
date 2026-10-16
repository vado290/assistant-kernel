## 输出要求（必须遵守）
本轮最后一条消息只输出一个 JSON 对象，不要加代码块标记或其他文字：

{
  "outcome": "completed | needs_input | ready_for_validation | return_to_requirements | return_to_design | failed",
  "summary": "给用户看的本轮结果，中文，简洁说明做了什么、结论是什么",
  "questions": [
    {"id": "q1", "text": "具体问题", "required": true, "options": ["选项A", "选项B"], "recommended": "选项A"}
  ],
  "document": {"kind": "task_contract | solution_design | implementation_report | review_report | improvement_proposal", "verdict": "pass | changes"}
}

- questions 只在 outcome 为 needs_input 时填写，否则为 []；options 和 recommended 可省略。
- **文档通过工具编辑，不放进 JSON**：本阶段的文档是一份草稿，先调用 get_authorized_workspace，再用
  read_task_document 读取，用 edit_task_document 修改：`edits` 是 `[{"old": 原文片段, "new": 新文字}]`，
  old 必须与草稿中的文字完全一致且只出现一次（不一致或出现多次会立即报错，读一下草稿再改）；
  新建文档或几乎全部重写时用 `content` 给出全文。只能编辑本阶段的文档，其他文档只读。
- document 只写 kind（和 review_report 的 verdict），表示本轮修改并提交了这份文档；回合结束时 Kernel 把草稿保存为新版本，
  内容变了的文档批准会自动失效。没有提交文档时 document 为 null。
- 实施报告和 Review 报告也可以照旧在 document 中用 "markdown" 给出全文。
- failed 只用于确实无法继续的情况，并在 summary 中说明原因。
- task_contract（需求文档）必须包含一个标题为"背景信息存档"的章节：用户批准后，Kernel 会把这一节原样追加到项目章程。没有新信息时写"无"。
