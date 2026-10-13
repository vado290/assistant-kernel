---
name: assistant-capability-maintenance
description: 对Role、Skill、Artifact和配置形成最小、可验证改进。
---

## 任务目标
读目标版本、实际绑定、反馈与基线，区分create、iterate和repair；repair不是第六阶段。

## 步骤
局部在本Project维护，公共在kernel Project。区分规则、输入、模型、服务、权限、代码与环境原因，固定案例，比较保持现状与改法。

形成improvement-proposal.v1，列问题、证据、原因假设、替代、收益、影响、验证、采用和恢复。公共方法集中，不把每个小步骤拆成Skill。

## 输出与交接
适用批准后走需求、设计、Build、Review和Publisher。公共升级不覆盖旧实例；既有Task采用范围明确，下一包重新绑定确认。

## 失败与验收
效果按实际包和条件分组，缺归因依据记未知。恢复覆盖消费者、数据及对象，不只复制旧文件。无收益不自动反复改；长期观察不占原Task。

## v1.2执行规则（承接v1.1）
迭代对象包含业务Agent/图/启动配置与框架Role/Skill/Artifact。允许简练新增DB表，但不增逐操作权限链；升级须核旧实例/消费者/历史读取与运行恢复。

## v1.2运行与配置联动
schema内运行参数按配置管线，代码/Role/Skill/契约/业务语义按原维护交付；采用对象、效果比较和回退兼容完整保留。
