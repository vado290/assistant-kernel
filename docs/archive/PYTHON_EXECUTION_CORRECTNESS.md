# Python execution correctness repair — 2026-10-08

Acceptance baseline: the repair design in the current conversation, followed by the
user's explicit request to implement it rigorously with minimal intrusion.

## Implemented scope

- Python execution uses `runs/<run_id>/attempts/<attempt>/`. The existing database
  claim counter supplies the attempt; an existing attempt directory is not overwritten.
- Input/result environment variables point to that attempt. `ASSISTANT_ATTEMPT` and
  `ASSISTANT_OUTPUT_DIR` are also provided. The Project Vault cwd is unchanged.
- Only the current attempt's result is read. Legacy auxiliary scripts may still omit
  JSON results. Business acceptance remains separate from process exit success.
- Each attempt retains stdout, stderr, result if present, and an `execution.json`
  receipt. Failure, timeout, cancellation and launch failure retain available evidence.
  A claim-fenced `executor_json.python_attempt` records the current observation and
  artifact references; the existing Run status remains authoritative.
- Python-only process cleanup checks the private Linux session, recorded identity and,
  when the parent has exited, inherited Run/attempt markers. Normal spawn pools get a
  bounded one-second natural-exit window for resource trackers. Stop uses TERM with a
  five-second grace, then KILL and up to five seconds of confirmation.
- Unknown identity is never a reason to signal an unrelated process. Unverified
  cleanup keeps the Run in the existing reconciliation flow. The recovery service and
  database card transition both prevent resolving/replaying a new-protocol Python Run
  without verified cleanup. Legacy Runs retain their existing compatibility path.
- Worker-expiry maintenance uses the same Python cleanup for attempts with recorded
  evidence. It does not claim a filesystem receipt is proof of a successful DB commit.
- Python authoring guidance uses supplied paths, bounded joined pools, and truthful
  shared-write declarations.

## Compatibility and deliberate limits

No database migration, new queue, new public API, scheduling algorithm, or new retry
policy is introduced. Historical root-level logs remain untouched. Consumers should
follow returned artifact references rather than construct new log paths themselves.
The generic Codex process helper and `shared-write:global` are unchanged: independent
Runs remain parallel; declared shared writes retain their current serialization.

This is not an OS sandbox, arbitrary detached-process tracker, external-effect rollback,
or automatic recovery of a process whose launch identity was never captured. Those
uncertain cases remain visible for inspection. A stopped process does not undo writes
or other effects. Attempt receipts can feed the planned unified log view without
creating another execution authority.

The n8n/Agent/LangGraph/service feature implementations remain outside this repair.
Their delivery still requires a real minimal scenario: timer to Kernel to result;
actual required model/MCP loading; used graph branches and, when applicable, answer
resumption; collector start, checkpoint, stop and restart. Generating a definition is
not evidence that the corresponding feature is live.

## Verification map

`tests/test_python_attempts.py` exercises stale-result isolation, immutable attempts,
invalid/missing output compatibility, normal spawn pools, timeout/cancellation, lost
claims before/after launch, parent exit with surviving children, TERM-resistant child
cleanup, wrong identity refusal, unverified-cleanup reconciliation, transactional
evidence/card guards, and cross-Project concurrency/shared-write protection.

`tests/test_slimming.py`, `tests/test_process_control.py`, and
`tests/test_five_stage_worker_hooks.py` preserve same-Project parallelism, interactive
capacity, idempotency, generic Codex process behavior and operation dispatch validation.
Database tests create/drop isolated test databases; no production Run or message is
created for these checks.

Validation on 2026-10-08: focused regressions **37 passed**; the full source suite
**424 passed in 414.75 seconds**. The full suite includes the focused tests.
Command: `PYTHONPATH=src python -B -m pytest -q -p no:cacheprovider`, using the existing
container Kernel Python environment. This is source/integration evidence, not a
production deployment or human-channel E2E receipt.

| Accepted obligation | Evidence |
| --- | --- |
| Attempt isolation and preserved old files | failed-then-missing-result and repeated-attempt tests |
| Existing JSON/cwd/environment compatibility | auxiliary result, spawn pool and existing parallel-script tests |
| Failure/timeout/cancel evidence and claim fencing | current-attempt receipts, lost-claim and isolated DB tests |
| Bounded group cleanup, including parent exit | actual parent/child, TERM-resistant child and wrong-identity tests |
| Unknown cleanup cannot authorize a second execution | recovery service and direct database card-guard tests |
| Independent work remains parallel; shared writes protected | same/cross-Project tests; unchanged scheduling policy |
| PM/Supervisor/publish/recovery compatibility | complete 424-test source regression; generic helper unchanged |
| Authoring guidance reflects managed execution | supplied-path and joined-pool rules in implementation Skill |
| No production rollout or schema migration in this repair | source-only package boundary; no new migration/API/queue |

## Deployment boundary

This change updates source and tests only. The production version pointer and installed
package are not changed. Before release, retain the existing deployment backup and
check in-flight/reconciling Python attempts. On rollback, preserve new attempt evidence;
do not resume a cleanup-unverified attempt using an older reader that lacks the guard.
