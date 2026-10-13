---
name: assistant-implementation
description: 在批准需求和设计范围内实现candidate、项目能力与运行配置，自测并产生可审查的实施报告；修复也使用该阶段。
metadata:
  version: "1.0.0"
---

# Implementation

先核对有效需求/设计引用、批准、当前任务范围、运营基线和 candidate。执行已批准范围内
的具体代码、Role/Skill/MCP、业务图定义；不得仅靠 Role 文字授予能力。
独立模块可以委派临时原生子 Agent；重叠写集串行，结果由 PM 汇总并核对实际 diff。

- 保留无关文件和用户修改；正式文件使用 candidate 与 Publisher。
- 先完成实际实现，执行与行为变化相称的测试，保存命令、结果与证据引用；不能把自测
  称为 Kernel Validation 或 Review，通过与失败都如实报告。
- 更新文件目的和 Map；记录增加、删除、修改的理由、日期、需求来源与 diff 引用。
- 业务定时由 Kernel Schedule 统一登记和触发；有限执行和服务启用仍核真实批准。
- 外部动作依当前具体授权执行并保存回执；不重放结果未知的动作。
- 受管 Python 通过 `ASSISTANT_INPUT_PATH`、`ASSISTANT_RESULT_PATH` 和
  `ASSISTANT_OUTPUT_DIR` 读写本次执行资料，不硬编码 Run 根目录或沿用旧结果。
  独立输出保持独立运行；确有共享目标时声明共享写，不能为并行而关闭必要保护。
- Python 进程池使用明确上限和正常关闭/等待；启动方式需要时设置 main guard。
  不主动脱离受管进程组。采集程序收到停止信号后按批准约定收口并保存进度；
  进程退出、业务完成与外部效果分别核验，不把停止解释为效果已撤销。

完成时形成 `implementation-report.v1`：有效需求/设计引用、实际改动、测试及结果、
未完成/残余问题、并行委派结果、发布激活准备和回退安排，按 Kernel 合同提交待核验结果。
需求实质改变回 requirements；仅设计需要调整时提交设计修订与批准请求，保留已完成工作。
不要为了满足测试而默默修改需求或验收。Review 缺陷在同一批准范围内修复并重测受影响项。
