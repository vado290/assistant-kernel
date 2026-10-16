---
name: assistant-git-delivery
description: 代码交付方式下的实施与 Review：在本任务专属的 git 工作区里修改代码、写测试、自测；Kernel 自动提交，用这次提交运行测试，批准后快进主分支。
metadata:
  version: "1.0.0"
  load: {roles: [pm], stages: [build, review], capabilities: [git_delivery]}
  changelog:
    - "1.0.0 2026-10-16 启用（R4a）：PM 只改文件不运行 git，Kernel 每轮自动提交并绑定提交编号"
    - "0.1.0 2026-10-16 草稿：随 R4a（代码交付）启用"
---

# 代码交付

## 与文件交付的区别
本项目的正式产物是代码仓库。你不写候选文件，而是在 Kernel 为本任务准备的 git 工作区（独立分支，从主分支拉出）里直接修改。
每轮结束 Kernel 自动提交；提交实施报告时，报告绑定到最新提交，Kernel 在这个提交上运行项目的测试命令（project.yaml 的
validation.command）；用户批准发布后，主分支快进到这个提交。

## 实施阶段
1. 先读仓库根目录的 README、AGENTS.md（如有）和与本任务相关的代码，了解结构和测试方法；按设计第 4 节的文件清单修改。
2. 修改行为的同时修改或新增测试；在工作区里运行测试命令（以及相关的单个测试），把命令和真实结果写进实施报告第 3 节。
3. 分多轮完成时，每轮结束的改动都会被提交，下一轮接着改即可；全部完成后 outcome = ready_for_validation。
4. 不运行 git commit / merge / push / checkout / reset；不部署。需要修改数据库结构时只新增迁移文件，在实施报告中醒目标注
   "包含数据库迁移"。
5. 实施报告第 2 节"候选文件清单"改写为"改动的文件"，可以用 `git diff --stat <主分支>` 的结果。

## Review 阶段
对照需求和设计阅读改动（`git diff <主分支>...HEAD`，只读）和 Kernel 的测试结果；检查测试是否真正覆盖了改动、
有没有删除或跳过已有测试、有没有超出设计范围的改动。

## 常见问题
- 测试命令没有配置时，Kernel 只确认提交存在：Review 必须人工核对测试，并建议在 project.yaml 中配置 validation.command。
- 发布时如果主分支已经有了新提交（例如用户手工提交过），快进会失败；告诉用户，由用户决定如何合并。
