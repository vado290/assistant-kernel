## 当前阶段：实施与自测
- 只实施已批准设计中的内容；发现设计不可行时用 return_to_design 说明。
- 所有要交付的文件都用 write_candidate_file 写成候选文件（目标路径即正式路径）；不要直接改正式文件。
- 在沙箱里自行运行必要的测试或检查，把真实结果写进实施报告。
- 全部完成后 outcome 用 ready_for_validation，document 提交 kind=implementation_report（Markdown）：做了什么、候选文件清单、自测方法和结果、已知问题。Kernel 会封存候选文件并运行验证。
- 分多轮完成时，中途用 completed 并在 summary 里写清进度和下一步。
