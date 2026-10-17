> 历史版本记录：其中 Butler/intake 的入口描述已失效。当前退役规则见 [KERNEL_MANAGEMENT_MERGE.md](KERNEL_MANAGEMENT_MERGE.md)；本次未部署生产。

# Project Runtime v2 foundation

Date: 2026-09-11

Application: Assistant Kernel 0.9.1

Schema: core4.v5

## Implemented

- Explicit `project.yaml` `layout_version: 2` and `agents/<agent>/config.yaml` layout.
- Dynamic Project Agent Registry with unique IDs, keys, aliases, status, and entrypoint policy.
- Immutable `execution-plan.v1` per Run, plus bound-resource and per-invocation prompt evidence.
- Retry-safe recovery of a Plan written before the Run pointer was persisted.
- Project MCP manifest resolution and content-addressed MCP source snapshots.
- Codex CLI injection for multiple STDIO and Streamable HTTP MCP servers using the
  official `mcp_servers.<id>` command/args/cwd or url configuration fields.
- A controlled Project STDIO launcher; Project-local MCPs do not open a port.
- Non-secret MCP transport/lifecycle summaries in the LLM prompt.
- Protected `assistant-session-rotation-handoff` Skill.
- Session rotation when Role, core Skill, Scope, identity, or output-contract compatibility changes.
- Deterministic Feishu `/agents`, `/agent`, `/pm`, and `/whoami` handling without an LLM Run.
- PM tools for dynamic Agent discovery, current route inspection, and same-Project Agent handoff.
- `validation-report.v2` release gates, deferred long-workflow targets, and bounded direct
  Implementation rework before Review.
- Migration of active `kernel` and `butler-maintenance` Project projections to layout v2.
- Complete isolated regression result: `72 passed`.

## Deliberately deferred

- General Workflow Coordinator and a Workflow DSL.
- Persistent Service registry and supervisor reconciliation.
- Kernel Scheduler and the proposed derived `assistant.schedules` table.
- Automatic behavioral execution of arbitrary candidate test commands.
- A generic `service://` HTTP MCP endpoint registry; the current safe implementation
  supports `env://ENV_NAME` endpoint references.
- Cross-Project `/agent` switching.
- Resource version selection or per-file rollback management.
- IB Project creation and trading Tools.

These deferred items require additional runtime ownership or safety decisions and are not
silently represented as implemented.
