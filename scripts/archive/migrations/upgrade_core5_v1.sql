BEGIN;

CREATE TABLE IF NOT EXISTS assistant.project_grants (
    grant_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    grant_kind text NOT NULL CHECK (grant_kind IN ('file','network','credential','account','trade')),
    resource_key text NOT NULL,
    subject_kind text NOT NULL CHECK (subject_kind IN ('project','task','run')),
    subject_run_id uuid REFERENCES assistant.runs(run_id),
    policy_json jsonb NOT NULL,
    policy_sha256 text NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    source_message_id uuid REFERENCES assistant.messages(message_id),
    approval_message_id uuid REFERENCES assistant.messages(message_id),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    expires_at timestamptz,
    CHECK ((subject_kind='project' AND subject_run_id IS NULL)
        OR (subject_kind IN ('task','run') AND subject_run_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS project_grants_active_file_root
    ON assistant.project_grants(project_id,resource_key)
    WHERE grant_kind='file' AND subject_kind='project' AND status='active';
CREATE INDEX IF NOT EXISTS project_grants_active_lookup
    ON assistant.project_grants(project_id,grant_kind,status,expires_at);

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core5.v1"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;

