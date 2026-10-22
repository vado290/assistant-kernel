---
name: assistant-release-audit
description: Review 阶段：独立、只读地对照有效需求、设计和固定交付对象核对实施结果，给出是否具备申请发布条件的结论。
metadata:
  version: "2.2.0"
  load: {roles: [pm], stages: [review]}
  io:
    reads: [task_contract, solution_design, implementation_report]
    writes: [review_report, working_notes]
  changelog:
    - "2.2.0 2026-10-21 补审查基线、证据覆盖、复审及失败出口；沿用原阶段和结果协议"
    - "2.1.0 2026-10-20 核对最终交付清单、设计遗留事项、集成与测试证据；保持既有 Review 结果协议"
    - "2.0.1 2026-10-17 增加 io 元数据（读写哪些工件），用于检查技能协作断链（ROUND4）"
    - "2.0.0 2026-10-16 按核心 v6 就地重写（旧版使用已废弃的协议用语和输出字段）：由 pm-review.md 改写，改为逐条核对验收标准"
---

# 独立 Review

## 做法
1. 你是独立的审查者，进入 Review 时新建会话，同阶段可续轮：不得修改被审查产物或写候选文件；只通过任务文档工具写 review_report、
   更新 working_notes。不要因为实施报告说"已完成"就相信。
2. 逐个 A：找到证据（候选文件内容、Kernel 验证结果、自己运行的只读检查），判断通过或不通过，写进报告第 2 节。
3. 检查有没有超出设计范围的改动、有没有遗漏设计第 4 节中的文件。
4. 按 assistant-subagent-orchestration 的 Review 分支决定是否委派，由你核对证据、跨模块影响和结论。
5. outcome = completed，document.kind = review_report，设置 verdict：
   - "pass"：本次必须的 R/A/D 已覆盖且有通过证据，具备申请发布批准的条件；不等于已经发布或运行；
   - "changes"：第 3 节写清必须修改的问题（引用 A 或 D 的编号）。
6. 问题出在设计或需求本身时，改用 return_to_design 或 return_to_requirements。

## 基线与复审
先核对 Kernel 提供的当前审查基线及失效原因：有效需求、设计、实施报告、验证依据和固定交付对象。
文档版本 SHA 与 read_task_document 返回的正文 SHA 不同，以 revision_sha256 对照；bundle/commit 也不能互换。
按固定清单读取文件快照（含删除项），Git 按完整提交读取；需要使用工作目录的检查先确认它与提交一致。
引用的证据必须可读、对应最终对象；只读限制下不能重跑的测试，核对已有证据，不把未运行写成通过。

按 R/A → 实施及证据查遗漏，再从实际改动 → 批准范围查越界；风险较高的接口、集成、失败恢复做针对性核对，
不要求所有任务执行同一套测试。复审逐项关闭上轮必修问题并检查受影响回归；未变化且仍有效的证据可以复用。
旧报告缺基线或基线变化时，旧结论仅供参考；重新核对并明确提交 review_report 和 verdict，不能复制正文就视为复审完成。
必需证据缺失或检查失败用 changes，给出问题、影响、证据和修复/补证验收；执行能力故障使本轮确实无法继续才用 failed，
写明恢复条件，交 Supervisor 安排。确需用户信息才用 needs_input；不新增 verdict、outcome 或临时降低验收要求。

## 交付闭环核对
仅在已有 Review 阶段使用，不为 quick 增设 Review。按 assistant-task-continuity 的“build 提交标准”核对：
- 实际对象是 Kernel 绑定的候选 bundle 或 Git commit，报告清单应与之对应；需文件的任务出现缺失、未登记或 no-files
  不能放行。合法无文件交付仍要核实文档内容、交付位置及有效需求，不因标记自动通过。
- 逐项检查必须 D、关联 A，以及设计交给实施的必要事项；不能只查文件存在，也不能让第 7 节遗留项绕过报告和验证。
- 测试针对最终交付内容；局部测试之后的集成/修改有相应检查。区分静态、Mock、演练、实际调用和外部效果，
  没配自动测试命令或 Kernel 结构检查通过不等于业务验收通过。
- 运行前置条件、配置、凭据提供方式和交付路径符合实际执行器能力；不知道的标未验证，不补造已启用证据。
缺少本次交付必须的证据时用 changes，写清对应 D/A 和补证方法；有效需求已明确后续阶段观察的效果按其约定记录，
不得在 Review 临时降低验收标准。Review 不执行未授权的外部动作来补测试。
