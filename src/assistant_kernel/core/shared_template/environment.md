---
name: environment
description: "Technical environment: development machine, containers, database, model services, channels, remote hosts and deployment targets"
metadata:
  version: "1"
  load: {roles: [pm, supervisor], stages: [design, build, review, operate], purposes: [consult]}
---

# Shared background · Technical environment

## 1 Development machine
(to be written: operating system, shell, where the project files live)

## 2 Containers and paths
- The Docker container `AI` runs the Kernel services; the container path `/home/v/AI` is the host's workspace folder.
- supervisord in the container runs kernel-api (health checks and the dashboard), kernel-core-dispatcher (runs),
  kernel-core-gateway (Discord gateway) and optionally docling-serve (document parsing).
- The data folder `assistant-data`: `vaults/<project>` (each project's files), `runtime` (environments, wheels, venvs),
  `config`, `backups`.

## 3 Database
- PostgreSQL holds Kernel's state in the schema `core`.

## 4 Model and tool services
- Agents call the models through the Codex CLI; Kernel keeps the login in step — do not handle it yourself.
- Further model services (text generation, OCR, embeddings) are called through Kernel tools; their credentials never
  enter prompts.

## 5 Channels
- Discord: each project has the channels #pm and #supervisor; cards appear in #supervisor.

## 6 Remote hosts
(to be written: each host's alias, purpose and operating system; no login details beyond the address)

## 7 Deployment targets
- Kernel itself: the user runs `assistant-kernel/scripts/deploy.ps1` (tests in the test environment first, then installs
  into prod and restarts).
- Where and how other projects are deployed once built: (to be written)

## 8 Change log
- first draft (R1)
