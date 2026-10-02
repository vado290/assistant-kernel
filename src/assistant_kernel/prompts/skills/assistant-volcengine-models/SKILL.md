---
name: assistant-volcengine-models
description: 使用Kernel统一提供的火山方舟文本生成、图片OCR、Embedding与视频能力；模型凭据由共享Worker配置持有，不进入提示词。
metadata:
  version: "1.1.0"
---

# 火山模型公共能力

Kernel为所有Project和Agent提供共享的火山模型能力。凭据文件属于Kernel控制面，
不进入Project Scope、Context View、提示词或Agent Shell环境。模型调用必须通过
Kernel Tool或确定性Worker进行，由Worker读取凭据并记录实际模型与调用结果。

## 当前固定模型

- 普通生成与结构化文本：`doubao-seed-2-0-mini-260428`
- 图片页OCR：`doubao-seed-2-0-mini-260428`
- 文本向量：`doubao-embedding-vision-251215`，固定1024维
- 视频生成预留：`doubao-seedance-2-0-fast-260128`
- ASR预留：`Seed-ASR 2.0`，LAS资源`seedasr`
- Rerank预留：`doubao-seed-rerank`

## Agent可用工具

1. 先调用`volcengine_model_status`确认能力是否启用；返回值只包含布尔状态和模型名，
   不包含凭据。
2. 普通低成本生成、抽取、分类或格式转换使用`volcengine_generate`。
3. 已经取得HTTPS图片地址或`data:image/...`内容时，使用`volcengine_ocr`提取原文；
   不得要求模型猜测图片中不存在的内容。
4. Embedding、批量文档解析、ASR、Rerank和视频生成由Kernel确定性Worker/API负责，
   Agent不得通过Shell自行拼接密钥或直接调用供应商接口。

## 边界

- 火山模型是辅助能力，不替代当前Run的Harness、Session、审批、Scope或业务状态。
- 工具失败时报告实际错误；认证失败不得寻找、读取或要求暴露控制面凭据文件，只报告
  对应能力未配置或被拒绝，并交由Kernel运维处理。
- 不在回复、Artifact、日志、命令行、环境诊断或异常信息中输出任何Key、AK、SK或Token。
- 不对同一次外部调用盲目重试；涉及视频等计费副作用时必须先核对任务ID和回执。
