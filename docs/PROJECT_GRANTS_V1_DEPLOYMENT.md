# Project file grants rollout (deployed 2026-09-18)

## Production result

- Installed `assistant-kernel 0.11.1` with `core5.v1` and four active file
  grants. `CORE5_VERIFY_OK` and the API ready check passed. The three active
  Projects' 12 core Skill manifests matched their bundles.
- The first cutover failed its native shell test because `/home/v` was mode
  `0750`, preventing Codex's unprivileged bwrap user namespace from traversing
  the parent of authorized paths. It was rolled back to 0.10.4/core4.v5 with
  no new messages or Runs. The AI entrypoint now adds only the directory
  traversal bit (`0751`); the Windows host permissions were not changed.
- After the second cutover, production Codex `sandbox`, `exec` fresh, and
  `exec resume` made real shell calls. Both trading Roots allowed read, write,
  and command execution; paths outside the grant and an excluded IB config
  file were denied. `exec resume` also required moving `-p` before `resume`.
- Full source suite: 120 passed; focused Harness suite after the resume fix:
  10 passed. After adding directory and symlink effect receipts, the full
  source suite passed 121 tests; the focused receipt suite passed 3 tests.
  No user-sent Feishu Project Run has been used for the final
  channel-level E2E. File grants do not enable network or make Alpaca scripts
  at the `crypto` root part of the narrowed native Scope.
- A follow-up native CLI test confirmed create/delete of files and directories
  in both trading Roots and denial of out-of-Scope creation/deletion. It also
  found that a file matching `.env.*` created *after* profile materialization
  was accessible in the same Run. Existing excluded files were denied. This
  is an unresolved limit of snapshot-expanded exclusions for direct-write
  Roots; do not describe future matching files as continuously hard-denied.
- Pre-cutover backup: `assistant-data/backups/20260918-125630-project-grants-cutover`.
  Provider and service credential file hashes were unchanged.

## Boundary

One durable `assistant.project_grants` table owns active Project grants. The
existing Project Scope YAML remains a legacy projection and the discovery
catalog. New Runs pin grant IDs and a hash-checked native Codex permission
profile. All Project Agents inherit the same Project file grants. `direct`
working roots change immediately; `gated` roots remain behind candidate,
Validation, Review, and Publisher. Network, credential, account, and trade
executors are separate and are not supplied by this file-grant rollout.

The owner selected a narrow `crypto` directory and kept exclusions as hard
denials. Import `crypto_strategy_reference` as a direct native grant for
`/home/v/AI/crypto/trade8/strategies`; import the already approved
`IB/tradeIB` root as direct. Do not import the unrelated Binance
`history_fetch_script` grant. The trading Project Card is updated during the
Agent bundle sync to permit direct edits only inside the active file grants.
Both imports compile to a native policy with the current Codex CLI. These
grants do not open network access. The existing Alpaca scripts at the `crypto`
root are outside the narrowed native grant; no data download is claimed by
this file-permission rollout.

## Pre-deployment evidence

- Source/config backup: `assistant-data/backups/20260918-101900-project-grants-v1`.
- PostgreSQL assistant-schema dump: `assistant-pre-project-grants.dump` in that
  backup (SHA-256 `8ccb659e6f819a6d148b0677e5328f5f746c0f520b92485d2c25a5817d7ab877`).
- Previous 0.10.4 wheel is in the backup's `wheels` directory.
- Owner-approved isolated migration: 4 grants imported; repeat imported 0;
  the two trading Roots resolve to direct native paths shown above. The test
  database was dropped after verification.
- Native Codex 0.151.0 sandbox with the derived Docker seccomp profile: allowed
  test workdir write; denied outside read, `.env` read, and command network.
- Full source test suite: 120 passed before the latest small changes; rerun
  after final grant terms are chosen.

## Deployment order

1. Owner decisions are resolved as stated above. Rebuild the wheel after final
   source changes and rerun the relevant tests.
2. Verify there are no queued, running, or reconciling Runs. Preserve waiting
   and blocked Run IDs and raw traces. Verify the backup and current service
   credential hashes.
3. Build `assistant-ai:0.1.0` from the current `Dockerfile.AI` **before**
   recreating AI. The wheel lives in the image, so a pip install into the old
   container would be lost on recreation.
4. Stop only Kernel API, Worker, and MCP through the configured supervisor
   socket. Apply `migrations/upgrade_core5_v1.sql` to the existing assistant
   schema, then run `scripts/migrate_project_grants.py --apply` once. Compare
   the four imported policies and run the importer again to confirm 0 inserts.
5. Run `scripts/sync_project_permission_agents.py --apply` with the saved
   baseline. Verify all 12 Project Skill manifests. Keep historical Run/Skill
   snapshots unchanged.
6. Render Compose, then recreate **AI only** with the tested seccomp profile;
   the AI container also hosts IB/VNC, so recheck those processes and ports.
7. Require `/health/ready`, Core5 schema verification, real Codex fresh and
   resume shell tests, file boundary denials, and a Feishu inbound-to-reply
   Project Run. A ready endpoint alone does not complete business E2E.

## Failure handling

Do not automatically replay a Run that may have changed a shared root. Use its
`file-effects` receipt and protected backup, then explicitly reconcile. If
cutover fails before any new Run acts, restore the prior wheel, active
Skill/Role/config files, Compose, and assistant schema from the backup. After
new file effects occur, perform a reviewed reconciliation rather than a blind
database rollback. Never remove the existing backup during rollback.
