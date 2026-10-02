---
name: assistant-history-retrieval
description: 在当前Project内按最近N天和分页读取历史对话，用于核实过去的决定、批准、约束和原始表述；不提供语义或跨Project搜索。
metadata:
  version: "0.1.0"
---

# 最近历史检索

## 任务目标

在当前上下文不足时，通过 `list_recent_history` 找回当前Project近期的可读对话证据，并用消息ID和时间说明结论来源。Project范围由Kernel固定；不得要求用户或工具参数选择另一个Project。

## 何时查询

仅在下列情况查询，不要每轮例行调用：

- 用户明确提到“以前、上次、当时、某天、之前决定或批准”。
- 回答依赖过去的决定、约束、授权、原始措辞或未完成事项，而当前上下文没有充分证据。
- 长时间后恢复工作，需要核对近期原始记录而不应只依赖摘要。
- 当前说法与Session handoff或Project资料可能冲突，需要回到原始消息核实。

普通闲聊、当前消息已经足够、或只需要Project文件事实时不要查询历史。

## 查询方式

1. 根据用户给出的时间确定 `lookback_days`；没有明确时间时默认365天。
2. 先使用 `scope=current_conversation`，避免把同一Project内不相关渠道记录混入。
3. 用户明确要求Project整体历史，或当前会话没有找到而任务确实需要时，再使用 `scope=project`。
4. 每页默认最多50条；只有当前页不足以回答时才使用返回的 `next_cursor` 继续，最多读取完成任务所需页数。
5. 工具按时间倒序返回记录，不执行关键词、语义或向量检索；不得声称已经找全语义相关内容。

## 回答与证据

- 区分用户原话、Agent回复、审批问题、系统路由和模型总结。
- 关键历史结论附带 `message_id` 和 `created_at`；如与Run有关再附 `run_id`。
- 没找到时说明实际查询Project、天数和分页范围，不得用摘要或印象补造历史事实。
- Session handoff和Run summary是导航线索，不替代原始消息。涉及“用户是否批准”时必须以对应原始问题和回答为准。

## 边界

本Skill不授予跨Project读取、向量检索、历史修改、消息删除或审批权限。它也不替代 `list_projects`：Project定位由大管家根据Project Card完成，历史工具只查询已经确定的当前Project。
