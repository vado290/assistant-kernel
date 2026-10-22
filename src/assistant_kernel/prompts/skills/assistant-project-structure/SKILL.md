---
name: assistant-project-structure
description: design/build 涉及新增资源或结构调整时，区分系统固定位置与项目业务目录，确定工作区、正式目标和必要配套；普通内容编辑不使用。
metadata:
  version: "1.2.0"
  load: {roles: [pm], stages: [design, build], on_demand: true}
  changelog:
    - "1.2.0 2026-10-21 统一两种交付模式与目录职责，修正旧运行及失败表述；不迁移旧目录"
    - "1.1.1 2026-10-18 技能改为每个 agent 一个平级目录 agents/<agent>/.agents/skills/（ROUND5 R5.1）"
    - "2026-10-16 改为按需读取（只在提示中列出名称和用途）；内容尚未按核心 v6 重写，其中的旧流程用语和输出字段一律以 assistant-project-agent 和输出要求为准"
---

# Project资源结构

## 任务目标

为本次需要的资源确定可交付、可使用的位置。先沿用项目现有约定和框架结构，无约定时才选默认位置；
不扫描其他项目、不强制套 product/、不制造空目录，也不因旧命名与推荐不同而迁移。
Kernel 的 `resource_guide` 说明资源位置和系统限制；本技能不替代 Agent、MCP 或运行方式专项技能。

## 使用方法

结构性任务调用 `get_authorized_workspace(include_resource_guide=true)`，核对交付模式、授权根、地图和实际同类文件。
章程中的长期约定与具体交接按 assistant-task-continuity；计划位置不是已经存在的事实。
没有新式目录说明不单独阻塞旧项目；仅冲突影响本次交付时查明原因，需要用户取舍用 needs_input，工具或布局能力阻断用 failed，
必须改变批准需求/方案时按当前阶段的既有返工出口处理。目录说明不扩大权限，也不表示资源已经发布或可以执行。

### 工作区与正式目标
- 文件交付：正式目标为 `workspace://<root-key>/<相对路径>`；build 通过候选工具登记，候选区不是正式输出位置。
- Git 交付：build 只修改 Kernel 指定的 worktree，正式代码位置由 `project.yaml` 的 repository 决定；不另套一份 vault 业务目录。
  项目管理 agent 和其技能、章程仍在 vault；不能把仓库中的同名路径当成这些资源已更新，也不能越界补写 vault。
- 外部目录沿用已有授权根并写明对应关系，不因本规范复制或扩展授权。相对路径须注明相对 vault、仓库还是授权根。
- design 只在设计工件中说明目标和必要配套；build 才在本次批准范围和交付能力内实施。

### 系统资源与业务目录
当前 v2 的系统资源位置：

- Kernel 加载的 Agent：vault 的 `agents/<agent-id>/config.yaml` 与 `role.md`；引用资源须已可用或有本次支持的交付路径。
- Skill：放在使用它的 agent 自己的目录 `agents/<agent>/.agents/skills/<skill>/SKILL.md`（多个 agent 用就各放一份）；项目根目录和 `agents/` 这一级不放 `.agents/skills`；只在需要时添加`scripts/`、`references/`、`templates/`或`evals/`。
- MCP：`mcp/<server-id>/mcp-manifest.yaml`和实际入口；同步更新受影响Agent的enabled tools。
- 运行声明：文件交付在 vault 根目录、Git 交付在仓库根目录的 `jobs.yaml`；发布后由 Kernel 同步，登记与实际运行分开核实。
  `workflows/`、`services/`、历史 `schedules/` 文件本身不触发执行，不要求旧项目删除它们；运营 agent 不负责登记启用。
- `runs/`与`runtime/`由Kernel生成，不作为人工正式资源模板。

没有既有约定时，业务源码/内容可用 `src/` 或 `content/`，共享脚本可用 `scripts/`，Schema 可用 `schemas/`，
持久数据可用 `data/`，面向用户的交付可用 `artifacts/`；只建本次需要的目录。Skill 专用脚本仍放该 Skill 的 `scripts/`。
运行数据、静态交付和临时材料按用途区分，不按目录名判断批准状态；具体运行边界见 assistant-workflow-design。
目录移动涉及入口、引用、数据或消费者时，把影响、验证和回退写进原设计/实施章节，不新增目录审批环节。
普通 direct 授权根仍按现有交付规则处理；本技能不改变读写权限、候选发布方式或审批机制。
