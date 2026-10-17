## 定时作业与常驻服务：jobs.yaml（Kernel 依赖的格式）
项目里需要定时运行或长期运行的东西，一律在项目文件 `jobs.yaml` 中声明，不要另想办法（不写 crontab、
不自己起后台进程）。它和其他文件一样走"候选 → 验证 → 批准发布"：**发布即登记**，Kernel 随后按它定时运行、托管服务、
失败重试并在 Supervisor 频道告警；用户用 `/jobs` 查看、暂停、恢复、立即运行。验证阶段 Kernel 会检查这个文件。

```yaml
schema_version: runtime-manifest.v1
jobs:
  - id: daily-report                # 2–40 个小写字母、数字或连字符；项目内唯一
    type: script                    # script | agent | service_restart
    entry: scripts/daily_report.py  # script：项目内脚本（.py 用 python3 运行，.sh 用 bash，其他直接执行）
    args: ["--date", "yesterday"]   # 可选
    schedule: {kind: cron, expression: "0 8 * * *"}   # 或 {kind: interval, anchor: "2026-10-16T00:00:00+08:00", seconds: 3600}
                                                      # 或 {kind: once, at: "2026-10-20T09:00:00+08:00"}
    timezone: Asia/Shanghai         # 可选，默认 Asia/Shanghai
    timeout_seconds: 600            # 10–21600
    retries: 1                      # 0–5，失败后重试次数
    notify: supervisor              # 失败告警发到哪个频道：supervisor | pm | none
  - id: weekly-insight
    type: agent                     # 产物内部的 agent：每次运行一轮模型（只读），结果发到频道
    entry: agents/analyst           # 需要 agents/analyst/config.yaml 和 role.md；它的技能是 load.roles 含 analyst 的技能
    prompt: 读取本周的日报数据，写出三条最重要的经营发现和建议。
    post_result: pm                 # 结果发到哪个频道：pm | supervisor | none
    schedule: {kind: cron, expression: "0 9 * * 1"}
services:
  - id: price-listener              # 常驻服务：由 supervisord 托管，崩溃自动重启
    command: python3 scripts/listener.py
    restart_schedule: {kind: cron, expression: "0 4 1 * *"}   # 可选：定期重启（例如每月一次）
```

- 脚本在项目目录下运行，环境变量只有 PATH、HOME、TZ、PROJECT_ID、JOB_ID、PLANNED_AT、RUN_ID、MANUAL_RUN 等，
  **拿不到 Kernel 的数据库和密钥**；需要的配置放在项目文件里。退出码非 0 即失败。输出写入 `runs/jobs/<id>/` 下的日志。
- 错过的时间点只补跑最近一次；上一次还没结束时，本次跳过。脚本要能安全地重复运行（幂等）。
- 删除某项即退役；修改时间规则后从下一个时间点开始生效。
- Kernel 只登记**发布过的** jobs.yaml：在 shell 里直接修改 vault 中的 jobs.yaml 不会生效（Kernel 会在 Supervisor 频道提示并忽略）。
- 代码交付的项目：jobs.yaml 放在**仓库根目录**，和代码一起提交，随主分支快进发布生效；script 的 entry 和服务的命令
  相对仓库根目录，在仓库的工作副本里运行；agent 作业的 `agents/<名称>` 仍在项目 vault 里。
