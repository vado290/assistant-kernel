# Assistant Kernel 0.24.6

Assistant Kernel is the durable control plane for a document-driven personal
assistant. It uses short-lived `codex exec --json` turns; PostgreSQL and immutable
Project documents preserve continuity, authority and recovery.

## Runtime model

```text
Discord / Feishu / local API
→ Message + parent Task Run
→ Context Manifest + Execution Plan
→ PM Turn and optional bounded subagents
→ immutable Turn Record
→ stateless Supervisor Action Plan
→ Validation / Audit / Publish / Operate / Repair
→ receipt-backed reply and recovery
```

PostgreSQL `core5.v6` contains six tables: `kernel_meta`, `messages`,
`model_invocations`, `project_grants`, `runs`, and `sessions`. It owns Run,
Session, lease, idempotency, approval linkage and the parent Task's
`accepted_documents`/`state_revision`.

Project Vaults own immutable semantic document revisions, source/candidate
artifacts, traces, receipts and derived maps. A `current.json` file is only a
rebuildable pointer and never a second writable authority.

## Project agents

Every active Project uses `task_protocol: single-agent.v2` and exactly four active
Agent configurations:

- PM — the only business Task owner;
- Supervisor — stateless per-Turn evaluation and Action Plan;
- Child — one bounded Kernel child Run, parent-only result;
- Operator — incident observation and repair proposal.

The PM may also use Codex native subagents, one level deep and at most three in
parallel. Their raw transcript remains in trace storage; the PM receives a
structured Delegation Result and evidence references.

Supervisor selects only a Project-published Work Mode Bundle:
`requirements`, `build`, `audit`, `operate`, or `repair`. The bundle fixes its
profile, Skills, Context Profile, Permission Profile and Validation Policy.

## Documents and context

The v2 runtime uses one `resource-ref.v1` shape and immutable revisions:

- `task-contract.v1`;
- `turn-record.v1`;
- `supervisor-action-plan.v1`;
- `task-state-snapshot.v1`;
- Delegation Request/Result;
- Candidate, Validation, Audit and Publication receipts.

`context-manifest.v3` is the only prompt-loading list. Project Semantic Maps and
Conversation Digests provide navigation but cannot override original Messages,
accepted documents, source, validation or external receipts.

Requirements approval binds an immutable Task Contract revision and SHA in the
database. Approval is not written back into the contract. If Build needs a
contract change, the PM returns `return_to_requirements`; Kernel clears approval,
accepts a new draft revision and rotates the physical Session.

## Supervisor barrier

Each settled PM Turn is evaluated. Safe Action Plans are transactionally accepted
and automatically continue the PM or start bounded children. High-impact plans
wait for the user to confirm, revise or cancel the Action Plan. Cards do not create
an independent adopted-Prompt state.

After two Supervisor failures, Kernel records degraded supervision and continues
only in the current Work Mode. It cannot widen permissions, publish, trade or
create a Project through this fallback.

## Candidate, validation and publication

Formal source changes are candidate-only. Execution Plans pin workspace Scope,
Agent/Role/Skill snapshots, MCP bindings, model configuration, Work Mode Bundle,
Context Manifest and validation policy.

Validation checks the sealed candidate, hashes, syntax and configured Project
tests. Optional cold Audit uses a fresh Session and read-only Work Mode. Publisher
rechecks the baseline and applies atomically with backup, diff and receipt.
Uncertain effects move to reconciliation; they are never guessed or blindly
replayed.

## Maps

- Runtime Global Catalog: `assistant-data/butler/knowledge/runtime-map/`;
- Project Semantic Map: `<vault>/knowledge/map/`;
- whole-system audit snapshots: maintained outside runtime by
  `assistant-kernel-system-map`.

Each map uses an immutable generation and manifest. `latest.json` moves only after
hash and completeness checks. A stale map falls back to authoritative source and
documents.

## Operations

Active version pointer:

```text
H:\AI\assistant-data\runtime\kernel-current.txt
```

Production processes are managed by `deploy/supervisord.conf`. Deployments create
a versioned venv, run dependency/import checks, switch the pointer, restart Kernel
processes, and roll back on readiness failure.

Verification:

```powershell
docker exec AI /usr/bin/env -u PYTHONPATH `
  /home/v/AI/assistant-data/runtime/kernel-venvs/<version>/bin/python `
  /home/v/AI/assistant-kernel/scripts/verify_core4.py
```

The script name is historical; successful output is `CORE5_VERIFY_OK`.

Key endpoints:

- `GET /health/live`
- `GET /health/ready`
- `GET /version`
- `POST /v2/messages`
- `GET /v2/runs/{run_id}`
- `POST /v2/runs/{run_id}/cancel`
- `POST /v2/runs/{run_id}/reconcile`

Real Discord/Feishu interaction E2E and external-system effects must be reported
separately from unit/integration tests and health checks.

## Discord reliability boundary

Discord interactions are acknowledged before database work. Fresh Slash commands
whose acknowledgment expired or became network-uncertain are still ingested once
through the normal idempotent path; stale interactions are not executed. Expired
buttons, approvals, reconciliation actions and credential submissions never apply
their business effect and instead queue a retry notice.

Ordinary message ingress retries only transient infrastructure failures with the
same Discord message ID. Supervisor output accepts the exact alias
`continue -> continue_current_stage`, attempts one contract-only correction for
other malformed output, then degrades without dead-lettering the parent Task.

`/health/ready` retains its existing gate and additionally reports non-gating
Discord latency, event-loop lag, Run Worker freshness, execution backlog and
recent delivery failures. Events never delivered by Discord during a Gateway
outage cannot be reconstructed by Kernel and require user resend.
