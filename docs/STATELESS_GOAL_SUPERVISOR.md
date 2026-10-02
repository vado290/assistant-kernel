# Stateless Goal Supervisor · Action Plan v1

The Supervisor has no resumable model Session. Every evaluation reconstructs the
current Task from PostgreSQL, accepted immutable documents, Messages, receipts
and bounded evidence references.

After each settled PM Turn, the Kernel queues one idempotent Supervisor
evaluation. User questions and approval requests are delivered immediately and do
not wait for that evaluation.

The Supervisor writes `supervisor-action-plan.v1` with:

- evaluated Turn and context epoch;
- decision, reasons, evidence and missing evidence;
- target Work Mode and Session action;
- context requests and bounded Delegation Requests;
- confirmation class and PM instruction.

The Supervisor can select only a Project-published Work Mode. The Kernel resolves
that mode's Skills, Context Profile, Permission Profile and Validation Policy.
The Supervisor does not grant permissions, approve Requirements, publish files,
create Projects, or authorize trading.

## Commit order

```text
write immutable Action Plan
→ transactionally verify Task/Turn/epoch and accept its reference
→ move the Task to queued, waiting_children, or waiting_user
→ update rebuildable projections and send the status card
```

Safe plans are marked `auto_applied` and have no adoption button. A
`user_required` plan remains blocked until the user adopts, requests revision, or
cancels it. Card actions record a Decision Ledger message; they do not create a
separate adopted-Prompt authority.

Stale Action Plans and cards remain historical evidence but cannot affect the
Task. After two Supervisor failures, the Task records degraded supervision and
continues only in its existing Work Mode; it cannot widen permission, publish,
or create a new Project. The next eligible Turn evaluates Supervisor again.

UI `current` files are optional compare-and-swap projections. Runtime authority
is the parent Task Run's `accepted_documents`, `state_revision`, lifecycle fields,
and immutable document SHA values.
