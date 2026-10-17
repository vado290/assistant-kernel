# Document-driven Task Runtime v1

Baseline target: Assistant Kernel `0.24.6`, schema `core5.v6`.

## Authority by domain

- User intent and approval: original Message plus Decision Ledger.
- Project semantics: `project.yaml`.
- Task contract: accepted immutable `task-contract.v1` revision.
- Runtime state: PostgreSQL Run and Session rows.
- Effective semantic documents: parent Run `request_json.accepted_documents` and monotonic `state_revision`.
- Implementation: formal source or sealed Candidate SHA.
- Validation and external effects: receipts.
- Supervisor, Digest and Map outputs are advisory or derived.

## Minimal Task documents

- `task-contract.v1`: goal, scope, requirements, acceptance and constraints.
- `turn-record.v1`: one settled PM Turn and its evidence references.
- `task-state-snapshot.v1`: rebuildable projection from PostgreSQL.
- `supervisor-action-plan.v1`: one stateless next-action decision.
- `delegation-request.v1` / `child-result.v1`: bounded delegated work.
- `context-manifest.v3`: the sole document-loading manifest.

Every document uses `resource-ref.v1`. Revisions are immutable; `current.json`
contains only an accepted reference and is rebuildable. A revision is effective
only after the parent Run transaction accepts it and increments `state_revision`.

## Turn protocol

```text
PM Turn -> immutable Turn Record -> waiting_supervisor
-> Action Plan transaction
-> queued | waiting_user | waiting_children
```

Questions and approval cards are delivered immediately and are not delayed by
the advisory barrier. A failed Supervisor evaluation resumes the current mode
with `degraded_supervision=true`; it cannot apply a partial plan or expand
authority.

## Delegation

Native Codex subagents inherit the PM environment and capabilities. At most
three run concurrently, without descendants. Declared disjoint write sets may
run in parallel; the parent Turn records the final diff and unexplained effects
enter reconciliation.

Durable work uses `interaction_kind=child_task`, `parent_run_id`, an independent
Execution Plan and parent-only finalization. Child Runs never create user Outbox
messages.

## Maps

Runtime navigation lives in `vault://system/knowledge/runtime-map/` and Project
Semantic Map generations. System-map audit snapshots use a separate cache and
never become runtime authority.

Hooks and business Slash commands are outside this version.
