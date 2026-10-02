BEGIN;

DROP INDEX IF EXISTS assistant.runs_one_active_project;

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_project_lane
    ON assistant.runs(
        project_id,
        COALESCE(
            executor_json->>'scheduling_lane',
            CASE
                WHEN run_type='chat'
                 AND (agent_key='system-butler' OR right(agent_key,8)=':manager')
                THEN 'conversation'
                ELSE 'execution'
            END
        )
    )
    WHERE status IN ('running','reconciling');

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core5.v3"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
