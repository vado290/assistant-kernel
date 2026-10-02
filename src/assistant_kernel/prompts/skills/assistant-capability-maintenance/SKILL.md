---
name: assistant-capability-maintenance
description: Kernel保护的Skill能力包创建、生产反馈驱动迭代与残缺修复协议；根据当前正式基线区分create、iterate和repair。
metadata:
  version: "1.3.0"
---

# Skill 能力包创建与迭代

## 任务目标

在当前Project内创建、迭代或修复一个可发现、可执行、可验证的Skill Bundle，并保持其任务目标、触发边界、配套脚本、引用和测试自洽。正式路径默认只保留当前最新版；Run快照和candidate基线用于证据与冲突保护，不建设历史版本选择或一键回退功能。所有新建或修改的Skill必须包含明确的 `## 任务目标`；涉及Schedule、MCP、Agent或Workflow细节时，由PM为本任务绑定相应专项Skill。

从已接受Task Contract、Action Plan和固定基线读取目标Skill与`change_mode`。不得自行改变模式或绕过candidate、Validation和Publisher。

## create

目标 `SKILL.md` 不存在。需求阶段重点确认使用场景、触发与排除条件、输入输出、所需脚本/MCP/外部依赖、权限边界和首次验收样例。实施阶段创建最小完整 Bundle；candidate 写入必须声明目标预期不存在。发布时目标若已出现，必须停止并重新评估，不能覆盖。

## iterate

目标 Skill 已存在，即使当前没有脚本、或本轮首次增加脚本，也属于迭代。先读取当前 `SKILL.md`、manifest、相关 scripts/references/evals、实际反馈和测试证据；先区分问题来自Skill触发、判断方法、知识、脚本、MCP、权限、模型、输入数据还是外部系统，不能把所有失败归因于Skill。将已确认问题转成可复现案例，固定当前baseline结果，再做最小差异。所有更新或删除必须携带旧SHA-256；Review使用原有案例和新增失败案例比较baseline与candidate，检查目标改善、回归、追问次数、工具调用和未解决风险。

## repair

目标目录残缺、manifest 异常或引用文件丢失。保留可恢复内容，说明缺失和风险；不得当作 create 从空白覆盖。会改变原意时先询问用户。

Skill Bundle 可以包含 `SKILL.md`、`skill-manifest.json`、`scripts/`、`references/`、`templates/` 和 `evals/`。稳定判断与方法写入SKILL.md；大量条件知识放references；确定性重复动作写入scripts；可复现成功、失败和回归案例放evals。每个脚本在Skill中说明相对路径、用途、输入、输出、副作用、调用方式和失败语义。Project工作目录的direct文件Grant不包含受控正式Skill路径；完成candidate后必须提供manifest、最小差异、自测证据和未验证项，正式Skill仍只能由最终批准后的Kernel Publisher更新。
