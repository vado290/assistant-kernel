---
name: assistant-implementation
description: 实施阶段：严格按已批准的设计写候选文件并自测，提交实施报告。
metadata:
  version: "2.0.0"
  load: {roles: [pm], stages: [build]}
  changelog:
    - "2.0.0 2026-10-16 按核心 v6 就地重写（旧版使用已废弃的协议用语和输出字段）：由 pm-build.md 改写，增加子任务汇总"
---

# 实施与自测

## 做法
1. 只实施设计文档第 4 节列出的变更；发现设计不可行时用 return_to_design 说明，不要自行改方案。
2. 所有交付文件都用 write_candidate_file 写成候选文件（目标路径即正式路径），不要直接改正式文件。
3. 设计第 8 节列出的可并行子任务，可以交给子 agent，每个子 agent 的写集不能重叠（见 assistant-subagent-orchestration）。
4. 在沙箱里实际运行测试或检查，按设计第 5 节逐个验证 A，把命令和真实结果写进报告。
5. 全部完成后 outcome = ready_for_validation，document.kind = implementation_report，章节按 assistant-task-continuity。
   分多轮完成时，中途用 completed，并在 summary 中写清进度和下一步。

## 自检清单
- [ ] 没有超出设计范围的改动；与设计不一致之处都写了原因
- [ ] 自测结果是真实运行得到的
