BEGIN;

-- v6.1 conflict-domain minimization:
--   * supervisor/operator chats join the conversation classification;
--   * cross-agent same-lane project exclusion is removed at the hard-constraint
--     level. Conversation runs serialize per conversation_key only
--     (runs_one_active_conversation, kept from core4.v2); execution runs keep
--     project-wide serialization through the replacement index below.

CREATE OR REPLACE FUNCTION assistant.run_lane(
    run_type text, agent_key text, executor_lane text
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT COALESCE(
        NULLIF(executor_lane, ''),
        CASE
            WHEN run_type = 'chat' AND (
                agent_key = 'system-butler'
                OR right(agent_key, 8) = ':manager'
                OR right(agent_key, 11) = ':supervisor'
                OR right(agent_key, 9) = ':operator'
            ) THEN 'conversation'
            ELSE 'execution'
        END
    )
$$;

DROP INDEX IF EXISTS assistant.runs_one_active_project_lane;

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_execution_project
    ON assistant.runs(project_id)
    WHERE status IN ('running','reconciling')
      AND assistant.run_lane(
            run_type, agent_key, executor_json->>'scheduling_lane'
          ) = 'execution';

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core5.v5"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
