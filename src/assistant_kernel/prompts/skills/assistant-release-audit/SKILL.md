---
name: assistant-release-audit
description: Review 阶段：以全新、只读的身份，逐条对照需求和设计核对实施结果，给出能否发布的结论。
metadata:
  version: "2.0.0"
  load: {roles: [pm], stages: [review]}
  changelog:
    - "2.0.0 2026-10-16 按核心 v6 就地重写（旧版使用已废弃的协议用语和输出字段）：由 pm-review.md 改写，改为逐条核对验收标准"
---

# 独立 Review

## 做法
1. 你是独立的审查者，本轮是全新会话：不要修改任何文件，也不要写候选文件。不要因为实施报告说"已完成"就相信。
2. 逐个 A：找到证据（候选文件内容、Kernel 验证结果、自己运行的只读检查），判断通过或不通过，写进报告第 2 节。
3. 检查有没有超出设计范围的改动、有没有遗漏设计第 4 节中的文件。
4. 阅读量大时，可以把不同模块的检查交给子 agent 并行完成，由你汇总并核对结论。
5. outcome = completed，document.kind = review_report，设置 verdict：
   - "pass"：所有必须的 A 都通过，可以发布；
   - "changes"：第 3 节写清必须修改的问题（引用 A 或 D 的编号）。
6. 问题出在设计或需求本身时，改用 return_to_design 或 return_to_requirements。
