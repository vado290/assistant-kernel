# C26 — scheduled execution deferred after P6

The user changed the upgrade scope on 2026-10-11 (Asia/Shanghai): remove the external scheduling backend now; design Kernel-owned scheduling separately after P6. Do not build an internal replacement during this upgrade.

## Active scope

- Keep finite Python execution, business Agent/graph work, manual service start/stop/restart, per-work-item accounting, user questions, cancellation, bounded fault recovery, and operation observation.
- Keep the Kernel's existing Worker polling, heartbeats, leases, Outbox retry and recovery loops. They are not user business timers.
- Retire business once-at-time/recurring schedules, scheduled reports, automatic periodic maintenance, workflow generation/registration/enabling, callback/tick admission and their scheduled-execution acceptance cases.
- Historical business requirements remain readable. C26 changes the foundation upgrade scope; it does not mark an old business deliverable successful, consume an answer, or rewrite an approval.

## Implementation

The current contract registry has no schedule, trigger, backend-readiness or maintenance-timer writers. Runtime definitions accept manual triggers; service manifests have no periodic restart policy. Old shapes remain compatibility definitions and archived readers retain their original bytes. New configuration omits all retired backend/schedule paths. The current view of an older config projects only active fields; applying C26 creates a new accepted revision and keeps the prior source intact.

Schedule Skills and loading rules are removed from both public template sets. Candidate validation refuses newly proposed schedule assets. PM and Supervisor receive the current capability restriction even when an older Task retains its fixed Role/Skill snapshot. Existing business/manual API endpoints are preserved; there was no deployed scheduling adapter to replace.

Kernel backup and configuration import no longer require the retired backend, and new internal-token initialization no longer creates its callback credential. Existing external application data and historical keys are not deleted.

## Phase adjustment

- P4 continues W21–W27 without a scheduler dependency.
- P5 retains W28 service registration and W29 manual lifecycle/maintenance, busy/waiting protection and bounded recovery. W30/W31 and the timer-triggered part of W29 are explicitly deferred, not passed.
- P6 verifies the remaining effective requirements, user channels, migration/recovery and Linux portability. Backend-specific deployment and scheduled-execution cases are no longer blockers.
- A future scheduling module requires its own registration and due/overlap/resume design. This release does not pre-create its tables, loop, API or platform.

## Release and recovery

0.28.8 is based on the running 0.28.7 source and retains core5.v8; it does not include the unfinished P4 checkpoint. Before cutover preserve source, installed environment, database, Vault/config and accepted package refs. Then preserve both contract/template readers, accept only the C26 config projection and apply public template changes at the approved package boundary. Old waiting Task bindings stay intact.

On rollback retain the newly archived readers and accepted configuration facts. The old binary expects the former config fields; restore the prior accepted config/input projection together with the release pointer while stopped, without restoring the whole database over newer user input. Original task/question/answer/approval data are not migrated by this change.
