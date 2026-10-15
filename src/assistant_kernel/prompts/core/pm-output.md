## 输出要求（必须遵守）
本轮最后一条消息只输出一个 JSON 对象，不要加代码块标记或其他文字：

{
  "outcome": "completed | needs_input | ready_for_validation | return_to_requirements | return_to_design | failed",
  "summary": "给用户看的本轮结果，中文，简洁说明做了什么、结论是什么",
  "questions": [
    {"id": "q1", "text": "具体问题", "required": true, "options": ["选项A", "选项B"], "recommended": "选项A"}
  ],
  "document": {"kind": "task_contract | solution_design | implementation_report | review_report | improvement_proposal", "markdown": "完整文档正文", "verdict": "pass | changes"}
}

- questions 只在 outcome 为 needs_input 时填写，否则为 []；options 和 recommended 可省略。
- document 只在本阶段要提交文档时填写，否则为 null；verdict 只用于 review_report。
- failed 只用于确实无法继续的情况，并在 summary 中说明原因。
