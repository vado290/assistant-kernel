BEGIN;

ALTER TABLE assistant.runs
    DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE assistant.runs
    ADD CONSTRAINT runs_status_check CHECK (
        status IN (
            'queued','running','waiting_user','reconciling',
            'succeeded','failed','cancelled','dead_letter'
        )
    );
ALTER TABLE assistant.runs
    ADD COLUMN IF NOT EXISTS interaction_seq integer NOT NULL DEFAULT 0
        CHECK (interaction_seq >= 0),
    ADD COLUMN IF NOT EXISTS claim_token uuid,
    ADD COLUMN IF NOT EXISTS process_pid integer,
    ADD COLUMN IF NOT EXISTS process_start_ticks bigint;
ALTER TABLE assistant.runs
    ADD COLUMN IF NOT EXISTS conversation_key text;
UPDATE assistant.runs
SET conversation_key='legacy:' || run_id::text
WHERE conversation_key IS NULL;
ALTER TABLE assistant.runs ALTER COLUMN conversation_key SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_conversation
    ON assistant.runs(conversation_key)
    WHERE status IN ('running','reconciling');

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core4.v2"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
