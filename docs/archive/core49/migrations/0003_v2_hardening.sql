BEGIN;

ALTER TABLE assistant.workflow_versions
    DROP CONSTRAINT IF EXISTS workflow_versions_workflow_id_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS workflow_versions_one_active
    ON assistant.workflow_versions(project_id, workflow_id)
    WHERE project_id IS NOT NULL AND status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS messages_one_reply_per_source
    ON assistant.messages(conversation_id, source_message_id)
    WHERE direction = 'outbound' AND source_message_id IS NOT NULL;

ALTER TABLE assistant.control_jobs
    ADD COLUMN IF NOT EXISTS result_json jsonb;

CREATE TABLE IF NOT EXISTS assistant.model_events (
    model_event_id uuid PRIMARY KEY,
    logical_session_id uuid REFERENCES assistant.logical_sessions(logical_session_id),
    physical_session_record_id uuid REFERENCES assistant.physical_sessions(physical_session_record_id),
    job_id uuid REFERENCES assistant.control_jobs(job_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_trace_uri text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (physical_session_record_id, job_id, ordinal)
);

CREATE TABLE IF NOT EXISTS assistant.tool_events (
    tool_event_id uuid PRIMARY KEY,
    logical_session_id uuid REFERENCES assistant.logical_sessions(logical_session_id),
    physical_session_record_id uuid REFERENCES assistant.physical_sessions(physical_session_record_id),
    job_id uuid REFERENCES assistant.control_jobs(job_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    tool_name text,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_trace_uri text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (physical_session_record_id, job_id, ordinal)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'logical_sessions_current_physical_fk'
          AND conrelid = 'assistant.logical_sessions'::regclass
    ) THEN
        ALTER TABLE assistant.logical_sessions
            ADD CONSTRAINT logical_sessions_current_physical_fk
            FOREIGN KEY (current_physical_session_record_id)
            REFERENCES assistant.physical_sessions(physical_session_record_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'logical_sessions_current_checkpoint_fk'
          AND conrelid = 'assistant.logical_sessions'::regclass
    ) THEN
        ALTER TABLE assistant.logical_sessions
            ADD CONSTRAINT logical_sessions_current_checkpoint_fk
            FOREIGN KEY (current_checkpoint_id)
            REFERENCES assistant.structured_checkpoints(checkpoint_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

INSERT INTO assistant.schema_migrations(version, description)
VALUES ('0003', 'V2 fencing, trace, idempotency and project workflow hardening')
ON CONFLICT (version) DO NOTHING;

COMMIT;
