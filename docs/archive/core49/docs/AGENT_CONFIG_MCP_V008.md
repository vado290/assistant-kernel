# Agent Config and per-invocation MCP v008

Migration `0008_agent_config_mcp.sql` is the first implemented slice of the
approved Agent configuration and MCP architecture.

## Implemented

- Versioned `agent_scope_versions` and `agent_config_versions`, with active
  pointers on `agents`.
- One readable `agent-config.yaml` plus immutable `config-versions/v0001` for
  every active Butler, PM, and Stage Agent.
- Reviewed Agent config publication through
  `POST /v2/agents/{agent_id}/config-versions`: validates YAML and protected
  defaults, pins the current Role/Scope, writes an immutable version and
  Markdown diff, then switches the active pointer and projection.
- Reviewed Scope publication through
  `POST /v2/agents/{agent_id}/scope-versions`; it creates a new Scope version
  and matching Agent Config version so the effective binding remains explicit.
- A common Pydantic/YAML schema for Role reference, Scope mode, Harness defaults,
  required/allowed/denied Skills and Tools, safe default Tool parameters, and
  update policy.
- Project Manager and Butler intake Skills, installed into their own Project or
  system Skill directory when the Agent config is initialized.
- A modular `kernel_agent` stdio MCP entrypoint with workspace, history,
  project-list, capability-search, and existing stage-interaction modules.
- Per-invocation Codex CLI injection. Tool configuration is not copied into each
  `CODEX_HOME`.
- Project-bound `workspace_list`, `workspace_search`, `workspace_read_text`, and
  hash-fenced `workspace_write_text`.
- Project history queries with a configurable `lookback_days`; the default is
  365 days and the accepted range is 1 through 3650 days. Results are paginated.
- MCP Tool definitions, immutable invocation bindings, and per-call audit rows.
- A second database check binds every Agent MCP call to the current leased Job,
  worker, Agent, Project, and Stage node. Tool arguments cannot select a Project.
- Agent MCP also compares its complete Tool list, defaults, and Scope hash with
  the immutable `invocation_tool_bindings`; a forged environment context cannot
  add a write Tool or expand Scope.
- Read-only API routes for current Agent config/scope and Project conversation,
  history, and message queries.

## Default Agent behavior

- Butler: its own system history plus Project registry listing. It does not gain
  Project business-file access from `list_projects`.
- PM: its Project files, capabilities, and Project message history; no write Tool.
- Read-only Stages: the same Project read/history tools plus durable question and
  confirmation tools.
- Implementation: the same Project tools plus `workspace_write_text`, with
  expected-SHA fencing on overwrite.

The Agent MCP server receives a non-secret invocation context. It loads database
configuration internally and does not put a PostgreSQL password or long-lived
Kernel management token in the Codex MCP command line or Tool arguments.

## Verification

> Historical deployment note: the 33-profile count below was the initial v008
> bootstrap and included smoke/test Projects plus four profiles later judged
> unnecessary. It is not the current active layout. See
> `SIMPLIFIED_AGENT_PROFILES.md` for the 9-profile production state.

- Static/unit suite: `27 passed, 33 skipped`.
- Isolated PostgreSQL full suite: `61 passed in 111.66s`.
- Both new Project Manager and Butler Skills pass the Skill Creator validator
  with UTF-8 mode enabled.
- Production migration `0008` applied successfully.
- 33 active Agent configs and 33 active Scope versions initialized, then upgraded
  to v0002 with active config/Role/Skill paths protected from direct MCP writes.
- 12 active MCP Tool definitions initialized; the final code-bound definitions
  are version 4 after deployment security refinements.
- API/MCP/Worker restarted and `/health/ready` returned `ready`.
- Live read-only verifier returned
  `AGENT_CONFIG_MCP_LIVE_OK; history_conversations=5; lookback_days=365`.
- The 14 pre-existing pending Outbox records were preserved.

Backup before deployment:

- `H:\AI\assistant-data\backups\agent-config-mcp-20260909-082402`
- PostgreSQL dump SHA-256:
  `90120046ECD2EAFAF51FF3F2DA506DBA71B898BDB333244F6BC97A7E3450B012`

## Deliberately deferred

- PM `request_stage1_confirmation`. Existing v007 Interaction responses assume a
  Task/WorkflowRun/NodeRun; a conversation-level transition needs a compatible
  generic Interaction migration rather than a special-case shortcut.
- `workspace_apply_patch`, delete/rename, and arbitrary Shell. Full-file writes
  with create/expected-SHA semantics are implemented first.
- Exact Task/Message filters and semantic history retrieval. The v1 contract uses
  existing `project_id_snapshot`, timestamps, text, and cursor pagination.
- Trae Adapter and Trae MCP materialization. Trae is not installed on this host;
  no production compatibility claim is made.
- Immutable executable Tool release directories. Tool definition code hashes and
  versions are recorded; retaining runnable old releases needs a separate loader
  and cleanup policy.
- Optimizer retirement, JobAttempt tables, full `handoff.v1`, and physical
  directory migration. These are separate cutovers and were not required to make
  Agent config/history/MCP useful now.

## Key files

- `migrations/0008_agent_config_mcp.sql`
- `src/assistant_kernel/services/agent_config.py`
- `src/assistant_kernel/repositories/agent_config.py`
- `src/assistant_kernel/repositories/history.py`
- `src/assistant_kernel/mcp_server/agent_server.py`
- `src/assistant_kernel/mcp_tools/`
- `tests/test_agent_config_mcp.py`
- `scripts/initialize_agent_configs.py`
- `scripts/verify_agent_config_mcp_live.py`
