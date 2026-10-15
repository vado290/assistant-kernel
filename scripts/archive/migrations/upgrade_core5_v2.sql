BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_project
    ON assistant.runs(project_id)
    WHERE status IN ('running','reconciling');

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core5.v2"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
