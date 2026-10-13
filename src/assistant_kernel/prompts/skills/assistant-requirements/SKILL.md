---
name: assistant-requirements
description: 分析和扩展用户需求，开展必要调查与澄清，形成可批准的需求文档；批准后进入独立design阶段，不实施或发布。
metadata:
  version: "2.1.0"
---

# Requirements Work Mode

## 目标与输入

以父Task Run的`accepted_documents.task_contract`、当前用户Message、Project Contract、相关
Semantic Map组件和来源证据为输入，形成完整需求产物 `task-contract.v1`。Map与摘要用于导航，不能
覆盖用户原文、数据库状态或正式源码。

Task Contract必须明确goal、included/excluded scope、requirements、acceptance criteria、
constraints和source refs。判断任务属于首次建设、迭代或修复；迭代/修复先读取现有实现、
测试、回执和Incident，不从空白覆盖。

Requirements Work Mode是只读正式实现的模式。可以使用互联网、已配置数据源和只读工具
调查，但不能写candidate、启动生产服务、发布、连接交易账户或执行交易。任何实际工具结果
都要保留Trace/Artifact/Receipt引用；失败不得写成已验证。

## 调查与澄清

- 先查Project内最相关的Map组件和权威来源，再按需扩大读取；
- 区分用户决定、已验证事实、Agent声明、外部来源和建议；
- 主动寻找会否定方案的证据，并覆盖失败、恢复、幂等、外部副作用和验收；
- 只把会改变架构、业务结果或验收标准的问题作为`needs_input`；
- 每次优先提出最少的关键问题，先说明当前理解与影响，不能要求固定口令。

适合独立检索、基线清点、接口核对或反例审查时，按
`assistant-subagent-orchestration`提出有界Delegation Request，让子Agent返回结构化Result与
证据引用，避免把完整transcript装入PM上下文。

## 组件选择

只为任务实际需要的能力选择Agent、Skill、脚本、MCP、Workflow、Service或Schedule：

- Agent处理语义判断、研究、综合和用户交互；
- Skill规定一类工作的判断与方法；
- Python处理确定性转换、计算、校验和批处理；
- MCP连接实时系统、共享数据或受控外部效果；
- Workflow处理多节点顺序、并行、重试和人工门禁；
- Service承担长连接、持续监听或高频确定性规则；
- Schedule只按时间创建工作，不等于常驻Agent。

缺少本Task确实需要的专项Skill时，在Action Plan或Result中列明缺口，不凭不完整规则编造
设计。高频采集优先脚本/Service；简单固定流程不引入LangGraph。

## 完成与批准

Requirements成熟前返回`continue`或`needs_input`。全部关键问题关闭、验收标准可执行、边界
明确后，按 Kernel JSON 合同返回需求批准请求，并在`document_markdown`中给出完整Task Contract内容。
Kernel先写不可变Task Contract revision，再让用户Decision绑定该revision与SHA；不得把批准
结果回写合同正文。

需求批准后进入独立 design，设计批准后才进入 implementation。调查期间可以讨论初步
可行方向，但不得把初步方向当成已批准实施方案。用户要求修改时继续Requirements并生成新revision。
Design 或 Build发现已批准合同必须变化时，由PM
返回`return_to_requirements`，Kernel清除批准、切回Requirements并创建新物理Session。

需要用户回答时返回Kernel合同要求的JSON，例如：

```json
{"outcome":"needs_input","summary":"当前已确认需求","questions":[{"question_id":"稳定编号","question_text":"完整问题原文","required":true}]}
```

不得声称未发生的读取、测试、批准、外部效果或发布。
