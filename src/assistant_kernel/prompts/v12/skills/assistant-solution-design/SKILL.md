---
name: assistant-solution-design
description: 将有效需求和真实环境转成最小可行、可验证方案。
---

## 任务目标
读取批准需求、相关版本、环境和能力，区分已实现、待新增和待验证。

## 步骤
建立需求到改动映射，比较实际影响结果的选项，优先复用。说明产物、范围、写集、并行、测试、发布、运行及恢复。

Python方案固定包、环境、输入输出、资源和清理。定时需求固定为 Kernel ScheduleDefinition，调用 preview_schedule 校验时间，明确 Build 候选与 Operate 登记启用。监测方案明确项目指标、窗口、阈值和渠道，不套统一默认。

## 输出
solution-design.v1及具体批准请求。低风险动作明确纳入设计；高影响动作在最终差异和证据展示后另批。

## 失败与验收
需求实质变化回需求，设计变化重批受影响内容。缺环境提出适配，不静默升级。方案能对应需求与证据才交实施。

## v1.2执行规则（承接v1.1）
调用assistant-business-runtime-design及适用workflow/service专项，明确.py入口、Agent配置真实加载、图/业务状态、依赖、资源、触发与启停。常驻复用ProcessManagerAdapter；定时使用 Kernel scheduler；调用 assistant-schedule-management 明确规则、登记、启用与验证义务。

## v1.2运行与配置联动
分别固定运行形态、手动启动和手动维护边界；核工作项/等待/幂等/预算、全局配置绑定与必要停止探针。旧工作项未处理完不升级图版本。
