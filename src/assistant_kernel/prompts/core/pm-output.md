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

- outcome：本阶段工作完成用 completed（需求、设计、Review、运营和复盘都是如此，轻量流程也一样）；
  ready_for_validation 只用于实施阶段，表示候选文件已写好、请 Kernel 验证。
- questions 只在 outcome 为 needs_input 时填写，否则为 []；options 和 recommended 可省略。
- **文档通过工具编辑，不放进 JSON**：本阶段的文档是一份草稿，先调用 get_authorized_workspace，再用
  read_task_document 读取，用 edit_task_document 修改：`edits` 是 `[{"old": 原文片段, "new": 新文字}]`，
  old 必须与草稿中的文字完全一致且只出现一次（不一致或出现多次会立即报错，读一下草稿再改）；
  新建文档或几乎全部重写时用 `content` 给出全文。只能编辑本阶段的文档，其他文档只读。
- document 只写 kind（和 review_report 的 verdict），表示本轮修改并提交了这份文档；回合结束时 Kernel 把草稿保存为新版本，
  内容变了的文档批准会自动失效。没有提交文档时 document 为 null。
- 实施报告和 Review 报告也可以照旧在 document 中用 "markdown" 给出全文。
- **工作笔记（每轮都要更新）**：`working_notes` 是你在本任务中的工作笔记，任何阶段都可以用 edit_task_document 编辑，
  不需要批准，也不写进 document 字段。每轮结束前把它更新到最新，固定章节：当前理解 · 已定事项 · 开放问题 · 假设与默认 · 下一步。
  以后接手的会话（可能是一个全新的会话）只能看到 Kernel 的任务状态簿、这份笔记和文档，看不到本次对话，
  所以要写到"换个人接手也能继续"的程度；状态簿已记录的原话和回答不必抄写，写你的理解、判断和打算。
- failed 只用于确实无法继续的情况，并在 summary 中说明原因。
- task_contract（需求文档）必须包含一个标题为"背景信息存档"的章节：用户批准后，Kernel 会把这一节原样追加到项目章程。没有新信息时写"无"。
- task_contract 还必须包含一个标题为"用户画像观察"的章节：本任务中观察到的、与协作方式有关的用户特点（每条写维度、观察、证据，
  未经用户确认的标 [推断]）。用户批准后，Kernel 把它追加到项目的 knowledge/user-profile.md。没有新观察时写"无"。
- **行业速写（domain_brief）**：需求阶段的行业调研结果，用 edit_task_document 编辑（通常由调研子 agent 写入），不需要批准，
  不写进 document 字段；需求文档获批时，Kernel 把它存为项目的 knowledge/domain-brief.md，以后的任务直接使用。
- **delegations（可选）**：本轮派过子 agent 时，在 JSON 中加
  `"delegations": [{"name": "子 agent 定义名称", "goal": "交给它的目标", "result": "一句话结果"}]`。
  Kernel 看不到子 agent 的过程，只能靠这里的记录做交接和复盘；没派就省略。
