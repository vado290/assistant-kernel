---
name: assistant-project-credentials
description: 在当前聊天中通过Discord或受支持通道的一对一凭据卡片管理Project本地凭据。
---

# Project凭据

## 任务目标

在当前Project执行需要账号、API Key、Token或密码的任务时，先复用已有凭据；缺失时在当前聊天中发问，由Kernel显示与该问题一对一关联的Discord或当前通道凭据卡片，用户填写后保存到Project Vault并继续原Run。

每个Project统一使用Project Vault内的`runtime/credentials.json`；例如`trading-risk-bot`在Windows宿主机上的当前文件是`H:\AI\assistant-data\vaults\trading-risk-bot\runtime\credentials.json`。新Project创建时由Kernel自动创建同名空文件，不得另起`secrets.json`、`.env`等平行凭据文件。

文件允许按服务、账户或环境组织嵌套字典。每个最终叶子必须是`{"value": <实际值>, "purpose": <该值的具体用途>, "env": <可选环境变量名>}`；不能只给整个凭据记录写一段总说明而让各个值含义不明。记录本身还保存`credential_key`、总体用途、状态、revision、首次创建时间、更新时间和最近请求/外部事件标识。

## 规则

1. 只有任务实际需要凭据时才调用`list_project_credentials`，不要在每个Run机械扫描。
2. `list_project_credentials`返回凭据名称、总体用途、各value路径及用途、环境变量名、状态、revision、创建时间和更新时间，不返回实际值。找到用途和字段符合脚本要求的记录后，可用`get_project_credential`读取并使用；Project内所有Agent均被视为可信。
3. 缺失时选择稳定、清晰的`credential_key`，写清记录的总体用途；每个卡片字段的`label`必须说明该value的具体用途，字段`name`优先使用目标脚本实际读取的环境变量名，然后调用`request_project_credential`。总体用途和字段用途均不得只写“API Key”之类无法区分服务、环境或作用的泛称。
4. Tool返回的`question`必须原样放进最终`needs_input.questions`；不得修改`question_id`、`input_schema.request_id`或字段名。问题属于当前聊天，Discord等通道会把它呈现为一对一凭据卡片；不要改成脱离聊天的其他流程。
5. Kernel回报“凭据已保存”后，重新调用`list_project_credentials`确认，再继续脚本或API任务。
6. 认证失败、用户要求轮换或用途对应的值已变化时，对同一`credential_key`再次发起卡片；新提交覆盖旧值、保留首次创建时间、递增revision并更新用途、更新时间、request ID和外部事件ID。
7. 只有用户明确要求删除时才调用`delete_project_credential`。
8. 不得在最终自然语言回复中复述凭据值；不得声称已保存，除非Kernel Tool或恢复答案明确返回`saved`。

## 缺失凭据的最终输出

```json
{
  "outcome": "needs_input",
  "summary": "继续任务需要Project凭据。",
  "questions": [
    {
      "question_id": "credential:<Kernel返回值>",
      "question_text": "<Kernel返回值>",
      "required": true,
      "input_schema": {
        "kind": "project_credential",
        "request_id": "<Kernel返回值>",
        "credential_key": "<Kernel返回值>",
        "fields": []
      }
    }
  ]
}
```
