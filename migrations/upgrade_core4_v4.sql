BEGIN;

ALTER TABLE assistant.runs
    DROP CONSTRAINT IF EXISTS runs_run_type_check;
ALTER TABLE assistant.runs
    ADD CONSTRAINT runs_run_type_check
    CHECK (run_type IN ('chat','stage','python','publish'));

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core4.v4"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
