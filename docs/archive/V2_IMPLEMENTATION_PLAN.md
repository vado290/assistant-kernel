# Historical Assistant Kernel V2 implementation plan

Archived baseline; the active ProjectBuild implementation is described in ../PYTHON_STAGES.md.

Status: implemented and verified baseline (2026-09-02)

## 1. Objective and boundary

This stage delivers one recoverable control-plane loop inside the existing `AI`
container. Hermes is not a runtime dependency or a state owner. It is used only
as a design reference.

The Kernel is the business-state source of truth. LangGraph checkpoints own only
the executable graph cursor and node-local state. n8n owns transport, schedules,
notifications, approvals UI, and externally visible side effects.

## 2. State ownership

| State | Authoritative owner | Recovery rule |
| --- | --- | --- |
| Project, conversation, task, approval, workflow publication | Kernel tables | Reconcile from immutable Kernel events |
| Graph cursor and interrupt payload | LangGraph checkpointer | Match by pinned workflow version and graph thread |
| Native model transcript | Harness archive in Project Vault | Kernel stores URI, hash, and normalized events |
| Notification/external-effect intent | Kernel outbox | n8n acknowledges by idempotency key |
| Transport retry and external-effect retry | n8n | Never mutates Kernel business state directly |
| Agent/business retry | Kernel job and stage-run state | New attempt with retained prior evidence |

No filesystem `current.json`, n8n execution, or Hermes Board is authoritative.
They may only mirror Kernel state.

## 3. Required invariants

1. Every inbound message has a stable external id, immutable binding snapshot,
   and monotonically increasing sequence inside its conversation.
2. Only the FIFO head of one conversation may run. Different conversations may
   run independently when concurrency is enabled.
3. Every job is claimed with a lease and fencing worker instance id. Expired
   jobs are retried up to a fixed maximum and then moved to `dead_letter`.
4. A task/workflow transition uses compare-and-set from an allowed prior state.
5. A workflow run pins one immutable `workflow_version_id` for its lifetime.
6. An approval decision is consumed once. Duplicate callbacks return the stored
   decision and never resume a graph twice.
7. One stage uses one logical stage scope. Session rotation creates a new
   physical session under that scope and preserves parent lineage.
8. Rotation does not move the current pointer until checkpoint validation and a
   successful first turn in the new physical session.
9. Role policy, project control facts, run checkpoints, and raw evidence remain
   separate versioned records.
10. Outbox publication is committed in the same database transaction as the
    business change that caused it.

## 4. State machines

Control job:

`queued -> running -> succeeded | failed | cancelled | dead_letter`

`running -> queued` is allowed only after lease expiry and while attempts remain.

Task/workflow run:

`queued -> running -> waiting_user -> running -> succeeded | failed | cancelled`

`running -> needs_rework -> queued` creates a new stage attempt. Terminal states
cannot be reopened; rework after completion creates a new child task/run.

Physical session:

`starting -> active -> rotation_required -> sealed`

Failed replacement sessions are marked `failed`; the previous current pointer
remains usable.

Workflow version:

`draft -> candidate -> active -> retired`

Only one active version exists for `(project_id, workflow_id)`. Activation is a
single transaction after tests, review, and required user acceptance.

## 5. Delivery phases

### P1A - durable control-plane substrate

- V2 schema migration and enumerated transition rules.
- Conversations, messages, immutable binding snapshots, FIFO control jobs.
- Lease, heartbeat, retry, cancellation, dead-letter, and outbox delivery.
- Idempotent intake and callback APIs.

### P1B - conversation and session loop

- System butler and per-project manager agents through a common runtime.
- Versioned role policy and a separate project-context ledger.
- Logical/physical sessions, Codex resume, trace archive, and prompt manifests.
- Validated checkpoint and non-in-place rotation above the soft context limit.

### P2 - project build loop

- Requirements, confirmation, architecture, confirmation, builder, tests,
  reviewer, rework, acceptance, and finalization graph.
- Each stage has a fresh logical stage scope and physical session lineage.
- Approval interrupts release the worker and resume by the same graph thread.

### P3 - workflow registry and routine runs

- Draft/candidate/active workflow versions with SHA-256 and JSON Schemas.
- Atomic activation and frozen run version.
- Parameter validation, durable run job, result artifact, and outbox completion.

### P4 - minimum governance foundation

- Role versions/entries, artifact versions/links, change requests, source and
  claim ledgers, conflict preservation, and model execution profiles.
- The Curator contract is reserved as an explicit queued stage; no implicit
  second model call is made per turn. The Curator worker itself is deferred.
- Multi-Harness interfaces are defined but Codex remains the only production
  agent Harness in this stage.

## 6. Current verification boundary

| Phase | Current evidence | Remaining boundary |
| --- | --- | --- |
| P1A | Concurrent/sequential duplicate intake, FIFO, expired-lease reclaim, stale-worker fencing, retry/dead-letter, and outbox creation pass | Channel-specific outbound delivery requires a selected connector and credential |
| P1B | A live Codex conversation produced an archived trace, outbound message, and durable outbox event; session pointer and rotation ordering pass | A forced live high-context rotation is not part of routine smoke tests |
| P2 | The full graph passes with a deterministic fake Harness; live Codex passed requirements approval, checkpoint resume, architecture approval, and rejection cancellation | Builder/test/reviewer live execution should be run against a user-approved real build task |
| P3 | Published n8n caller, pinned workflow version, Python executor, JSON Schema result validation, result artifact, and completion outbox pass end to end | Additional executor types remain deferred |
| P4 | Role/context/artifact/change/source/claim/conflict/model-profile persistence and core APIs exist | Automated Curator and source-ingestion pipelines remain deferred |

## 7. Recovery acceptance

The stage is accepted only after automated and live checks prove:

- duplicate intake produces one message and one job;
- FIFO claims never overtake an earlier message in the same conversation;
- expired leases are reclaimed and fenced stale workers cannot finish jobs;
- duplicate approval callbacks resume once;
- a restart retains queued/waiting work and LangGraph checkpoints;
- rotation failure keeps the old physical-session pointer;
- a run keeps its workflow version after a newer version is activated;
- outbox delivery is retryable and idempotent;
- Project Vault paths cannot escape their project root;
- database rows and Vault artifacts can be traced by URI and hash.

## 8. Explicit deferrals

This stage does not add Hermes, arbitrary multi-project write concurrency,
automatic workflow generation without review, autonomous irreversible external
actions, a channel-specific outbound adapter without selected credentials, an
automated Curator, or a second workflow-state system. Those extensions must
reuse the same Kernel ids, transitions, leases, and evidence contracts.
