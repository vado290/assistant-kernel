---
name: assistant-workflow-design
description: 设计或审查由Agent、MCP、Python、Service和人工门禁组成的Project Workflow；仅在任务涉及多节点编排或常驻服务时使用。
metadata:
  version: "1.2.0"
---

# Workflow与Service设计

## 任务目标

把Project的长期业务过程分解为职责单一、输入输出明确、可独立调度和恢复的节点，并选择普通Python、n8n、Agent或必要时LangGraph作为最小合适编排方式。Workflow协调节点，Kernel保存业务Run和批准事实，不能出现多个状态所有者。

## 节点类型

- Agent Node：研究、语义判断、综合和交互。
- MCP Tool Node：实时查询或受控外部动作。
- Python Node：确定性计算、转换、校验和批处理。
- Service/Event Node：长连接、持续监听和高频采集。
- Human Gate：业务批准或关键选择。
- Validation Node：确定性验证，不是LLM Agent。

每个节点定义ID、类型、目标Agent/脚本/Tool、输入Schema、输出Schema、Artifact路径、前置依赖、timeout、retry、幂等键、失败去向和验收条件。子Agent的新Run继承当前Project文件Grant；`direct`目录写入立即生效，`gated`目标仍走candidate/Publisher。失败或取消后可能已有文件变化，必须先对账再重试。

## 编排选择

固定顺序、少量分支和普通人工等待优先使用持久化Python阶段任务；渠道、通知和简单定时可用n8n。只有动态分支、并行汇合、循环返工和图状态恢复确实带来价值时才使用LangGraph。不能让Kernel、n8n和LangGraph同时拥有同一业务状态。

## 增量与恢复

周期节点读取watermark或上次成功边界，不从头执行整个Workflow。崩溃后根据Run、Artifact哈希和外部回执对账，不能盲目重放副作用。长驻Service只负责事件和确定性规则，复杂判断通过创建Agent Run完成。

Service必须说明进程所有者、单实例或并发策略、健康检查、启动停止、重连、背压、凭据来源和怎样创建幂等Agent Run；不能把只存在于进程内存的状态当作可恢复业务事实。

## 验证

Implementation完成变更节点及受影响路径的测试。Validation Runner按 `validation-report.v2`核查必需的结构、组件、合同或短路径；完整Workflow耗时数小时或数天时，可以按批准计划延后E2E并建立后续验收Run。Review检查覆盖范围、deferred风险，以及正常、空输入、部分失败、重试、恢复、并发、人工拒绝和外部系统不可用场景。
