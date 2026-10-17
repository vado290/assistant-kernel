# Kernel scheduling 0.31.0 / core5.v11

The scheduler is a supervised Kernel process. PostgreSQL owns definitions' current
pointers, control revisions and occurrence facts. Finite programs use the existing
Python Run queue; service actions use the existing Unit/Instance controller.

## Management

Build produces a `kernel-schedule-definition.v1` through `write_runtime_document`.
Requirements describe `schedule_requirements`; Design fixes `schedule_plans`;
Implementation returns `schedule_handoffs`. Each plan states its acceptance IDs
and whether the first successful timed occurrence is required. The target program
is independently reviewed, published and registered.

Confirmed Operate uses `register_schedule` and `control_schedule`. Registration is
disabled. Controls are `update`, `enable`, `pause`, `resume`, `retire`, `run_now` and
`cancel_current`. Mutations require the actual current user confirmation, Project,
control revision and idempotency key. Read tools are `preview_schedule`,
`list_schedules`, `schedule_status`, and `list_schedule_ticks`. HTTP exposes the
same service under `/v1/projects/{project_id}/schedules`; write requests include
the confirmed Operate `task_id`.

## Time and transactions

Rules are once (`at` with offset), interval (`anchor` with offset and integer
`seconds >= 1`), or five-field calendar cron (`expression`, explicit IANA zone).
The default zone is Asia/Shanghai. Interval ticks retain their anchor. Cron uses
OR for restricted day/month and weekday fields, skips nonexistent local times,
and uses the first occurrence of a folded local time.

The process holds a PostgreSQL session lock on the connection which dispatches
ticks. Each dispatch takes the common RuntimeAdmission transaction lock, rechecks
Unit/Schedule state, writes its unique tick and existing Run/control request, and
advances the next time in one commit. Worker claim takes the same gate and stamps
an execution permit. A pause which commits first prevents claiming. A claimed
occurrence drains unless explicitly cancelled; retries keep the same Run/permit.

Downtime and reconnection establish a database-time cutoff. Earlier unsubmitted
times are skipped, never executed. Bounded historical logging does not prevent
future dispatch. Overlap includes queued work, waiting input, reconciliation and
unverified process cleanup. A once schedule becomes exhausted, which does not
mean the business succeeded. Manual firing does not advance or enable its clock.

Service maintenance skips busy, waiting, unavailable or stopped services. Manual
stop/pause/retirement and recovery protection pause related schedules. Scheduled
stop leaves future scheduled start rules intact. Pending controls cannot be
overwritten by another timer; manual override is tracked to its actual outcome.

## Results and visibility

Run/control records remain the execution authority. The scheduler records actual
settlement and inserts each occurrence notification in the same transaction.
Messages/Outbox own delivery and retries. Missing/ambiguous PM routes remain
pending; notification failure cannot replay the business. Immutable tick receipts,
operation observations and Project Map entries connect the fixed schedule to its
Run/control/result. Health reports enabled state, connection, latest scan and lag.

`/kernel/scheduler` has enabled, poll_seconds (1), late_grace_seconds (5) and
batch_size (100). Its configuration group is separate from the Worker group and
requires an actual scheduler consumer receipt. Rollout initially disables it.

## Release and rollback

Use a byte-preserving source baseline and the versioned 0.31.0 environment.
`migrate_core5_v11.py` adds only the two schedule tables and indexes.
`adopt_scheduler_release.py` updates accepted public packages and configuration
while preserving local additions, model/resource settings and old Task snapshots.
`Deploy-SchedulerRelease.ps1` takes a database backup, drains Kernel processes,
applies migration/adoption, starts the selected package with scheduling disabled,
and verifies protected interactions and readiness.

Rollback requires all schedules disabled/paused/retired and all submitted ticks
settled, with service cleanup verified. Restore old configuration/package heads
and executable pointer; retain the new tables and all evidence. Do not rewrite a
waiting user's Task or restore a stale whole database over new external effects.

Automated tests use explicitly synthetic authority in isolated databases. Actual
model turns, production process behavior and real user-channel acceptance must be
reported separately; process readiness alone is not end-to-end acceptance.

## 0.31.2 closure corrections

Scheduled service controls reserve a request first. The existing service consumer takes
RuntimeAdmission and validates the exact Tick and Schedule revision before committing
its start permit. Only then does it change desired state and close the service work gate.
Pause/retire winning first cancels the unstarted control; a granted occurrence drains.
Tick status exposes this actual service permit time. Cancelling the current occurrence
does not pause unrelated or future schedules.

Pause, retire and cancel use the registered Schedule/Unit identifiers even if the old
executable revision was replaced, retired or is unavailable. Execution and enabling
still require the current approved target. Random cron extensions are rejected.

Daily control of existing schedules uses the optional PromptDraft.schedule_actions
list, fixing schedule_id, definition_ref, action, expected_control_revision and changes
(rule/timezone/inputs only). The ordinary displayed Prompt confirmation remains required.
This task has only schedule authoring/control write tools and cannot register programs or
change execution permissions. Completion requires actual control receipts; it does not
claim new engineering acceptance or that run_now has already finished its business work.

Read-only listings return live facts. Existing invocation/tool traces supply evidence;
no separate per-query hash snapshot is written. Fixed version references and existing
idempotency checks remain. Historical backfill resolves definitions outside admission locks.
Rollback first requires paused clocks, settled ticks and no pending daily-operation tasks;
the old contract reader is checked against still-effective RuntimeUnits and manifests.
