---
name: assistant-implementation
description: 在批准范围制作交付物、自测并交Review。
---

## 任务目标
输入固定包、批准需求/设计、基线和candidate，制作代码、方法包、配置、数据或文档。

## 步骤
划分独立和重叠写集，保留无关改动，正式目标走candidate。测试与变化相称，自测不等Kernel Validation。

Python同时形成manifest：入口及模块、环境依赖、输入输出、资源、效果及子进程责任。简单串行，I/O按需有界，CPU池默认2、数值线程1。存在定时需求时，同时交付固定 ScheduleDefinition、时间预览、自测及登记/启用所需引用。

## 输出
implementation-report.v1、实际差异、自测、子结果、未完成及发布/注册/启用请求。文件变化附用途和原因引用。

## 失败与验收
缺依赖先提适配，旧EnvRef不漂移。范围变化回分析；未知效果先对账。必需产物、固定候选及证据齐备才交Review，exit0不能证明业务成功。

## v1.2执行规则（承接v1.1）
制作.py、必要模块、固定Markdown Role/Skills、JSON运行清单/模型MCP配置；分finite/service执行入口。自测语法、导入、关键路径、实际工具加载、停止/取消/恢复；真实启用须批准。生成文件不等注册启用或验活。

## v1.2运行与配置联动
按固定设计生成.py/资产/清单，实现停止/join、stderr、实际装配与checkpoint；固定attempt目录和配置Ref。不得以模板生成声称已经启用；定时由 Kernel scheduler 管理；Build 制作候选，Operate 正式登记启用。

## 定时任务联动
涉及指定时间、周期业务或服务维护时读取 assistant-schedule-management。按本阶段能力使用候选写入、预览、查询或 Operate 管理工具；登记、启用、到点提交和业务成功不得混称。
