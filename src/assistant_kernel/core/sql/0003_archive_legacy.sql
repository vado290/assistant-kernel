-- Phase 4b: the rebuilt core owns the two old tables it still uses (directory grants and the file
-- change ledger); every other table of the retired control core moves to archive_core5. Nothing is
-- dropped or deleted: archived rows stay queryable, and ids that pointed into archived tables stay
-- in their columns (only the foreign-key constraints across the boundary are removed).
-- assistant.schema_migrations stays where it is: it is this migration runner's own ledger.
CREATE SCHEMA IF NOT EXISTS archive_core5;

DO $$
DECLARE
    item record;
BEGIN
    IF to_regclass('assistant.project_grants') IS NOT NULL THEN
        ALTER TABLE assistant.project_grants
            DROP CONSTRAINT IF EXISTS project_grants_subject_run_id_fkey,
            DROP CONSTRAINT IF EXISTS project_grants_source_message_id_fkey,
            DROP CONSTRAINT IF EXISTS project_grants_approval_message_id_fkey;
        ALTER TABLE assistant.project_grants SET SCHEMA core;
    END IF;
    IF to_regclass('assistant.project_file_changes') IS NOT NULL THEN
        ALTER TABLE assistant.project_file_changes
            DROP CONSTRAINT IF EXISTS project_file_changes_legacy_message_id_fkey;
        ALTER TABLE assistant.project_file_changes SET SCHEMA core;
    END IF;
    -- Everything else of the old core: tables (with their indexes, owned sequences and constraints),
    -- views, standalone sequences and functions.
    FOR item IN
        SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'assistant' AND c.relname <> 'schema_migrations'
          AND (c.relkind IN ('r', 'p', 'v', 'm')
               OR (c.relkind = 'S' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('a', 'i'))))
    LOOP
        EXECUTE format('ALTER %s assistant.%I SET SCHEMA archive_core5',
                       CASE item.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                         WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, item.relname);
    END LOOP;
    FOR item IN
        SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'assistant'
    LOOP
        EXECUTE format('ALTER ROUTINE %s SET SCHEMA archive_core5', item.signature);
    END LOOP;
END $$;

-- A database that never had the old core (tests, a fresh install) gets the same two tables.
CREATE TABLE IF NOT EXISTS core.project_grants (
    grant_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    grant_kind text NOT NULL CHECK (grant_kind IN ('file', 'network', 'credential', 'account', 'trade')),
    resource_key text NOT NULL,
    subject_kind text NOT NULL CHECK (subject_kind IN ('project', 'task', 'run')),
    subject_run_id uuid,
    policy_json jsonb NOT NULL,
    policy_sha256 text NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    source_message_id uuid,
    approval_message_id uuid,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    expires_at timestamptz,
    CHECK ((subject_kind = 'project' AND subject_run_id IS NULL)
           OR (subject_kind IN ('task', 'run') AND subject_run_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS project_grants_active_file_root ON core.project_grants(project_id, resource_key)
    WHERE grant_kind = 'file' AND subject_kind = 'project' AND status = 'active';
CREATE INDEX IF NOT EXISTS project_grants_active_lookup ON core.project_grants(project_id, grant_kind, status, expires_at);

CREATE TABLE IF NOT EXISTS core.project_file_changes (
    change_id text PRIMARY KEY,
    project_id text NOT NULL,
    batch_id text NOT NULL,
    file_id text,
    target_uri text NOT NULL,
    path text NOT NULL,
    scope_kind text NOT NULL,
    operation text NOT NULL,
    before_sha256 text CHECK (before_sha256 IS NULL OR before_sha256 ~ '^[0-9a-f]{64}$'),
    after_sha256 text CHECK (after_sha256 IS NULL OR after_sha256 ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz,
    observed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    reason text NOT NULL,
    cause_status text NOT NULL CHECK (cause_status IN ('source_backed', 'unknown')),
    reason_source_refs jsonb NOT NULL,
    diff_ref jsonb,
    actor jsonb NOT NULL,
    task_ref jsonb,
    run_ref jsonb,
    lines_added bigint,
    lines_deleted bigint,
    file_lines_added bigint,
    file_lines_deleted bigint,
    diff_basis text,
    effect_status text NOT NULL,
    map_applied_generation text,
    record_json jsonb NOT NULL,
    legacy_message_id uuid UNIQUE,
    legacy_message_created_at timestamptz,
    UNIQUE (project_id, batch_id, target_uri, scope_kind)
);
CREATE INDEX IF NOT EXISTS project_file_changes_project_time ON core.project_file_changes(project_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS project_file_changes_project_path ON core.project_file_changes(project_id, path, observed_at DESC);
