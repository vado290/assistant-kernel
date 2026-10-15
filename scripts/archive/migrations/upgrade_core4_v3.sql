BEGIN;

ALTER TABLE assistant.sessions
    ADD COLUMN IF NOT EXISTS logical_session_id uuid,
    ADD COLUMN IF NOT EXISTS usage_known boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS usage_source text,
    ADD COLUMN IF NOT EXISTS last_invocation_id text,
    ADD COLUMN IF NOT EXISTS handoff_checkpoint_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS handoff_checkpoint_sha256 text,
    ADD COLUMN IF NOT EXISTS handoff_cutoff_message_id uuid,
    ADD COLUMN IF NOT EXISTS handoff_created_at timestamptz;

WITH RECURSIVE session_tree AS (
    SELECT session_id,session_id AS logical_session_id
    FROM assistant.sessions
    WHERE parent_session_id IS NULL
    UNION ALL
    SELECT child.session_id,parent.logical_session_id
    FROM assistant.sessions child
    JOIN session_tree parent ON child.parent_session_id=parent.session_id
)
UPDATE assistant.sessions target
SET logical_session_id=session_tree.logical_session_id
FROM session_tree
WHERE target.session_id=session_tree.session_id
  AND target.logical_session_id IS NULL;

UPDATE assistant.sessions
SET logical_session_id=session_id
WHERE logical_session_id IS NULL;

UPDATE assistant.sessions
SET usage_known=(context_window_tokens IS NOT NULL AND input_tokens>0),
    usage_source=CASE
        WHEN context_window_tokens IS NOT NULL AND input_tokens>0 THEN 'legacy_session_columns'
        ELSE usage_source
    END,
    handoff_checkpoint_json=checkpoint_json
WHERE TRUE;

ALTER TABLE assistant.sessions
    ALTER COLUMN logical_session_id SET NOT NULL;

ALTER TABLE assistant.sessions
    DROP COLUMN checkpoint_json;

CREATE INDEX IF NOT EXISTS sessions_logical_history_idx
    ON assistant.sessions(project_id,logical_session_id,created_at DESC);

ALTER TABLE assistant.sessions
    DROP CONSTRAINT IF EXISTS sessions_handoff_cutoff_message_fk;
ALTER TABLE assistant.sessions
    ADD CONSTRAINT sessions_handoff_cutoff_message_fk
    FOREIGN KEY (handoff_cutoff_message_id)
    REFERENCES assistant.messages(message_id) DEFERRABLE INITIALLY DEFERRED;

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core4.v3"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
