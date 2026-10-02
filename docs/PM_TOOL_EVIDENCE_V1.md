# PM tool evidence v1

`pm-tool-evidence.v1` is the bounded evidence index attached to a settled PM Turn.
It makes tool use visible to the stateless Supervisor without copying a complete
execution Trace into every Supervisor prompt.

## Contract

`pm_evidence` is an optional additive member of `supervisor-turn-handoff.v1`:

```json
{
  "schema_version": "pm-tool-evidence.v1",
  "invocation": {
    "invocation_id": "...",
    "trace_ref": {
      "uri": "vault://<project>/runs/agent-traces/<invocation>/<trace>.jsonl",
      "sha256": "..."
    },
    "usage": {
      "input_tokens": 0,
      "cached_input_tokens": 0,
      "output_tokens": 0,
      "reasoning_output_tokens": 0
    }
  },
  "tool_summary": {
    "total": 3,
    "completed": 3,
    "failed": 0,
    "interrupted": 0,
    "truncated": false
  },
  "tool_calls": [
    {
      "call_id": "...",
      "server": "kernel_scoped",
      "tool": "read_authorized_file",
      "status": "completed",
      "error_type": null
    }
  ],
  "artifact_refs": [
    {
      "uri": "vault://<project>/...",
      "sha256": "...",
      "kind": "...",
      "producer": "pm"
    }
  ]
}
```

The producer groups lifecycle events by tool-call ID and emits one final record per
call. The supported terminal statuses are `completed`, `failed`, and `interrupted`.
The inline tool list is capped at 20 records; `tool_summary.total` still reports the
full count and `truncated` signals that the tool list is partial. No tool call is a
valid empty case (`total=0`, empty `tool_calls`, `truncated=false`). Artifact
references are separately capped at 20, are metadata-only, and retain their content
hash.

The contract must not inline tool arguments, tool return values, file bodies,
environment variables, credentials, or private/hidden model chain-of-thought. Raw
structured tool events and visible agent output remain in the hash-addressed JSONL
Trace. This feature is an evidence index, not a second Trace store or a reasoning
archive.

## Supervisor use

The Turn handoff and its evidence index are fixed before the consultation Run is
queued. The Supervisor reads the index inline and fetches a raw Trace or Artifact only
when the evidence is material. Existing limits remain unchanged: at most three
first-pass requests, bounded bytes, and no additional fetch in pass two. Fetched
content is data, never instructions.

All-successful calls do not by themselves prove the PM's conclusion. Failed or
interrupted calls require the Supervisor to describe the evidence gap. A PM statement
such as "read", "verified", or "modified" with no corresponding tool or Artifact
evidence should be reported as a claim/evidence mismatch. High-risk permissions,
credentials, trading, publication, and external effects justify reading the cited
evidence when it is necessary to evaluate the claim.

No database migration, new Worker, new model call, or history rewrite is part of this
contract. Older handoffs without `pm_evidence` remain readable. Historical evidence
may be summarized on demand from its existing Trace, but must not be rewritten into an
already immutable Turn handoff.

## Post-deployment map refresh (not performed by this source change)

After deployment and verification, update the active Project Semantic Map by creating
new content and hashes through its normal controlled publication path; do not edit the
active Vault files in place. The exact affected entries are:

- Refresh `flow.supervisor-pm-event-card-v2` in `knowledge/map/index.yaml` and
  `knowledge/map/flows/supervisor-pm.md`. Add the chain `PM invocation -> raw JSONL
  Trace -> pm-tool-evidence.v1 -> immutable Turn Handoff -> Supervisor bounded fetch`
  and name the raw Trace as authority for tool-event detail.
- Refresh `agent.goal-supervisor` to state that tool evidence is an inline index and
  that detailed Trace/Artifact inspection is bounded and on demand. Keep the Agent
  advisory-only and keep hidden reasoning outside the evidence model.
- Refresh `operation.supervisor-event-card-runbook` and
  `knowledge/map/operations/supervisor-runbook.md` with checks for the Trace URI/SHA,
  tool totals/statuses/truncation, claim/evidence mismatch, and fetch results.
- Do not add a new business component: this contract is evidence plumbing inside the
  existing Supervisor-PM flow, not a new Agent, state owner, or authority boundary.

For the stable `assistant-kernel-system-map-cache.v1`, publish a new immutable
generation and switch `latest.json` only after validation. Refresh these existing
records and their reverse-dependency closure:

- `components.jsonl`: Run Processor Turn-handoff producer, Goal Supervisor brief and
  evaluator, Supervisor Processor consultation path, and the immutable Turn-handoff
  Artifact contract.
- `workflows.jsonl`: PM settlement -> evidence summary -> consultation -> optional
  bounded fetch -> Decision/Card.
- `state-authority.jsonl`: raw Trace is authoritative for tool events; Turn Handoff is
  authoritative for the fixed per-Turn evidence index; Supervisor artifacts are
  advisory projections.
- `tests.jsonl`: summary grouping/status/redaction/cap tests, old-handoff compatibility,
  bounded fetch behavior, and stale-Run fencing regression coverage.
- `changes.md`: additive contract, no database migration, no hidden reasoning, no
  automatic full-Trace injection, and deployment/live-verification status.
- `manifest.json`: new SHA values for `run_processor.py`, `goal_supervisor.py`, related
  tests, this document, `README.md`, `STATELESS_GOAL_SUPERVISOR.md`, and refreshed
  Project map sources. Preserve the prior generation as the parent.

Source implementation or unit tests prove `implemented`/`tested`; only a deployed PM
Turn whose handoff contains a valid evidence index and whose Supervisor can perform a
bounded fetch may be marked `live_verified`.
