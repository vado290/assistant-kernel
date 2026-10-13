---
name: assistant-interaction-followup
description: 根据真实新输入复核待答问题，向Supervisor提交处理建议。
---

## 输入
当前输入、逻辑题与内容版本、已有正式答案、共享草稿、owner、当前状态及来源。

## 步骤
逐题判断明确答案、倾向、仍需询问、改题、延期、撤下或澄清。默认保留必要题，倾向不是正式答案，延期不解除阻塞。

普通与引用文字按同口径处理；旧按钮不能变自然聊天。部分答只解对应题，独立组不互废，新题不因自身镜像作废，新版不搬旧草稿。

## 输出
题ID/版本、建议动作、理由、来源引用和未决项。子代理不发卡，不接受答案；Supervisor验收后Kernel按顺序、owner与版本提交。

## 失败与验收
stale结果不覆盖新答，不能恢复取消owner。副本、翻页、投递及无事件等待不触发新复核。明答必须可追溯真实用户内容。

## v1.2执行规则（承接v1.1）
业务needs_input复用原问题引擎，补runtime_owner/业务线程/图checkpoint，镜像仍在Sup/PM既有入口。明确答案恢复对应工作项，不直接从头重跑已产生效果。

## v1.2运行与配置联动
维护问题补control_ref、definition_ref、当前实例/到期；PM可操作，同一问题引擎按题接受一次。答案不直接重启；Kernel重核版本/状态/窗口，过期或已忙跳过。

## 已确认实施契约修订
runtime_business 使用 runtime_both，Supervisor 主入口与 PM 镜像同样可答；runtime_maintenance 使用 runtime_pm_primary。两入口引用同一内容版、草稿和答案。
