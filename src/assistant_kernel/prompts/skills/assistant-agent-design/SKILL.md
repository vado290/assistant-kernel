---
name: assistant-agent-design
description: 设计产物内部的 agent（由 jobs.yaml 的 agent 作业运行）：目录结构、config.yaml、role.md 和专属技能怎么写；修改本项目 PM/Supervisor 的角色要点时也参考。
metadata:
  version: "2.0.1"
  load: {roles: [pm], stages: [design, build], on_demand: true}
  changelog:
    - "2.0.1 2026-10-16 运行声明文件改为项目根目录的 jobs.yaml（vault 的 runtime/ 目录存放 Kernel 内部数据）"
    - "2.0.0 2026-10-16 按核心 v6 和通用运行时（ROUND3 S3）重写：产物内 agent 由 agent 作业定时运行，只读，结果发到频道"
---

# 产物内 agent 的设计

## agent 是什么
一份执行配置，不是常驻进程：定时到了，Kernel 用它的角色、技能和作业的 prompt 运行一轮模型（只读，不能改项目文件），
最后一条消息就是结果，按作业的 `post_result` 发到频道。需要写文件的步骤交给脚本作业。

## 目录与文件
```
agents/<名称>/config.yaml     # 模型设置
agents/<名称>/role.md         # 角色：身份、职责、边界、完成标准（几段即可）
.agents/skills/<技能>/SKILL.md # 专属技能：metadata.load.roles 写 [<名称>]，Kernel 才会给它加载
```

config.yaml 最小写法：
```yaml
schema_version: agent-config.v2
agent_id: analyst
kind: worker
status: active
role: role.md
harness:
  provider: openai
  reasoning_effort: medium
  timeout_seconds: 900
tools: [get_authorized_workspace, list_authorized_tree, read_authorized_file, vault_read_text]
```
只列它需要的只读工具。

## 角色与技能的分工
- **role.md** 写"我是谁、为谁服务、负责什么、不负责什么、结果怎样算好"。
- **技能**写"怎么做"：读哪些数据、分析步骤、判断标准、输出格式（例如"三条发现，每条附数据和建议"）。
- 作业的 **prompt** 只写这一次要做的事（例如"读取本周日报数据"），不重复角色和技能。

## 检查清单
- [ ] 用户最终在频道看到的结果格式在技能里写清楚了，长度合适
- [ ] 数据不足或读取失败时，agent 会如实说明而不是编造
- [ ] 没有需要写文件的步骤（有的话改成脚本作业）
- [ ] 技能的 load.roles 包含这个 agent 的名称
