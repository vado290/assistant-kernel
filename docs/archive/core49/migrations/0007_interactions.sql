BEGIN;
ALTER TABLE assistant.node_runs ADD COLUMN prompt_binding jsonb NOT NULL DEFAULT '{}';

CREATE TABLE assistant.stage_tool_requests (
    request_id uuid PRIMARY KEY,
    node_run_id uuid NOT NULL REFERENCES assistant.node_runs(node_run_id),
    job_id uuid NOT NULL REFERENCES assistant.control_jobs(job_id),
    worker_instance_id uuid NOT NULL,
    turn_key uuid NOT NULL UNIQUE,
    request_json jsonb NOT NULL,
    request_sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE assistant.interaction_questions (
    question_id uuid PRIMARY KEY,
    approval_id uuid NOT NULL REFERENCES assistant.approval_requests(approval_id),
    ordinal integer NOT NULL,
    question_key text NOT NULL,
    question_text text NOT NULL CHECK(length(question_text)>0),
    required boolean NOT NULL DEFAULT true,
    supersedes_question_id uuid REFERENCES assistant.interaction_questions(question_id),
    artifact_version_id uuid REFERENCES assistant.artifact_versions(artifact_version_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(approval_id,ordinal), UNIQUE(approval_id,question_key)
);
CREATE FUNCTION assistant.protect_question_original() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Question originals are immutable; create a replacement question'; END $$;
CREATE TRIGGER immutable_question BEFORE UPDATE OR DELETE ON assistant.interaction_questions
FOR EACH ROW EXECUTE FUNCTION assistant.protect_question_original();

CREATE TABLE assistant.interaction_answers (
    question_id uuid PRIMARY KEY REFERENCES assistant.interaction_questions(question_id),
    answer_json jsonb NOT NULL,
    answered_by text NOT NULL,
    source_json jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER immutable_answer BEFORE UPDATE OR DELETE ON assistant.interaction_answers
FOR EACH ROW EXECUTE FUNCTION assistant.protect_question_original();

CREATE TABLE assistant.channel_deliveries (
    delivery_id uuid PRIMARY KEY,
    outbox_event_id uuid NOT NULL REFERENCES assistant.outbox_events(outbox_event_id),
    channel text NOT NULL, app_id text NOT NULL, chat_id text NOT NULL,
    part integer NOT NULL,
    external_message_id text NOT NULL,
    approval_id uuid REFERENCES assistant.approval_requests(approval_id),
    question_id uuid REFERENCES assistant.interaction_questions(question_id),
    content_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(outbox_event_id,part), UNIQUE(channel,app_id,external_message_id)
);
CREATE TABLE assistant.channel_events (
    channel text NOT NULL, app_id text NOT NULL, event_id text NOT NULL,
    request_sha256 text NOT NULL, payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','handled','needs_clarification','failed')),
    attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now(),
    result_json jsonb, created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(channel,app_id,event_id)
);
CREATE INDEX channel_events_pending ON assistant.channel_events(status,created_at);

INSERT INTO assistant.schema_migrations(version,description)
VALUES ('0007','Immutable question-answer pairs, staged agent requests and channel delivery receipts');
COMMIT;
