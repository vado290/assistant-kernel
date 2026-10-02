---
name: assistant-project-structure
description: 在创建或迁移Project级Agent、Schema、Skill、脚本、MCP、Workflow、Service或Schedule时，按当前layout放置资源并补齐必要配套文件；普通内容编辑不使用。
metadata:
  version: "1.1.0"
---

# Project资源结构

## 任务目标

在不扫描其他Project、不复制旧布局、不制造空目录的前提下，为当前Project的新资源选择正式位置，并同时生成该资源运行所需的最小配套文件。目录规则来自Kernel返回的 `resource_guide`；本Skill只规定如何使用这些规则，不替代Agent、MCP、Schedule或Workflow专项Skill。

## 使用方法

结构性任务第一次调用 `get_authorized_workspace` 时设置 `include_resource_guide=true`。先读取 `project.yaml` 的 `layout_version`、当前同类资源和guide，再确定candidate目标；未知布局、guide与现有正式文件冲突时返回blocked，不自行猜路径。

当前v2基本约定：

- Agent：`agents/<agent-id>/config.yaml`与`role.md`；引用的Skill、Schema和MCP必须同时存在或包含在同一candidate。
- Skill：`.agents/skills/<skill>/SKILL.md`；只在需要时添加`scripts/`、`references/`、`templates/`或`evals/`。
- Project共享脚本：`scripts/`；Skill专用脚本放对应Skill的`scripts/`。
- JSON Schema：放Project的`schemas/`下，使用稳定的相对路径，并由Agent、Workflow或Tool配置显式引用。
- MCP：`mcp/<server-id>/mcp-manifest.yaml`和实际入口；同步更新受影响Agent的enabled tools。
- Workflow、Service、Schedule：分别使用`workflows/`、`services/`、`schedules/`；当前Runtime没有对应执行器时只能作为未激活设计，不能声称已运行。
- `runs/`与`runtime/`由Kernel生成，不作为人工正式资源模板。

Agent、Role、Skill、MCP及其他受控正式资源的新建、更新和删除仍通过当前Run的candidate工具完成；普通`direct`工作目录按Project文件Grant执行并保留变更证据。本Skill不授予额外Scope、MCP、发布或审批权限。
