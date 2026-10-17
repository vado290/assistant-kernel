# Assistant Kernel 0.24.8 瘦身与并发修复交付报告

交付日期：2026-10-04。生产已部署 `0.24.8/core5.v7`，自动测试和生产脚本并发验证通过。原等待用户任务、问题和问卷能力保留。

## 修改与精简

| 排名 | 原问题 | 已完成修改 |
|---|---|---|
| 1 | Project 内所有 execution Run 互斥 | 使用 runs.queue_kind/mutex_key；独立 Python 和 candidate 子任务并行，工程主线及共享写入按明确键互斥 |
| 2 | 相同正文取消较早请求 | 删除正文指纹取消，保留事件幂等；不同事件的相同正文分别保留 |
| 3 | 忙碌输入没有确定消费 | 绑定原任务、固定本轮输入、成功提交原子确认；后到输入阻止旧批准/终态，发布期间补充在发布后继续处理 |
| 4 | Python HTTP 重试创建新 Run | 稳定 Idempotency-Key；v2 复用校验；不同参数、父任务或写入声明返回冲突 |
| 5 | 两套 worker 与重复 lane 推断 | 删除聚合入口、worker_claim_loops 和运行时 lane 推断；部署交互 2、脚本 4 个进程 |
| 6 | 旧 Stage 执行链长期保留 | 退役创建、推进、返工和权限恢复写路径；保留短错误入口与历史读取 |
| 7 | 无效配置及未启用 HTTP 采集 | 移除 Supervisor max_retries/max_fetch_requests、HTTP 收集缓存及依赖；三个 Project 的无效键已备份后移除 |

工程键为 engineering:{project_id}，共享写入键为 shared-write:global。同会话唯一约束保留。Python 父任务/触发消息只表示来源，不形成运行锁。发布线程保持心跳，记录进程身份并支持取消、回退；结束后清理可复用 worker 的 PID 记录。

## 净代码变化

对比实施开始时保存的 **0.24.7 工作区**，包括原有未提交问卷代码，而不是旧 Git HEAD：

| 指标 | 实施前 | 实施后 | 净变化 |
|---|---:|---:|---:|
| 生产 Python 行数 | 28,357 | 27,262 | -1,095，约 -3.86% |
| 生产 Python 非空行 | 26,922 | 25,817 | -1,105，约 -4.10% |
| Python 文件 | 69 | 70 | +1 个小型调度模块 |

旧公共入口保留短退役错误，新输入消费和调度增加少量辅助函数，因此函数数量不是削减指标。收益来自删除旧业务分支，未通过搬动大函数计算收益。原 services/questionnaire.py 与实施前备份逐字节一致。

## 验证

- 完整测试 **297 passed**，使用独立 PostgreSQL 数据库；包括真实 Python 子进程、领取竞争、输入消费/重启、审批、问卷、Validation、冷审计、发布/对账、两个并行子任务汇总和迁移往返。
- 15 个旧 Stage 执行合同测试被当前 v2 审批、验证、返工、发布和退役拒绝场景替代；名单保存在 slim-retired-tests.json。未简单跳过失败测试。
- 生产 API 和真实 worker：同一 kernel Project 四个无网络脚本最大同时运行 4 个，执行窗口重叠 **3.960 秒**，全部成功、输出独立。交互池与 API 持续就绪；幂等重试返回原 Run。
- 安装包与当前源码、Role、Skill 资源 **94/94 SHA-256 一致**，版本化 venv 的 pip check 无冲突。验收清除继承的 PYTHONPATH，避免旧 Conda 包干扰。
- /version 为 0.24.8/core5.v7；readiness 为 ready，interactive/runtime ready 为 true，Discord ready，无降级项。
- CORE5_VERIFY_OK；六张业务表保留，三个 Project 仍为 single-agent.v2；没有重建 AI 容器或重启 Docling。
- 等待用户 Run b894b976-95d9-400a-9caa-61fd39265e9c 仍为 waiting_user；原问题、交互序号 1、request/result 不变。迁移前后业务签名均为 762e01dee284bd73af4eec66da59af36。

真人 Discord 发消息和点击原卡片尚未用用户账号实测。已验证连接、健康和隔离数据库中的渠道业务链，不能将连接就绪等同于真人端到端验证。

## 接口约定

- v1/python-executions 保留原返回格式，新增可选 shared_write=false 和 Idempotency-Key；提交重试沿用同一键，缺失键表示新执行。
- v2/runs 必须显式提供 run_type；缺失返回 422，stage 返回 409；相同键不同请求或父任务、写入声明返回 409。
- 独立读取、计算及自身输出默认并行。可信脚本写正式文件或共享外部状态时声明 shared_write=true；声明不增加权限，也不能自动识别任意脚本的隐含副作用。
- 审批、租约、取消、Session 轮换和恢复合同保留。异常仅阻塞相同会话/互斥组，独立脚本继续。

## 恢复与证据位置

- 初始源码、未提交改动及项目配置：assistant-data/backups/kernel-slimming-20261003-0247。
- 切换前数据库、原 Supervisor 配置与指针：assistant-data/backups/kernel-slimming-20261004-predeploy。
- wheel、SHA manifest、总补丁、三组审阅补丁、测试日志、迁移签名、生产 canary 和健康记录：assistant-data/runtime/releases/0.24.8。
- 完整 implementation.patch 对任务基线执行 git apply --check 通过。分组 patch 用于审阅；恢复使用完整补丁、wheel、原配置和兼容数据库，不逐组盲目回放。
- Git 改动未提交，未创建或推送 PR/tag；版本标签不是 Git 恢复点。
- 迁移/降级拒绝 running/reconciling；降级存在 pending 输入时保留数据并拒绝自动重放。恢复旧索引前收敛活动 Run。
- 部署失败核对真实 Schema，必要时受控降级，再恢复配置和指针；不能只恢复版本指针。

历史系统地图未重建，不作为新调度事实的权威来源。本轮以部署源码、Schema、配置和上述证据为准，没有追加全系统审计。
