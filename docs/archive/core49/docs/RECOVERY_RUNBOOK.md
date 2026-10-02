# Assistant Kernel V2 recovery runbook

## Backup contents

`Backup-AssistantState.ps1` creates a timestamped directory under
`H:\AI\assistant-data\backups` containing:

- a PostgreSQL custom-format dump (Kernel business state and Session rotation checkpoints);
- Project Vaults, native traces, artifacts, and per-project runtime databases;
- n8n workflows and encrypted credential exports;
- Assistant Kernel source, Compose, migrations, and a SHA-256 manifest.

Per-project `auth.json`, provider secrets, and service credentials are excluded.
Restore those through the existing secret-management files and then run
`refresh_codex_harness_auth.py`.

## Restore gate

1. Run a fresh backup of the current state before restoring an older one.
2. Verify every file against `sha256-manifest.json`.
3. Record the exact backup directory and stop `kernel-api` and `kernel-worker`.
4. Move the current `assistant-data\vaults` directory to a timestamped
   pre-restore directory; do not delete it.
5. Copy the selected backup's `vaults` directory into `assistant-data\vaults`.
6. Copy `postgres.dump` into `rag-postgres` and run `pg_restore -Fc --clean
   --if-exists --no-owner` against the configured database.
7. Import n8n credentials first, then workflows with `--activeState=fromJson`.
8. Re-provision Codex Harness auth for every restored active project.
9. Start the Kernel services and run `pytest`, `verify_v2.py`, and one approved
   read-only Project Build stage before allowing external delivery.

Never restore only database state or only filesystem artifacts.
The database dump and Project Vault snapshot are one recovery set.
