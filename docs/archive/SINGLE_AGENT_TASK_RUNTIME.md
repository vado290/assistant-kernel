# Single-Agent Task Runtime v2

## Contract

One Project owns at most one open Task. The PM remains the only business Task
owner and may delegate bounded work to native subagents or durable Kernel child
Runs. The protocol is enabled by `task_protocol: single-agent.v2`.

PostgreSQL owns Run/Session state and the accepted document references. Immutable
Task revisions live below `runs/<task-id>/documents/`. A `current.json` file is
only a rebuildable pointer to an accepted revision; it is never a writable
business authority.

## Documents

The effective Task inputs are selected only through the Context Manifest:

- `task-contract.v1` — goal, scope, requirements, acceptance criteria and constraints;
- `turn-record.v1` — one PM Turn delta and evidence references;
- `supervisor-action-plan.v1` — the stateless Supervisor's proposed next action;
- `task-state-snapshot.v1` — a Kernel-generated projection of database state;
- delegation request/result documents and delivery receipts as applicable.

User approval binds an immutable Task Contract revision and SHA in PostgreSQL.
Approval is not written back into the contract, so no SHA cycle exists.

## Turn control

- `continue` waits for the per-Turn Supervisor barrier when Supervisor is enabled;
- `needs_input` and `needs_approval` reach the user immediately;
- `return_to_requirements` replaces the accepted Task Contract with a draft revision;
- `ready_for_validation` requires an approved Task Contract;
- terminal outcomes keep documents, traces and candidate evidence for recovery.

The Supervisor reconstructs state every Turn, writes one immutable Action Plan,
and selects only a published Work Mode (`requirements`, `build`, `audit`,
`operate`, or `repair`). Safe plans are applied automatically. High-impact plans
wait for an explicit user decision. A failed Supervisor attempt degrades safely
without widening permissions, publishing, or creating a Project.

## Delegation

Native subagents use a Delegation Request/Result contract, one level deep and at
most three in parallel. Their transcript stays in trace storage; the PM receives
only the structured Result and references. Kernel child Runs use
`interaction_kind: child_task`, a parent-only finalizer, their own Execution Plan
and Session, and never create a user Outbox message.

## Continuity and release

Physical Session rotation reads accepted document references rather than copying
the previous transcript. A `session-handoff.v3` revision and Conversation Digest
provide navigation, while original Messages and receipts remain authoritative.

Source changes stay candidate-only until deterministic Validation and optional
cold Audit pass. Publisher verifies the sealed bundle, formal Scope and original
hashes, applies atomically, and records publication receipts. Uncertain effects
enter reconciliation instead of being guessed.

Historical v1 files are migration inputs only. New Runs neither read nor create
Task Pack, writable Requirements Current, writable Task State Current, or adopted
Supervisor Prompt state.
