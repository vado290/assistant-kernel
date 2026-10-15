BEGIN;

CREATE SCHEMA IF NOT EXISTS assistant;

CREATE TABLE IF NOT EXISTS assistant.kernel_meta (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.sessions (
    session_id uuid PRIMARY KEY,
    logical_session_id uuid NOT NULL,
    project_id text NOT NULL,
    agent_key text NOT NULL,
    channel text NOT NULL,
    chat_id text NOT NULL,
    native_session_id text,
    parent_session_id uuid REFERENCES assistant.sessions(session_id),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','rotation_required','sealed','failed')),
    input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    context_window_tokens bigint,
    usage_known boolean NOT NULL DEFAULT false,
    usage_source text,
    last_invocation_id text,
    handoff_checkpoint_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    handoff_checkpoint_sha256 text,
    handoff_cutoff_message_id uuid,
    handoff_created_at timestamptz,
    archive_uri text,
    archive_sha256 text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_native_unique
    ON assistant.sessions(native_session_id) WHERE native_session_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_current
    ON assistant.sessions(project_id,agent_key,channel,chat_id)
    WHERE status IN ('active','rotation_required');
CREATE INDEX IF NOT EXISTS sessions_logical_history_idx
    ON assistant.sessions(project_id,logical_session_id,created_at DESC);

CREATE TABLE IF NOT EXISTS assistant.messages (
    message_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    session_id uuid REFERENCES assistant.sessions(session_id),
    run_id uuid,
    channel text NOT NULL,
    chat_id text NOT NULL,
    user_id text,
    direction text NOT NULL CHECK (direction IN ('inbound','outbound','system')),
    message_type text NOT NULL DEFAULT 'text',
    external_event_id text,
    external_message_id text,
    reply_to_message_id uuid REFERENCES assistant.messages(message_id),
    content_json jsonb NOT NULL,
    delivery_status text NOT NULL DEFAULT 'received'
        CHECK (delivery_status IN ('received','pending','delivering','sent','failed','cancelled')),
    retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    max_retries integer NOT NULL DEFAULT 10 CHECK (max_retries > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    worker_id text,
    worker_instance_id uuid,
    lease_until timestamptz,
    idempotency_key text,
    error_text text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_external_event_unique
    ON assistant.messages(channel,external_event_id)
    WHERE external_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS messages_idempotency_unique
    ON assistant.messages(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS messages_one_outbound_reply
    ON assistant.messages(reply_to_message_id)
    WHERE direction='outbound' AND message_type='reply' AND reply_to_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_history_idx
    ON assistant.messages(project_id,channel,chat_id,created_at,message_id);
CREATE INDEX IF NOT EXISTS messages_route_binding_lookup_idx
    ON assistant.messages(channel,chat_id,user_id,created_at DESC,message_id DESC)
    WHERE direction='system' AND message_type='route_binding';
CREATE INDEX IF NOT EXISTS messages_delivery_claim_idx
    ON assistant.messages(delivery_status,available_at,created_at)
    WHERE direction='outbound';

CREATE TABLE IF NOT EXISTS assistant.runs (
    run_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    agent_key text NOT NULL,
    conversation_key text NOT NULL,
    session_id uuid REFERENCES assistant.sessions(session_id),
    trigger_message_id uuid REFERENCES assistant.messages(message_id),
    parent_run_id uuid REFERENCES assistant.runs(run_id),
    run_type text NOT NULL DEFAULT 'chat'
        CHECK (run_type IN ('chat','stage','python','publish')),
    stage text,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','waiting_user','waiting_supervisor','waiting_children','blocked','reconciling','succeeded','failed','cancelled','dead_letter')),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    interaction_seq integer NOT NULL DEFAULT 0 CHECK (interaction_seq >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    request_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_json jsonb,
    executor_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    artifact_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    waiting_question_message_id uuid REFERENCES assistant.messages(message_id),
    worker_id text,
    worker_instance_id uuid,
    claim_token uuid,
    lease_until timestamptz,
    process_pid integer,
    process_start_ticks bigint,
    cancel_requested_at timestamptz,
    idempotency_key text,
    error_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    ended_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency_unique
    ON assistant.runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS runs_claim_idx
    ON assistant.runs(status,available_at,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_conversation
    ON assistant.runs(conversation_key)
    WHERE status IN ('running','reconciling');

ALTER TABLE assistant.messages
    ADD CONSTRAINT messages_run_fk FOREIGN KEY (run_id)
    REFERENCES assistant.runs(run_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE assistant.sessions
    ADD CONSTRAINT sessions_handoff_cutoff_message_fk
    FOREIGN KEY (handoff_cutoff_message_id)
    REFERENCES assistant.messages(message_id) DEFERRABLE INITIALLY DEFERRED;

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core4.v5"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
