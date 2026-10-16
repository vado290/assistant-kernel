---
name: assistant-project-structure
description: 在创建或迁移Project级Agent、Schema、Skill、脚本、MCP、Workflow或Service时，按当前layout放置资源并补齐必要配套文件；普通内容编辑不使用。
metadata:
  version: "1.1.0"
  load: {roles: [pm], stages: [design, build], on_demand: true}
  changelog:
    - "2026-10-16 改为按需读取（只在提示中列出名称和用途）；内容尚未按核心 v6 重写，其中的旧流程用语和输出字段一律以 assistant-project-agent 和输出要求为准"
---

# Project资源结构

## 任务目标

在不扫描其他Project、不复制旧布局、不制造空目录的前提下，为当前Project的新资源选择正式位置，并同时生成该资源运行所需的最小配套文件。目录规则来自Kernel返回的 `resource_guide`；本Skill只规定如何使用这些规则，不替代Agent、MCP或Workflow专项Skill。

## 使用方法

结构性任务第一次调用 `get_authorized_workspace` 时设置 `include_resource_guide=true`。先读取 `project.yaml` 的 `layout_version`、当前同类资源和guide，再确定candidate目标；未知布局、guide与现有正式文件冲突时返回blocked，不自行猜路径。

当前v2基本约定：

- Agent：`agents/<agent-id>/config.yaml`与`role.md`；引用的Skill、Schema和MCP必须同时存在或包含在同一candidate。
- Skill：`.agents/skills/<skill>/SKILL.md`；只在需要时添加`scripts/`、`references/`、`templates/`或`evals/`。
- Project共享脚本：`scripts/`；Skill专用脚本放对应Skill的`scripts/`。
- JSON Schema：放Project的`schemas/`下，使用稳定的相对路径，并由Agent、Workflow或Tool配置显式引用。
- MCP：`mcp/<server-id>/mcp-manifest.yaml`和实际入口；同步更新受影响Agent的enabled tools。
- Workflow、Service 分别使用 `workflows/`、`services/`；没有对应执行器时不能声称已运行。业务定时使用 Kernel Schedule；在 schedules/ 保存固定候选，Operate 经批准登记启用。
- `runs/`与`runtime/`由Kernel生成，不作为人工正式资源模板。

Agent、Role、Skill、MCP及其他受控正式资源的新建、更新和删除仍通过当前Run的candidate工具完成；普通`direct`工作目录按Project文件Grant执行并保留变更证据。本Skill不授予额外Scope、MCP、发布或审批权限。
