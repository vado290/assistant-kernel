# Assistant Kernel

A document-driven personal assistant: you talk to a project's **PM** and **Supervisor** in Discord; the
Kernel runs short `codex exec` turns and keeps all state in PostgreSQL.

Since 2026-10-15 production runs the **rebuilt core** (`src/assistant_kernel/core/`). The full design,
decisions and progress are in [docs/REBUILD_PLAN.md](docs/REBUILD_PLAN.md). Older documents in `docs/`
describe the retired system and are kept for history.

## How a task flows

```text
#pm message ─► task (queued → active)
   Supervisor evaluates ─► one card item in #supervisor (questions / next step / decision)
   you answer or click ─► PM turn in the current stage ─► Supervisor evaluates again ...
stages: requirements → design → build → (Kernel validation) → review → release → completed
```

- Each project has one active task, and an active task always waits on exactly one thing: one run or one
  card item. Each project has at most one open card.
- Requirements and design approvals are given together with "execute"; every publish needs your approval.
  Approvals stay valid only while the approved documents are unchanged.
- Messages in `#pm` are added to the current task; messages in `#supervisor` are consultations and never
  become PM input. Text commands: `/status`, `/pending`, `/cancel 确认`, `/evolve` (start a self-evolution review now).

## Code map

| Path | Role |
|---|---|
| `core/sql/` | `core` schema migrations (applied by `assistant-kernel-migrate`) |
| `core/rules.py` | stages, allowed moves, gates, derived approvals (pure functions) |
| `core/machine.py` | task state machine — the only code that changes task state |
| `core/cards.py` | the one open card per project |
| `core/kernel.py` | entry points: messages, card actions, claim/complete/fail runs |
| `core/dispatch.py` | dispatcher: runs claimed runs concurrently, leases, recovery |
| `core/executors/` | PM, Supervisor, validation and publish executors |
| `core/gateway/` | Discord gateway (cards, buttons, modals, outbox, reminders) |
| `prompts/core/` | PM and Supervisor roles and output formats |

Project agent files (roles, skills, models) live in each project's vault: `assistant-data/vaults/<project>/agents/`.
Channel routing is `assistant-data/config/core-discord.yaml`.

## Operations

```powershell
.\scripts\deploy.ps1            # build wheel → test venv + tests → prod venv → migrate → restart → check
.\scripts\deploy.ps1 -TestOnly  # test venv only
```

Production processes (`deploy/supervisord.conf`): `kernel-core-dispatcher`, `kernel-core-gateway`,
`kernel-api` (`/health/ready`, `/version`), `kernel-mcp`, `docling-serve`. Only two venvs exist:
`kernel-venvs/test` and `kernel-venvs/prod`.

Tests: `tests/core/` (each test gets a temporary database). `scripts/core_smoke.py` runs one real-model task
on a temporary database.
