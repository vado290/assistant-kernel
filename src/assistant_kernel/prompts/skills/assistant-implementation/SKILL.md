---
name: assistant-implementation
description: 实施阶段：严格按已批准的设计写候选文件并自测，提交实施报告。
metadata:
  version: "2.1.1"
  load: {roles: [pm], stages: [build]}
  changelog:
    - "2.1.1 2026-10-16 运行声明文件改为项目根目录的 jobs.yaml（vault 的 runtime/ 目录存放 Kernel 内部数据）"
    - "2.1.0 2026-10-16 实施交付包括 jobs.yaml 和脚本的真实试运行（ROUND3 S3）"
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

## 会运行的产物
设计第 9 节有作业或服务时：
- 写 `jobs.yaml` 候选文件（格式见 Kernel 给出的说明），与设计第 9 节逐项对应；
- 每个脚本至少真实运行一次（测试数据或演练参数），命令和输出写进实施报告；agent 作业写好 agents/<名称>/ 和专属技能；
- 发布后由 Kernel 登记并按时运行，不要自己写 crontab 或启动后台进程。

## 自检清单
- [ ] 有作业或服务时，manifest 与设计第 9 节一致，脚本真实运行过
- [ ] 没有超出设计范围的改动；与设计不一致之处都写了原因
- [ ] 自测结果是真实运行得到的
