---
name: assistant-python-execution
description: 固定脚本、环境和输入，提交受管执行并核结果及清理。
---

## 任务目标
区分辅助分析、批准试跑和正式执行，不把完整文件权限当业务批准。

## 步骤
固定入口、本地模块闭包、环境依赖、args、输入、工作目录、输出、资源和效果。扩根脚本可快照为Run工作包，不能因不在Vault就否认已授权执行权。

简单串行；I/O默认最多8，CPU默认池2、数值线程1。更大方案须具体资源策略批准，不让每Run自行扩大。预算不是OS硬限制。

提交现有runtime队列，不阻塞控制流程。每attempt独立日志/结果，核过程、合同、业务及整组清理。正常、失败、超时、取消、父先退出均核剩余成员。

## 输出
manifest、Run/attempt回执、产物及cleanup receipt。辅助依赖归invocation，交付依赖归Task，验收交接后长期对象归Project运营。

## 失败与验收
旧结果不能冒成功，效果未知先对账。取消受理不等已停；源/环境不匹配不静默换版。必要清理与合同满足才声明执行完成。

## v1.2执行规则（承接v1.1）
此专项适用于有限Python Run；service转assistant-service-lifecycle。关联正式RuntimeUnit/逻辑工作项及装配回执；每attempt输出、已有效果、父退出后组清理分别核，不以新Run ID重放工作。

## v1.2运行与配置联动
解释器报错中断即时登记真实stderr和通知义务到对应Project PM；普通warning/已处理异常不新增推断报警。逻辑工作项跨attempt/resume保幂等、累计预算和效果；每attempt输出独立。

## 已确认实施契约修订
CPU 池大于零必须声明实际 start_method；包括混合 I/O 程序。identity_unknown 保留已知 PID、未知身份字段为 null，不凭不完整身份杀进程或自动重跑，先对账。

## 固定有限执行接口

编制清单使用 `write_runtime_document(document_kind,payload)`：Build/Operate 可写 python-script-manifest 或 runtime-unit-definition；只有 Operate 可写 activation-report。Kernel计算固定Envelope/Ref，写入不登记、不批准、不启用。

Operate 在 `runtime_handoffs` 中交接登记、首次启用或已审版本更新。首次 enable 的 definition.approved_execution_scope_ref 与 manifest.purpose.authorization_ref 必须绑定本 Task 真实的具体设计批准消息，已批准设计明确含该 Unit 及适用启用计划。Kernel在Turn接受事务中核实际Review/publication后登记和启用；模型字段本身不构成批准。register只登记停态，update完成后停态，后来的人工暂停/停止不能被旧handoff撤销。

实际有限Run通过输出、业务检查、效果和清理后，以 action_receipt_refs/per_item_checks 引用真实Run或结算回执，写 activation-report，并将该Ref放入 runtime_handoffs.activation_refs。报告必须覆盖全部已设计Unit；Kernel核实际结果后才接受activation与Project归属，不能只引用启用命令或自报passed。

Build／Operate 使用 `submit_project_python`，提交已写入不可变文档的 `manifest_ref`、`inputs`、稳定 `idempotency_key`、`execution_kind`，以及正式执行的 `runtime_unit_ref`；首次辅助执行的 `runtime_unit_ref`、`logical_execution_id`、`resume_owner` 可以为 null。返回 ExecutionAdmission（含 Run Ref），排队不是运行成功；使用 `run_status` 等待本次结果。

辅助／试跑先读取 `current_conversation_route` 的 python_execution_context，绑定其中实际 creating_invocation_ref、creating_task_ref、当前 confirmed Turn 包及原确认消息；不可自造记录。它随创建调用结束而取消；模型需要结果时必须在当前调用内等待，不能提交后立即结束并把它当作独立后台任务。正式执行必须引用已注册且经具体授权启用的固定 Unit。

代码包使用 `python-code-bundle.v1` 的 files 清单（relative_path、ref）以及 `python-import-closure.v1` 的 stdlib_modules／dependency_modules。固定环境记录解释器路径与 SHA、Python minor、site-packages 路径、实际 distribution_versions；第三方依赖同时固定 lock／manifest。输入绑定使用 destination_relative_path 和 method(copy/reference)，工作目录可用 attempt://code，独立输出用 attempt://output。

共享写目标在 io.write_targets 中声明真实 workspace／vault URI，Kernel 将目录别名解析到实际路径参与串行；不以任意自造 resource key 代替目标。result.json 必须符合 output_schema，required_outputs 必须实际存在。简单业务检查可固定 `python-business-check.v1` 文件（json_pointer、operator=equals、expected）；没有业务检查时，business_status 保持 unverified，不能把退出码当业务验收。普通 Python 不强制装配业务 Agent 或 LangGraph。

io.secret_refs 仅接受 `secret://projects/<project_id>/<credential_key>`，从现有 Project 凭据 Store 解析该条目的已声明 env 字段；指定 revision_id 时须匹配当前版本，null 表示启动时采用该已授权条目的当前有效版本。回执只记实际版本与变量名，不保存值。不能以凭据覆盖解释器、路径、线程或 Kernel 执行控制参数。

本轮简单文件效果可使用固定 `python-business-check.v1`：source_kind=file_text、target_uri（必须是manifest.io.write_targets中的实际文件）、空json_pointer、operator=equals、expected及effect_keys。Kernel读取真实文件并保存本attempt不可变快照；只有对应effect_keys全部有通过的实际检查，effect_status才为verified。程序自报、exit0或模型回复均不能代替这种证据；其他外部效果未取得真实业务证据时仍为unknown。
