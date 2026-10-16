---
name: assistant-mcp-design
description: 设计或审查Project MCP Server和原子Tool，包括Transport、生命周期、Manifest、Agent绑定、权限、审批和启动验证；仅在任务涉及MCP时使用。
metadata:
  version: "1.3.0"
  load: {roles: [pm], stages: [design, build], on_demand: true}
  changelog:
    - "2026-10-16 改为按需读取（只在提示中列出名称和用途）；内容尚未按核心 v6 重写，其中的旧流程用语和输出字段一律以 assistant-project-agent 和输出要求为准"
---

# MCP设计

## 任务目标

把需要实时系统、共享数据、凭据或外部副作用的能力实现为结构化、最小权限、可启动验证的MCP Tool，并让Project Agent Config能够按需注入正确Server和Tool。MCP提供能力边界，不承载业务推理流程。

## Tool边界

Tool保持清楚的原子业务动作：例如查询持仓只返回持仓事实；统计总市值、风险暴露和解释由Skill脚本或Agent完成。原子不等于把一次合理查询拆成大量无意义小Tool；分页、过滤和同一资源的必要字段可以保留。

每个Tool声明：名称、用途、输入Schema、输出Schema、read/write/external/trade效果、幂等性、超时、错误类型、凭据引用和审批要求。联网默认可用；Project文件Grant只授权文件操作，MCP Tool仍须独立核验凭据、账户和交易边界。写入、发布、交易类Tool必须有稳定幂等键、服务端权限校验和真实回执。

## Server边界

- Kernel scoped MCP只保留Workspace、Run、Work Mode、candidate、Map和路由等控制面工具。
- 无状态、启动快、仅供当前Invocation使用的Project MCP选择STDIO；源码位于当前Project，由Kernel受控launcher启动，STDIO不占端口。
- 长连接、昂贵初始化或多进程共享的领域MCP使用内部Streamable HTTP；Endpoint由Kernel通过 `env://ENV_NAME`等非敏感引用解析，并且只绑定环回或内部网络。
- 按依赖、信任、生命周期和副作用划分Server，不把全部Project Tool并入Kernel总Server，也不机械地每个Tool一个Server。

## 文件与配置

Project MCP放在 `mcp/<server-id>/`。必须包含 `mcp-manifest.yaml`和实际server入口；tools、services、tests等只在确有内容时创建，不生成空目录。Agent Config只引用已批准Server并列出enabled tools；不得保存凭据值或任意外部命令。

同一candidate必须包含MCP代码、Manifest和所有受影响Agent Config。新Tool在当前LLM轮次不能改变已经初始化的工具目录；发布后由下一轮Resolver生成新的 `ResolvedExecutionPlan`、固定MCP源码快照并注入。一次Plan内暴露给LLM的Tool名称必须唯一，冲突时拒绝解析或使用预先配置的稳定alias，不能自动猜测。

Prompt应向LLM说明每个Server是本地STDIO还是共享HTTP、生命周期和故障语义，但不得暴露凭据或敏感Endpoint。STDIO启动失败与HTTP网络失败必须区分；外部结果不确定时先对账再重试。

## 验证

Implementation至少验证导入、initialize、tools/list、正常调用、参数错误、权限拒绝、超时和有界关闭。HTTP服务更新先在临时端口通过健康和Tool目录验证，再切换或重启；失败时保留旧服务。Review核对Manifest、实际tools/list和Agent enabled tools完全一致。
