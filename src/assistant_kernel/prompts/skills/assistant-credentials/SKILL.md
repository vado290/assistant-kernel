---
name: assistant-credentials
description: 项目凭据（登录账号、API 密钥等）怎么查、怎么向用户要、怎么命名和使用：先查后问，存进项目凭据库，不写进任何文档。
metadata:
  version: "1.0.0"
  load: {roles: [pm, supervisor], on_demand: true}
  changelog:
    - "1.0.0 2026-10-18 新增（ROUND5 R5.2）：每个项目一个凭据库 runtime/credentials.json，先查后问"
---

# 项目凭据

## 凭据在哪里
每个项目一个凭据库（`runtime/credentials.json`，由 Kernel 管理），只属于本项目。每条凭据有：key、值、
用于什么服务（service）、用途（purpose）、谁在用（used_by）、更新时间、来源。

## 先查后问
1. 需要登录某个网站、调用某个外部服务时，先用 `list_credentials` 看有没有对应的 key（只列说明，不含值）。
2. 有：用 `get_credential(key)` 取值来用。
3. 没有：在本轮输出 needs_input，在那个问题上加 `credential`（key、service、purpose），例如
   `{"id": "q1", "text": "请提供币安 API Key 和 Secret（只读权限即可）", "credential": {"key": "binance_api",
   "service": "币安 API", "purpose": "查询行情与持仓"}}`。这类问题直接出卡片给用户，用户填写后 Kernel 存进凭据库。
4. 一次只问真正缺的；用户已经提供过的不要重复问。

## 命名
- key 用小写字母、数字和下划线，格式建议 `<服务>_<用途>`，如 `binance_api`、`ib_account`、`smtp_report`。
- service 写清是哪个系统（含网址或环境，如"币安主网"），purpose 写清拿来做什么，方便以后一眼认出。

## 使用规则
- 凭据的值不写进需求、设计、报告、地图、代码、日志或任何消息；文档里只写 key。
- 产物的脚本和服务不要把值写死在代码里：在 `product.yaml` 声明需要哪些 key，运行时由 Kernel 以环境变量提供。
- 发现凭据失效（登录失败、被拒绝），在报告里说明是哪个 key，由用户更新；不要猜测或尝试别的账号。
- Supervisor：用户问"保存了哪些凭据"时用 `list_credentials` 回答；用户要删除时提示发送 `/secrets delete <key>`。
