# 历史归档：多图改造验收记录 — 2026-09-04（该运行机制已退役）

## 已验证

- 实际 AI 容器 `/opt/assistant-kernel-venv`，完整测试结果：**33 passed in 39.78s**。
- PostgreSQL 集成测试自动创建独立数据库；未向生产业务表注入测试任务。
- 多图、多版本隔离、原文件修改不影响快照、注册不执行模块、哈希校验。
- 并行中断答案收集、同 ID 连续中断（含嵌套子图）、false/0/空字符串/null。
- 独立 Python 子进程载入旧图快照并从 PostgreSQL 恢复成功。
- checkpoint 写入后、业务提交前崩溃重试；业务提交后、job 完成前重复处理。
- 并发相同 job、重复批复、冲突批复、失效租约、死信同步及显式重试、排队取消。
- 新 ProjectBuild 三轮审批到成功、旧 ProjectBuild 审批链接与 checkpoint 兼容。
- 原 python_script Routine Workflow 仍进入原 Python 执行队列。

## 已部署

- `0005_multigraph.sql` 已应用到实际 PostgreSQL。
- 部署前 running control jobs = 0；没有需要回填的活跃旧 ProjectBuild task。
- 仅重启 kernel-api / kernel-mcp / kernel-worker；均 RUNNING。
- `/health/ready` = ready；新 responses / retry / versions 路由已加载。
- 注册表只读接口返回正常，生产 control jobs 未增加。
- 隔离测试数据库已全部自动清理。
- 未调用收费模型，未改 n8n、Compose、IB/VNC/trading 或 Docling。

## 备份

`H:\AI\assistant-data\backups\multigraph-20260904-source`

- 改动前 src、migrations、README。
- `postgres-before.dump`：assistant + assistant_checkpoints 两个 schema。
- PostgreSQL custom dump 可由 pg_restore --list 正常读取。
- dump SHA256：`7066DE75AC6531C58EECB72E86CACED4E0ABA58E1393A36695B456C679B246FA`。

## 验证边界

端到端业务验证使用 FakeStages / 确定性图和独立数据库；生产仅做迁移、进程与
接口只读检查。没有声称已进行真实 LLM 生成、飞书送达或外部副作用 exactly-once 验证。
动态图模块是受信任单文件代码快照，不是任意 Python 沙箱或完整依赖环境快照。
详细契约与恢复限制见 MULTIGRAPH_RUNTIME.md。
