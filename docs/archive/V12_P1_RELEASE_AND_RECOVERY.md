# P1 release and recovery procedure

This procedure is authorized by the approved upgrade plan, but its prerequisites
must be satisfied. It does not authorize a migration while the configuration
contract decisions D08/D09 or a phase acceptance blocker remain open.

## Prepare without interrupting production

1. Freeze a clean commit of the isolated worktree; retain the separate original
   Python repair commit. Export `scripts/export_release_manifest.py` on the Git
   equipped Windows host. Keep the source manifest and test evidence together.
2. Resolve configuration blockers and regenerate the fixed library from the
   original design SHA plus approved corrections. Re-run affected validations;
   record exact code/contract hashes against test evidence.
3. Build a new unique runtime using `scripts/prepare_release.py`. It clears
   PYTHONPATH/PYTHONHOME, uses the locked dependency closure, verifies installed
   files and imports, and seals the directory. Never install over `.complete` or
   the current version. `pip check` and matching version alone are insufficient.
4. Preview `scripts/sync_release_source.py` against the recorded original-source
   manifest and preserved repair commit. Any unrelated source drift blocks the
   copy. Apply the tested delta with its ZIP backup; preserve the original Git
   index, existing changes and branch. This does not change the running package.
5. Run `scripts/inspect_upgrade_state.py` read-only. Inspect pending drafts,
   approvals, publish/effect markers and Outbox dispositions; retain all waiting
   questions. Existing seven failed messages are not requeued by the upgrade.

## Controlled cutover

1. Stop Kernel input endpoints/channels and maintenance with the existing
   supervisord configuration. Drain the interactive/runtime workers using their
   existing graceful TERM handling; do not replace it with a kill or infer safety
   from a stale preflight. Stop MCP after worker draining. Do not stop docling or
   unrelated services. Verify actual stopped PIDs and no running/reconciling Run.
2. Repeat the detailed inspection after writers stop. Save a fresh PostgreSQL
   dump and all referenced Vault/project/config state, current version pointer,
   runtime environment, live supervisor file and source delta backup. Include
   `runtime/contract-libraries`, accepted configuration inputs and consumer
   receipts. Archive the non-secret manifest separately from restricted secret
   material. Preserve n8n Workflow states; P1 never changes its existing Intake.
3. Verify the stopped snapshot and backup digests, then run `migrate_core5_v8.py`
   dry-run and apply. Re-run the inspection and compare business-table digests,
   pending question hashes and every accepted document reference before exposure.
4. Import only the reviewed configuration candidate with explicit provenance;
   first keep all non-adopted groups pending. Expose get/validate/status and the
   authorized Kernel Skill through a fixed adopted management package. Do not
   switch old Task output protocols or consume existing questions.
5. Atomically change the version pointer to the sealed candidate. P1 does not
   require a wholesale rewrite of the live supervisor configuration. Start the
   existing service set and verify actual process/package/version/schema hashes,
   both worker queues, API readiness, channel connectivity and old Task reads.
6. Compare the protected waiting interactions and accepted refs to the stopped
   snapshot. If genuine new input arrived after exposure, account for its new
   durable records instead of declaring them corruption or reverting them.
7. Record the P1 requirement/evidence acceptance matrix and then permit P2.

## Failure and recovery boundaries

- Before schema changes: restore only the upgrade's source delta if necessary,
  keep the original pointer and restart the original service set. Reverify current
  source bytes before restoring any file changed by another writer.
- After v8 migration but before new configuration or business exposure:
  `migrate_core5_v8.py --rollback-before-state <stopped-state.json>` previews the
  guarded rollback. `--apply` succeeds only when all business facts match the v7
  snapshot and every ledger row is an exact preserved legacy-message replica.
  Restore the source delta and original pointer together, then restart/read back.
- After configuration acceptance or new business writes: the narrow downgrade
  refuses. Keep compatible v8 readers, preserve all new answers/approvals/effects,
  and repair forward or prepare an explicit reconciled restore. Do not simply
  activate 0.25.0, which cannot interpret all new state or its schema readiness.
- Unknown process identity/cleanup or external effects: stop affected new claims,
  retain reservations and evidence, and reconcile. Never kill using an incomplete
  identity or replay an uncertain side effect as a recovery shortcut.
- Full restore must reconcile DB, Vaults, accepted configuration, archived reader
  libraries, source/runtime pointer, environment and external object state as one
  recovery set. The P0 restored DB/filesystem rehearsal is evidence for the old
  baseline only; it does not authorize erasing later input.
