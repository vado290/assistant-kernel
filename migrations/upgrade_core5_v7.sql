BEGIN;
ALTER TABLE assistant.runs ADD COLUMN IF NOT EXISTS queue_kind text NOT NULL DEFAULT 'interactive'
    CHECK (queue_kind IN ('interactive','runtime'));
ALTER TABLE assistant.runs ADD COLUMN IF NOT EXISTS mutex_key text
    CHECK (mutex_key IS NULL OR length(mutex_key)>0);
UPDATE assistant.runs SET
    queue_kind=CASE WHEN run_type='python' THEN 'runtime' ELSE 'interactive' END,
    mutex_key=CASE
      WHEN run_type='publish' OR request_json->'shared_write'='true'::jsonb THEN 'shared-write:global'
      WHEN right(agent_key,8)=':manager'
        AND COALESCE(NULLIF(request_json->>'current_stage',''),NULLIF(request_json->>'work_mode',''),'requirements')<>'operate'
        THEN 'engineering:'||project_id
      ELSE NULL END;
-- A Python parent or trigger is provenance, not its model Session.
UPDATE assistant.runs SET conversation_key='python:'||project_id||':'||run_id
WHERE run_type='python';
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_mutex ON assistant.runs(mutex_key)
    WHERE mutex_key IS NOT NULL AND status IN ('running','reconciling');
DROP INDEX IF EXISTS assistant.runs_one_active_execution_project;
CREATE INDEX IF NOT EXISTS runs_queue_claim_idx ON assistant.runs(queue_kind,status,available_at,created_at);
INSERT INTO assistant.kernel_meta(key,value) VALUES ('schema_version','"core5.v7"'::jsonb)
ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();
COMMIT;
