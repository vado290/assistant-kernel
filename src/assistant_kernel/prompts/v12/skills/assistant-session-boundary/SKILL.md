---
name: assistant-session-boundary
description: 在阶段安全边界建立正确的物理会话，承接业务事实并保原生 compact。
---

## 输入
当前 Task/模式/TurnSettlement，目标模式建议，有效需求/设计/批准，待答答案，实际执行/清理回执，必需子任务，Project约定、profile与装配清单，state_revision/context_epoch。

## 方法
1. 同阶段且没有真实不兼容来源时建议 keep；跨阶段建议 fresh/phase_changed；audit 独立，明确用户新会话和新Task亦 fresh。
2. 正在运行先结算或核明确取消及实际效果。必需 child/问答/批准或效果缺口写阻塞，不重做业务。
3. Supervisor 绑定当前来源和目标；Kernel 核版本/owner/批准，在短事务提交模式及唯一边界。固定同一 Prompt 供确认及实际派发。
4. 新调用省去上一阶段原始聊天，从有效 Artifact 重建必要内容；必要原消息按引用读取并记 ContextManifest。
5. 实际 Harness 不使用旧 native_id 创建新会话；同阶段 resume 使用原 ID。记录新 ID 和理由，不删除原 Session/Trace/批准。
6. 启动失败进入原有限恢复；只补会话/派发，不生成 LLM 接续摘要，不回退真实动作。

## 输出与验收
SessionRequest、Kernel BoundaryReceipt、ContextManifest 和缺口。验证跨阶段未自动载入原始聊天、需求/批准/证据仍能追溯、同阶段可 resume、原生 compact 未被旧阈值截断。

## v1.2运行与配置联动
只将实际载入的配置/背景子集用于兼容性；同阶段resume、跨阶段fresh及有效Artifact恢复保留。常驻模型上下文在下次成功维护重启重建，不改变PM正常边界。
