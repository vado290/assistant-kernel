# Discord-first Project lifecycle

## Product boundary

- `chief-of-staff` is the only cross-Project intake channel.
- One active Project has at most one top-level Discord channel binding.
- A Discord Thread inherits its parent channel's Project, but keeps its own
  Kernel conversation and native Session.
- A Thread cannot bind or unbind a Project independently.
- Discord is the primary Project conversation surface. Feishu `standby` mode
  keeps outbound notifications, approvals, `/pending`, `/resume`, and `/whoami`
  without accepting ordinary Project chat.
- One Project still has at most one running/reconciling Run across its channel
  and Threads.

## Approved Project creation

1. Butler writes an immutable `project-proposal.v2` in the system Vault.
2. The user approves the correlated question.
3. `create_project` idempotently bootstraps the Project Vault.
4. When the source Run is in a configured Discord Guild and automatic
   provisioning is enabled, Kernel lists Guild channels and looks for the
   durable topic marker `assistant-kernel-project:<project-id>`.
5. Kernel reuses that channel or creates a text channel named after the
   Project. A configured Project category is optional.
6. Kernel transactionally records the unique Discord Project binding.
7. Kernel establishes the first PM Run in the new channel.
8. Only after the PM Run exists, Kernel queues one success receipt in
   `chief-of-staff` and one welcome message in the Project channel.

The topic marker closes the create-channel/record-binding crash gap: a retry
finds the already-created channel rather than creating another. If more than
one channel has the marker, provisioning stops for manual repair.

## Existing Project entry

Butler continues to use the existing `handoff_to_project` tool. For a Discord
source Run, Kernel now upgrades that handoff into one channel-aware operation:

1. Load the active Project Card and reject unknown, inactive, or ambiguous IDs.
2. Resolve the Project's active Discord binding.
3. Verify the bound channel through Discord; reuse it when valid.
4. If no binding exists, create and bind a channel. If Discord returns 404 for
   a deleted bound channel, append an unbind tombstone and create a replacement.
5. Create the target PM Run in the resolved channel.
6. Only after the PM Run exists, send a deterministic source-channel receipt
   containing the Discord channel mention.

This keeps natural-language routing and explicit `/bind_project` compatible.
Manual channel creation plus `/bind_project` remains an administrator recovery
path, not a normal prerequisite.

An existing binding is verified against Discord before it is reused. A 404
means the channel was manually deleted: Kernel appends an auditable unbind
tombstone and provisions a replacement. Permission errors and transient API
errors never erase a binding.

## Post-change audit command

`/audit` is implemented by Kernel, not by Discord alone. Discord supplies the
native Slash UI; Kernel persists `session-impact-audit.v2` and starts one
ordinary, idempotent Run in the current Project conversation. `/audit` defaults
to the full read-only protocol, while `/audit quick` limits the requested depth.

The audit object is the latest completed change round in the current Session.
The prompt starts with current-session evidence and expands globally only along
dependencies actually affected by that round, such as callers, consumers,
shared state, routing, concurrency, permissions, recovery, or channel fallback.
It must not become an unrelated whole-system health scan. The audit separates
implemented, deployed, tested, real E2E, and unverified claims and is explicitly
prohibited from performing repairs or invoking write workflows.

## Identity extension point

Discord events persist a snapshot of the author user ID, display name, and
Discord Role IDs under transport metadata. Transport metadata is excluded from
the LLM semantic prompt. A future Kernel-owned identity layer can resolve these
stable external IDs into `Principal`, `ProjectMembership`, `ProjectRole`, and
`ActionGrant` records before adding a versioned speaker context to prompts.

Authorization must never be inferred from a display name or from text supplied
to the LLM. Discord channel visibility is only a UI boundary; Kernel must still
enforce Project and action permissions.

## Recovery and safety

- Project creation, channel discovery, binding, receipts, and PM handoff use
  stable idempotency keys.
- A channel can be renamed because routing uses its immutable Discord ID and
  the topic marker.
- A Project/channel uniqueness conflict is rejected rather than overwritten.
- Provisioning never deletes a channel automatically after a partial failure.
- External users cannot be silently added to a Guild; invitations or OAuth2
  consent remain separate administrative operations.
