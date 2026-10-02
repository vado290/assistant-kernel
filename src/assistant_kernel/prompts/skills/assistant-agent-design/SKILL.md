---
name: assistant-agent-design
description: 设计或审查Project中的PM、Supervisor、Child、Operator和业务Agent，包括Role、Config、Scope、Skill、MCP、Trigger与I/O；仅在任务涉及Agent结构时使用。
metadata:
  version: "1.2.0"
---

# Agent设计

## 任务目标

为一个职责清楚的Agent生成可启动、可约束、可审查的Role和Config，使Kernel能够在每次Run中注入准确的模型参数、Project Scope、核心及专项Skill、MCP Tools、触发方式、输入输出和失败策略。Agent是执行配置，不是常驻进程。

## 设计边界

- Role只写身份、责任、权限边界、禁止事项和完成定义。
- Skill写完成该类任务的方法、判断、提问和工具使用。
- Config是机器执行的事实，不得依赖Role文字授予权限。
- 长连接、持续监听、频繁轮询和硬规则属于Service；Service在需要推理时创建Agent Run。
- Child Agent与PM使用相同Config Schema；原生子Agent使用Delegation合同，不创建第二套业务状态权威。

## Config必填项

使用 `agent-config.v2`，至少定义：`agent_id`、`agent_key`、`kind`、`status`、Role路径、核心与可选Skill、Scope profile、MCP Server及enabled tools、`entrypoints`、`triggers`、input/output Schema，以及 `harness` 中的provider、model、reasoning、timeout、retry和concurrency。Project文件权限由Kernel按Project Grant编译并固定到Run，不在每个Agent Config重复授权；没有真实运行支持的字段不得只写在Role中冒充已生效配置。

默认只能访问当前Project业务资料及已激活的Project文件Grant；所有Project Agent继承同一文件范围。`direct`工作目录可读写执行，`gated`正式目标使用candidate/Publisher；运行历史、缓存、凭据、其他Project和外部动作分别受限。

## 触发与常驻

Agent Config可以引用用户消息、Schedule、Workflow节点或Service事件，但自身不保存计时循环。每次触发创建独立Run，使用稳定幂等键。需要持续在线时创建Service配置，并明确它怎样触发Agent。

Agent必须通过Project Agent Registry动态发现。飞书直达和PM移交分别由 `entrypoints.feishu_slash` 与 `entrypoints.pm_handoff`控制；Supervisor、Child和Operator默认不允许直接进入。Role、核心Skill、身份、Work Mode Bundle或关键Scope变化必须考虑Session compatibility和安全rotation。

## 交付与审查

Implementation必须同时生成Role、Config、所需Skill/MCP绑定和I/O Schema；Review检查权限是否足够完成任务但不超过身份所需，并验证Registry、trigger和entrypoint确实能创建该Agent Run。每次Run最终由Kernel解析为不可变 `ResolvedExecutionPlan`，Agent不得自行扩大其中的资源绑定。
