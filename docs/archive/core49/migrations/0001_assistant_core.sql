CREATE SCHEMA IF NOT EXISTS assistant;
CREATE SCHEMA IF NOT EXISTS assistant_checkpoints;

CREATE TABLE IF NOT EXISTS assistant.projects (
    project_id text PRIMARY KEY,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.workflow_versions (
    workflow_version_id uuid PRIMARY KEY,
    workflow_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    definition_path text NOT NULL,
    definition_sha256 text NOT NULL,
    input_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'draft',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS assistant.tasks (
    task_id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    workflow_version_id uuid REFERENCES assistant.workflow_versions(workflow_version_id),
    status text NOT NULL,
    request_json jsonb NOT NULL,
    result_json jsonb,
    current_graph_thread_id text,
    current_physical_session_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.workflow_runs (
    workflow_run_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES assistant.tasks(task_id),
    workflow_version_id uuid NOT NULL REFERENCES assistant.workflow_versions(workflow_version_id),
    graph_thread_id text NOT NULL,
    status text NOT NULL,
    input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_json jsonb,
    started_at timestamptz,
    ended_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.node_runs (
    node_run_id uuid PRIMARY KEY,
    workflow_run_id uuid NOT NULL REFERENCES assistant.workflow_runs(workflow_run_id),
    node_id text NOT NULL,
    attempt integer NOT NULL CHECK (attempt > 0),
    status text NOT NULL,
    input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_json jsonb,
    error_json jsonb,
    started_at timestamptz,
    ended_at timestamptz,
    UNIQUE (workflow_run_id, node_id, attempt)
);

CREATE TABLE IF NOT EXISTS assistant.workflow_evaluations (
    evaluation_id uuid PRIMARY KEY,
    workflow_run_id uuid NOT NULL REFERENCES assistant.workflow_runs(workflow_run_id),
    evaluator_version text NOT NULL,
    verdict text NOT NULL,
    score double precision,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.agent_sessions (
    session_record_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES assistant.tasks(task_id),
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    physical_session_id text NOT NULL,
    parent_physical_session_id text,
    codex_home_uri text NOT NULL,
    reason text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (physical_session_id)
);

CREATE TABLE IF NOT EXISTS assistant.conversation_events (
    event_id uuid PRIMARY KEY,
    task_id uuid REFERENCES assistant.tasks(task_id),
    project_id text REFERENCES assistant.projects(project_id),
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_events_task_created_idx
    ON assistant.conversation_events(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS assistant.structured_checkpoints (
    checkpoint_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES assistant.tasks(task_id),
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    workflow_version_id uuid REFERENCES assistant.workflow_versions(workflow_version_id),
    graph_thread_id text NOT NULL,
    physical_session_id text,
    state_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.python_executions (
    execution_id uuid PRIMARY KEY,
    task_id uuid REFERENCES assistant.tasks(task_id),
    project_id text REFERENCES assistant.projects(project_id),
    status text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('function', 'script')),
    request_json jsonb NOT NULL,
    source_uri text NOT NULL,
    result_uri text,
    stdout_uri text,
    stderr_uri text,
    worker_id text,
    worker_instance_id uuid,
    lease_until timestamptz,
    process_group_id integer,
    cancel_requested_at timestamptz,
    started_at timestamptz,
    ended_at timestamptz,
    exit_code integer,
    error_type text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS python_executions_claim_idx
    ON assistant.python_executions(status, lease_until, created_at);

CREATE TABLE IF NOT EXISTS assistant.python_execution_events (
    execution_event_id uuid PRIMARY KEY,
    execution_id uuid NOT NULL REFERENCES assistant.python_executions(execution_id),
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS python_execution_events_execution_idx
    ON assistant.python_execution_events(execution_id, created_at);

CREATE TABLE IF NOT EXISTS assistant.idempotency_records (
    idempotency_id uuid PRIMARY KEY,
    scope text NOT NULL,
    idempotency_key text NOT NULL,
    request_sha256 text NOT NULL,
    response_status integer,
    response_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS assistant.outbox_events (
    outbox_event_id uuid PRIMARY KEY,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);
