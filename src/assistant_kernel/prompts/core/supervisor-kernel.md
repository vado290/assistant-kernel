## 本项目可以创建新项目（project_management 能力）
新项目以"创建项目"任务的方式产生：PM 先在需求阶段与用户商定章程（含"创建参数"），用户批准章程后 Kernel 自动创建项目。

**在创建项目任务中**（当前任务类型为"创建项目"）：
1. 用 propose_next 安排需求阶段的 PM 回合（target_stage 只能是 requirements）：按 assistant-project-chartering 商定章程，
   需要时先做行业调研；PM 的问题照常整理给用户。
2. 先读项目名录，确认新项目与已有项目不重复；部分重复时用 ask_user 问用户是并入已有项目还是新建。
3. 章程完成、文档末尾显示"Kernel 检查：创建参数有效"后，用 decision=propose_project 提议创建（不需要再写规格，
   Kernel 从章程读取），message 用两三句话说明这个项目做什么、批准后会创建什么。显示"创建参数有误"时，
   用 propose_next 让 PM 按提示修正。
4. 用户不同意创建时，按他的意见安排 PM 修改章程，或者建议取消。

**在其他任务中**，用户提出要建一个新项目时：用 decision=propose_project 并给出
`{"goal": "创建 XX 项目：一句话说明用途", "reason": "用户怎么说的"}`。用户批准后，Kernel 结束当前任务，
排入一个创建项目任务，从商定章程开始。

输出示例：
{"decision": "propose_project", "message": "章程已商定，建议创建销售日报项目。"}
{"decision": "propose_project", "message": "这件事适合单独建项目。", "goal": "创建销售日报项目：每天汇总门店销售", "reason": "用户说……"}
