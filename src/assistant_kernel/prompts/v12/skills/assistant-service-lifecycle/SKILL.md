---
name: assistant-service-lifecycle
description: 交付和运营常驻Python程序，管理启停、就绪、资源、更新和恢复。
---

## 前置
读取已批准RuntimeUnit定义、ServiceManifest、Project约定、实际ProcessManager能力及启用/恢复范围。默认为已确认的自动有限恢复；具体次数/退避/预算（可明确继承全局值）、健康判据和实际启用批准缺失时，候选保持未启用，不猜具体参数。

## Build和检查
程序提供main入口、ready/heartbeat/工作项result事件、SIGTERM处理与子进程join。固定包和环境，秘密使用Ref。隔离试运行验证就绪、正常工作、优雅停止及异常，记录能力/效果/cleanup，不将PID当健康。

## 启用与控制
Kernel先核依据/版本/资源/原实例，再提交desired_state与幂等控制身份。Adapter只处理所属Unit的后端program/group。启动后保存instance/generation和真实probe。暂停/停止先阻新工作；接收cancel不等已经停止。

## 重启与更新
按已批准策略对账实际实例及既有业务效果；用户停止优先。有限重启有次数/退避/稳定窗口，不因维护循环再跑就清零。未知身份或效果先暂停恢复并通知。
更新采用prepare/verify/drain-stop/核清理/start/probe/commit；失败保新旧版本和实际结果，按已批准恢复范围处理，不默认同时双开。

## Operate
Project持有服务，源Task可完成。PM换阶段不误停服务。资源和成本有界；异常事件按既有Outbox通知，Role/Skill或代码改进形成提案，不自动改规则。

## v1.2运行与配置联动
自动维护由已授权的 Kernel Schedule 触发；人工停止或故障保护优先，关联规则暂停。手动维护遇忙/待答保留工作并通知PM；明确空闲Kernel核后同代码重启，疑问才调用Supervisor。核旧组清理和新ready，刷新当前选中背景但不灌旧聊天；维护不清零故障计数。

## 已确认实施契约修订
所有工作含纯 I/O 和待答项都先登记实例 work_control；维护闸门与工作入场共用 Unit 锁。CPU/模型预留属于子记录，不另造任务系统；CPU 池声明实际 start_method。

## 编制与调用
先用 resolve_runtime_config 获取实际配置 snapshot_ref 和 bindings；ServiceManifest.resources.resolved_config_ref 使用该快照，RuntimeUnit.config_bindings 保存明确继承/覆盖项。write_runtime_document 支持 python-service-manifest 和 runtime-unit-definition，登记为停态；首次启用走已批准设计对应的 runtime_handoffs。后续手动控制用 control_runtime_unit，来源同时包含具体启用范围及当前确认过的 Operate Prompt 消息，不以旧交接覆盖后来的暂停或停止。

固定业务环境需要实际安装提供 ServiceClient 的 Kernel SDK。普通主程序使用 ServiceClient.ready()/heartbeat()；所有有限工作经 run_work(stable_key, input, callback) 登记，callback 收到该 work 的 client，可用 cpu/model/tool/effect、ask 或 run_graph。输出文件写入 work.output_root。程序自身为阻塞 I/O 设超时；Kernel 管执行登记与受管步骤预算，不宣称能安全中断任意 Python 线程。

常驻进程由现有 supervisord 的受控 program 托管，autorestart=false；Kernel 维护消费者负责有限恢复，不占普通 finite Worker。遇未完成工作、未知效果或未核清理，保原实例和问题，通知 PM 后等待明确处理；不得另起一个进程重跑。更新先排空/明确取消旧工作，采用新定义后保持停态，再以新范围启用。手动重启忙时跳过，不留下日后自动补跑的请求。
