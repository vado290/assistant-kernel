---
name: environment
description: 技术环境：开发机、容器、数据库、模型服务、渠道、远程主机和部署去向
metadata:
  version: "1"
  load: {roles: [pm, supervisor], stages: [design, build, review, operate], purposes: [consult]}
---

# 公共背景 · 技术环境

## 1 开发机
- Windows 10 宿主机，命令行为 PowerShell 5.1（也有 Git Bash）；所有项目文件在 `H:\AI` 下。（核实 2026-10-16）
- PowerShell 5.1 调用 `docker exec` 时会去掉参数里的双引号：多参数的命令写成脚本文件复制进容器执行。（核实 2026-10-16）

## 2 容器与路径
- Docker 容器 `AI` 运行 Kernel 服务，容器内路径 `/home/v/AI` 对应宿主机 `H:\AI`。（核实 2026-10-16）
- 容器内由 supervisord 管理：kernel-api（健康检查）、kernel-core-dispatcher（执行调度）、kernel-core-gateway（Discord 网关）、
  docling-serve（文档解析）。（核实 2026-10-16）
- 数据目录 `H:\AI\assistant-data`：`vaults/<项目>`（各项目文件）、`runtime`（运行环境、wheel、venv）、`config`、`backups`。（核实 2026-10-16）

## 3 数据库
- PostgreSQL 在容器 `rag-postgres`，数据库 `rag`；Kernel 的状态在 schema `core`，旧版数据归档在 `archive_core5`。（核实 2026-10-16）

## 4 模型与工具服务
- agent 通过 Codex CLI 调用 OpenAI 模型；登录信息由 Kernel 统一同步，不要自行处理。（核实 2026-10-16）
- 火山方舟提供文本生成、图片 OCR、Embedding，通过 Kernel 工具调用，凭据不进入提示词。（核实 2026-10-16）

## 5 沟通渠道
- Discord：每个项目有 #pm 和 #supervisor 两个频道；卡片都显示在 #supervisor。（核实 2026-10-16）

## 6 远程主机
（待补充：各主机的别名、用途、操作系统；不要写地址以外的登录信息）

## 7 部署去向
- Kernel 自身：用户在宿主机执行 `assistant-kernel\scripts\deploy.ps1`（先在 test 环境跑测试，再装入 prod 并重启）。（核实 2026-10-16）
- 其他项目开发完成后部署到哪里、怎么部署：（待补充）

## 8 变更记录
- 2026-10-16 初稿（R1）
