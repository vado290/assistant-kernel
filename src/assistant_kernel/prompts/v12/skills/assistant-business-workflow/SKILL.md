---
name: assistant-business-workflow
description: 设计和验证Python中的Agent图与业务状态，不重复Kernel的项目控制。
---

## 使用条件
多分支、状态恢复、节点并行或人机等待确有需要时使用；简单脚本直接执行。LangGraph可选，固定依赖版本并测试实际导入。

## 设计与实现
声明节点/Agent输入输出、边/分支、正常结束、失败、取消和等待路径。纯函数节点不伪装LLM Agent；Agent加载固定Role/Skills/模型/MCP。业务状态只放业务字段，Project/Task/批准/阶段只使用Kernel引用，不另维护可冲突的权威。

业务线程命名空间包含Project/Unit/Graph/BusinessThread。需要持久化则选现有可用backend或最小依赖，保存真实checkpoint/effect refs。有限等待用户时释放Worker；答案通过Kernel原问题引擎关联正确业务线程并恢复，禁止从头盲重跑。

并发节点、模型调用、I/O与CPU池分别有上限，结果写入采用明确合并规则。外部效果使用工作项幂等身份/实际回执；库重试不叠加Kernel重试而造成无限次数。

## 检查
覆盖主要分支、无工具/模型失败、部分结果、预算/取消和有checkpoint的恢复。无持久状态需求可标not_applicable并说明，不能将未测图路径写通过。

## v1.2运行与配置联动
图恢复可重入中断节点前段，外部效果必须有逻辑工作项幂等或既有回执；保累计预算。升级遇待答先完成或明确取消；手动重启同样保护待答。

## 已实现的最小业务入口

普通有限程序直接运行；确有 Agent/图需求时才在固定定义中声明。程序读取 `ASSISTANT_RUNTIME_CLIENT_PATH`，用 `assistant_kernel.runtime_client.RuntimeClient` 调用 `model(agent_id,input,key=...)`、`tool(agent_id,server_name,tool,arguments,key=...)`。参数和可用工具来自已受审、已启用的固定 Unit；不向程序传 Kernel 管理 token、Worker claim 或数据库密码。输入输出 schema、Role/Skill SHA、实际模型用量与工具结果分别留证据；工具发现和最终回复正确均不等于工具实际成功。

有限 CPU 节点用 `client.cpu(key,slots)` 复用父 Run 已预留的峰值额度，收到准入后才计算并在退出时释放。`client.effect(key,description,callback)` 的 key 必须在 manifest.expected_effects 中；先登记再行动，完成结果可复用，pending/unknown 不重放。回调返回只是程序报告，真实业务/effect验收仍要实际证据。

LangGraph固定依赖放在 Project 执行环境，Kernel 本体不强制安装图框架。固定图描述为 `business-workflow-graph.v1`，nodes 为实际节点名；完整边、分支和实现由已固定代码包确定。用 `client.run_graph(build,input)`，build 接收实际 SQLite saver 并返回已编译图；节点通过 `interrupt({'questions':[...]})` 提问。命名空间含 Project/Unit/Graph/线程。SDK保存真实SQLite快照，Kernel建立原QuestionGroup；进程退出并核清理后释放Worker。正式答案只恢复原快照、逻辑工作项和固定版本，不从起点盲重跑。业务图、CPU和effect都不维护Kernel阶段或批准。

业务模型必需固定 `runtime-resource-policy.v1`：project_id、max_model_calls、max_total_model_seconds，可收紧 max_project_model_calls_24h。普通调用记实际用量或unknown，累计账本不因resume清零；`runtime-budget-snapshot.v1`只引用现有账本，known_*合计与unknown_usage_calls分开。硬token/金额要求必须可由实际适配器保证；当前不能保证时启动前拒绝。原生CLI使用独立业务native home和原Project登录，模型及声明MCP来自固定绑定，不继承Kernel MCP。

Python Skill 声明 execution_style=python_step 时，implementation_ref必须在固定代码包模块列表中，模块提供 `run(value,client)`；`client.python_skill(agent_id,instruction_ref.id,value)` 执行该快照。仅prompt_instructions的Skill仍作为固定模型指令加载。
