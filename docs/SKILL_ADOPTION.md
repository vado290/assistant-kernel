# Skill adoption operations

This feature is off for every project unless an authorized maintainer configures an exact adoption policy.
Since 0.45.11 the per-version rollout of ROUND5 D24 runs again: copies a project never edited (content fingerprint
unchanged) are refreshed automatically. Adoption batches are only for special cases, such as merging a template into
a copy the project edited, or a whole batch that must be reversible.
Do not use this document as permission to enable it or deploy. The original task/approval/stage rules remain in force.

## Ownership and scope

- `core.skill_updates` owns only one project's coherent skill batch and its file-operation progress.
- Existing task/card approvals authorize work; `authorization_ref` points to the actual maintenance approval/policy record.
  It is not approval inferred from a model answer or merely from a source-template release.
- `core.projects.settings.skill_updates` is operator-owned configuration. No model tool writes this policy or the update table.
- `core.task_events` records progress; successful application adds an iteration through the existing machine owner for later effect review.
- `list_skill_updates` is project-scoped and read-only. Evolution receives the same evidence in its digest.
- `applied` means files read back correctly. Existing Run config hashes/skill labels establish loading; on-demand discovery
  alone does not prove reading. Business effects are reviewed using the existing effective/ineffective/pending mechanism.

## Policy and planning

After the existing maintenance approval, the maintainer may configure a policy such as the following in project settings.
All paths and hashes below are examples. This implementation does not enable any production policy.

```json
{
  "skill_updates": {
    "enabled": false,
    "releases": [{
      "base_wheel": "/approved/archive/old.whl",
      "base_sha256": "<verified old package SHA256>",
      "target_wheel": "/approved/archive/new.whl",
      "target_sha256": "<approved target package SHA256>",
      "selections": ["pm/example", "supervisor/example"],
      "authorization_ref": "<actual approval record or policy revision>",
      "compatible_with_active_tasks": false,
      "rollback_allowed": true,
      "expected_effect": "<approved improvement>",
      "measure": "<observable review evidence>"
    }]
  }
}
```

The maintenance tick reads this configuration; it no longer overwrites existing copies merely because the Kernel version changed.
Initial project creation still seeds templates. An existing removed copy is not reinstalled by adoption.
Use one release entry per coherent project batch. Different approved targets must not race or silently supersede each other.
Remove obsolete entries or explicitly defer their batches. Pending/blocked batches do not obstruct normal work.

Administrative commands use the configured host/database. Always verify the environment first:

```text
python -m assistant_kernel.core.skill_updates preview --project PROJECT --release RELEASE.json
python -m assistant_kernel.core.skill_updates prepare --project PROJECT --release RELEASE.json
python -m assistant_kernel.core.skill_updates list --project PROJECT
python -m assistant_kernel.core.skill_updates apply --project PROJECT --update-id UUID
python -m assistant_kernel.core.skill_updates rollback --project PROJECT --update-id UUID
python -m assistant_kernel.core.skill_updates recover --project PROJECT --update-id UUID
python -m assistant_kernel.core.skill_updates defer --project PROJECT --update-id UUID --reason REASON
```

`preview` only reads. `prepare` writes the batch and snapshots, not project copies. `apply` and `rollback` check the current exact
policy entry. Disabled or changed policies cannot authorize writes. Recovery of a previously started operation remains available
when policy is disabled, to finish reconciliation rather than leave partial skills active.

## Customization and immutable inputs

Each wheel is read once; both contents and file permissions are parsed from the same hash-verified bytes, even if the path changes later.
The old wheel must match its verified hash. Compare old template, project copy and target, including YAML values, body,
support files and file modes. Missing baselines or local changes become visible blocked batches, not guessed defaults.
Symlink resources and special permission bits require manual handling. Missing business directories are not created.

A reviewed merge may be supplied in the exact approved policy's `overrides`, keyed by `agent/skill`:
`{"path": "<reviewed candidate directory>", "sha256": "<candidate manifest digest>"}`.
The digest is SHA256 of canonical compact, sorted-key JSON with `files` (relative path to SHA256) and `modes`
(relative path to integer permission mode). The candidate must be validated/approved through the existing task or maintenance
workflow; the maintainer supplies the reference, not a new model approval field.
Load changes are compared against the actual project copy and require `allow_load_changes: true` in that exact policy.
Merged copies retain upstream template identity; they are not stamped as pristine merged templates.

Snapshots are stored under `data_root/runtime/skill-updates/PROJECT/UUID/`, outside business vaults. Never clean referenced
before/after snapshots while recovery or review still needs them. There is no automatic garbage collector in this change.
The plan digest covers pinned file/mode manifests. Changed wheel or candidate files cannot silently change a prepared plan.

## Concurrency, interruption and rollback

Run claim and batch start share the project row lock and recheck eligibility after locking. Claim still honors run/task mutexes
and oldest-run order. `applying`, `rolling_back` and `reconciling` stop new claims only for the affected project.
No running/reconciling Run is bypassed. By default an active task defers adoption; compatible existing tasks require an explicit
policy flag, and uncertain Release writers still require their existing stop evidence. Cards, approvals and task stages are untouched.

The writer additionally holds a project OS file lock. DB disconnection or lease expiry alone cannot authorize a second writer.
File writes are synchronous inside this lock, so coroutine cancellation cannot leave a detached publication thread running.
External file editors must honor the maintenance window; DB locks do not prevent manual filesystem edits.

Recovery checks the whole batch before writing:
- Completed target with only an application receipt missing: confirm application once.
- Known old/new mixture: restore the backed-up batch and record rollback.
- Third-party content, changed permissions, missing snapshot or uncertain state: keep the reconciliation barrier and notify Supervisor
  with the update ID and maintainer recovery conditions. Do not clear the barrier or overwrite later edits merely to unblock work.

The maintainer preserves later edits externally and restores a verifiable old/new state before retrying `recover`.
If rollback was blocked **before any write**, its receipt retains the rollback direction (or the previously applied state if the plan was damaged).
After preserving later edits and restoring the verified applied files, snapshots or original pinned plan, retry `rollback` with the same update ID. It rechecks policy, locks and all file evidence;
`recover`, ordinary apply, and the maintenance tick do not automatically resume this blocked rollback.
Original target + selections + authorization reference identify the batch; rescanning does not reapply rolled-back/deferred targets.
A new application attempt after rollback/defer or a failed preparation requires a new explicit authorization reference and plan.
After a corrected candidate is applied, the maintainer can `defer` the superseded unwritten batch with a reference to the successful update, closing its follow-up without deleting history.
No database-wide restore, task cancellation, business directory migration or runtime-data deletion is performed.

## Directory-contract first adoption

Use the verified old release and a newly validated target package, selecting only the eight agreed skill types and applicable agents.
Validate in isolated projects first: old charter without layout notes, full/quick/operation loading, customized metadata/resources,
partial writes, current cards, rollback, and preserved business data. New rules must not force rewriting project charters/maps or
creating an entire directory tree. Production application is a separate approved operation; this source change does not perform it.
