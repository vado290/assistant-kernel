BEGIN;

ALTER TABLE assistant.runs
    DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE assistant.runs
    ADD CONSTRAINT runs_status_check CHECK (
        status IN (
            'queued','running','waiting_user','waiting_supervisor','waiting_children',
            'blocked','reconciling','succeeded','failed','cancelled','dead_letter'
        )
    );

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core5.v6"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
