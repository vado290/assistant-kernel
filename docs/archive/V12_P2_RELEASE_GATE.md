# P2 release gate and recovery boundary

Status: P2 passed and production runs 0.27.2/core5.v8 as of 2026-10-09.
`V12_P2_ACCEPTANCE.md` and the hashed matrix record the completed gates.
This runbook supplements the approved plan, without
changing its phase ordering or approving new business actions.

## Entry and scope

Use the P1 complete PostgreSQL/files backup pair and a new final cutover pair.
`inspect_upgrade_state.py` must enumerate waiting questions, approvals,
publications and unknown effects as well as active Runs. Preserve their original
IDs, accepted references and input contents. A waiting Task is not permission to
answer, cancel, requeue or replace its questionnaire.

P2 publishes the immutable v1.2 template candidate and readers. Production PM
and Supervisor output protocols remain their existing accepted versions until
the P3 controller and both channel renderers are ready. Package preparation,
Role-file generation and MCP discovery are not protocol adoption.

The directory agreement itself applies to every Project role independently of
the business-output ABI. Legacy PM and Supervisor must also use the current
agreement at their next invocation, while historical limited roots remain
limited. Prepared 0.27.0 is superseded before deployment because it incorrectly
coupled these two adoption boundaries; its sealed environment remains untouched.

## Gates exercised before acceptance

- D10 resolves directory growth by explicit simplification: defer the additional
  runtime growth-notification branch. Retain bounded proposal measurements and
  coverage disclosure. Size warnings are advisory, never directory/Run limits.
  No watcher, growth ledger or automatic cleanup is introduced.
- Completed the P2 coverage/evidence review, including invocation-selected
  context, actual PM/Supervisor capability loading and compatibility paths.
- Committed the exact final source, exported a byte-verified manifest, built a unique
  environment and verify the installed assets, dependency constraints and CLI.
- Ran the final migration preview against fresh production state. D07 permits
  only the specifically identified Kernel source-directory conversion.

## Cutover procedure after all gates pass

1. Record source and installed package identities; preserve the original checkout's
   unrelated modifications with guarded source sync. Preserve the tiktoken cache,
   existing Codex HOME/auth references, n8n state and current supervisor config.
2. Quiesce affected Kernel write/claim entry points in the approved window. Verify
   no executing/reconciling process and no unaccounted publish/effect result.
   Waiting user interactions are retained and checked separately.
3. Capture a consistent database/files/config snapshot and compare protected
   business digests before and after. Do not overwrite the P1 recovery pair.
4. Apply `migrate_p2_shared_baseline.py` with the exact approved legacy grant SHA.
   This retains the revoked historical Grant, adds the D07 successor, preserves
   both empty legacy background files and their hashes, accepts the new empty
   background document, splits configuration consumer groups, and archives the
   template candidate. Its journal permits inspection after each committed step.
5. Use the guarded source delta and switch only the Kernel release pointer to
   the verified environment. Restart affected services; preserve unrelated
   processes and all existing n8n workflows.
6. Verify version/package hashes, database compatibility, API/worker readiness,
   unchanged protected interaction IDs/answers/references, current Grant and
   background heads, pending/applied configuration groups and template hashes.
   Service health is one check, not the P2 acceptance conclusion.

## Recovery

Never overwrite an installed sealed environment. Preserve new snapshots,
context manifests, assembly receipts and native traces even if activation fails.
On a partially applied migration, inspect the journal and current DB heads;
repeat idempotently or perform an explicit compatible forward repair.

Before any binary rollback, compare the final cutover snapshot with current
messages, answers, approvals, Runs, accepted background and package heads. If
there are new user facts or results, do not restore the old database over them.
Retain readers for the new format and suspend only affected execution until
the compatible repair is installed.

The 0.26.0 background reader uses the retained legacy documents. Therefore a
newly accepted or withdrawn nonempty background after P2 cannot be recovered by
switching that binary alone. Similarly, an adopted v1.2 Agent package requires
its reader and controller; a pointer change cannot undo its accepted semantics.
Keep the D07 approval and successor history; do not silently reinstate an old
grant or erase a revocation. Restore database, Vault/workspaces, config/secret
references, CLI/dependencies and external object state together only when their
recorded point is compatible and no subsequent user/effect facts would be lost.

## P3 handoff

P3 must connect the compiled phase/approval/result validator to the tested
SessionBoundaryService transaction, adopt complete output packages, reconcile
Project-local Role/Skill differences and pin each Task's project policy. Existing
waiting Tasks retain their original binding until an explicitly covered legal
adoption boundary. The P2 primitive and technical CLI proof do not constitute a
completed human five-stage business acceptance.

The new Supervisor has actual MCP/file capabilities. Its actor name alone no
longer proves replay is free of effects. Interrupted new invocations retain the
existing reconciliation path until P3's evidence can establish a safe replay;
the configured retry delays are resolved but not falsely marked applied.
