BEGIN;

CREATE TABLE IF NOT EXISTS assistant.schema_migrations (
    version text PRIMARY KEY,
    description text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.agents (
    agent_id text PRIMARY KEY,
    project_id text REFERENCES assistant.projects(project_id),
    agent_type text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    default_harness text NOT NULL DEFAULT 'codex_cli',
    default_provider text,
    default_model text,
    tool_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agents_project_type_unique
    ON assistant.agents(project_id, agent_type)
    WHERE project_id IS NOT NULL AND status = 'active';

CREATE TABLE IF NOT EXISTS assistant.roles (
    role_id uuid PRIMARY KEY,
    agent_id text NOT NULL REFERENCES assistant.agents(agent_id),
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, name)
);

CREATE TABLE IF NOT EXISTS assistant.role_versions (
    role_version_id uuid PRIMARY KEY,
    role_id uuid NOT NULL REFERENCES assistant.roles(role_id),
    version integer NOT NULL CHECK (version > 0),
    content text NOT NULL,
    content_sha256 text NOT NULL,
    token_count integer NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    status text NOT NULL DEFAULT 'active',
    parent_role_version_id uuid REFERENCES assistant.role_versions(role_version_id),
    diff_uri text,
    change_reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (role_id, version),
    UNIQUE (role_id, content_sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS role_versions_one_active
    ON assistant.role_versions(role_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS assistant.project_context_entries (
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    entry_id text NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    entry_type text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    scope text NOT NULL DEFAULT 'project',
    statement text NOT NULL,
    rationale text NOT NULL DEFAULT '',
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    supersedes_entry_id text,
    content_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, entry_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS project_context_one_active
    ON assistant.project_context_entries(project_id, entry_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS assistant.channel_bindings (
    binding_id uuid PRIMARY KEY,
    frontend text NOT NULL,
    user_id text NOT NULL,
    channel_conversation_id text NOT NULL,
    current_project_id text REFERENCES assistant.projects(project_id),
    current_agent_id text REFERENCES assistant.agents(agent_id),
    binding_version bigint NOT NULL DEFAULT 1 CHECK (binding_version > 0),
    status text NOT NULL DEFAULT 'active',
    confirmed_by text,
    confirmed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (frontend, user_id, channel_conversation_id)
);

CREATE TABLE IF NOT EXISTS assistant.conversations (
    conversation_id uuid PRIMARY KEY,
    frontend text NOT NULL,
    user_id text NOT NULL,
    channel_conversation_id text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    next_message_sequence bigint NOT NULL DEFAULT 1,
    current_binding_version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (frontend, user_id, channel_conversation_id)
);

CREATE TABLE IF NOT EXISTS assistant.messages (
    message_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES assistant.conversations(conversation_id),
    sequence_no bigint NOT NULL CHECK (sequence_no > 0),
    direction text NOT NULL CHECK (direction IN ('inbound', 'outbound', 'system')),
    external_message_id text,
    source_message_id uuid REFERENCES assistant.messages(message_id),
    binding_version bigint NOT NULL,
    project_id_snapshot text REFERENCES assistant.projects(project_id),
    agent_id_snapshot text REFERENCES assistant.agents(agent_id),
    content_type text NOT NULL DEFAULT 'text',
    content jsonb NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, sequence_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_external_inbound_unique
    ON assistant.messages(conversation_id, external_message_id)
    WHERE direction = 'inbound' AND external_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS assistant.control_jobs (
    job_id uuid PRIMARY KEY,
    job_type text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    conversation_id uuid REFERENCES assistant.conversations(conversation_id),
    message_id uuid REFERENCES assistant.messages(message_id),
    task_id uuid REFERENCES assistant.tasks(task_id),
    workflow_run_id uuid REFERENCES assistant.workflow_runs(workflow_run_id),
    sequence_no bigint,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority integer NOT NULL DEFAULT 100,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    worker_id text,
    worker_instance_id uuid,
    lease_until timestamptz,
    cancel_requested_at timestamptz,
    error_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    ended_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS control_jobs_claim_idx
    ON assistant.control_jobs(status, available_at, priority, created_at);
CREATE INDEX IF NOT EXISTS control_jobs_conversation_fifo_idx
    ON assistant.control_jobs(conversation_id, sequence_no, status);

CREATE TABLE IF NOT EXISTS assistant.logical_sessions (
    logical_session_id uuid PRIMARY KEY,
    project_id text REFERENCES assistant.projects(project_id),
    agent_id text NOT NULL REFERENCES assistant.agents(agent_id),
    scope_type text NOT NULL,
    scope_id text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    current_physical_session_record_id uuid,
    current_checkpoint_id uuid,
    lock_version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS assistant.physical_sessions (
    physical_session_record_id uuid PRIMARY KEY,
    logical_session_id uuid NOT NULL REFERENCES assistant.logical_sessions(logical_session_id),
    parent_physical_session_record_id uuid REFERENCES assistant.physical_sessions(physical_session_record_id),
    harness text NOT NULL,
    provider text,
    model text,
    native_session_id text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    role_version_id uuid REFERENCES assistant.role_versions(role_version_id),
    prompt_manifest_id uuid,
    active_input_tokens bigint NOT NULL DEFAULT 0,
    context_window_tokens bigint,
    context_ratio double precision,
    archive_uri text,
    archive_sha256 text,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    UNIQUE (harness, native_session_id)
);
CREATE INDEX IF NOT EXISTS physical_sessions_logical_idx
    ON assistant.physical_sessions(logical_session_id, started_at DESC);

CREATE TABLE IF NOT EXISTS assistant.prompt_manifests (
    prompt_manifest_id uuid PRIMARY KEY,
    logical_session_id uuid NOT NULL REFERENCES assistant.logical_sessions(logical_session_id),
    physical_session_record_id uuid REFERENCES assistant.physical_sessions(physical_session_record_id),
    role_version_id uuid REFERENCES assistant.role_versions(role_version_id),
    checkpoint_id uuid,
    message_id uuid REFERENCES assistant.messages(message_id),
    manifest jsonb NOT NULL,
    manifest_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE assistant.structured_checkpoints
    ALTER COLUMN task_id DROP NOT NULL,
    ALTER COLUMN project_id DROP NOT NULL,
    ALTER COLUMN graph_thread_id DROP NOT NULL;
ALTER TABLE assistant.structured_checkpoints
    ADD COLUMN IF NOT EXISTS schema_version text NOT NULL DEFAULT '2.0',
    ADD COLUMN IF NOT EXISTS logical_session_id uuid REFERENCES assistant.logical_sessions(logical_session_id),
    ADD COLUMN IF NOT EXISTS physical_session_record_id uuid REFERENCES assistant.physical_sessions(physical_session_record_id),
    ADD COLUMN IF NOT EXISTS parent_checkpoint_id uuid REFERENCES assistant.structured_checkpoints(checkpoint_id),
    ADD COLUMN IF NOT EXISTS checkpoint_kind text NOT NULL DEFAULT 'workflow',
    ADD COLUMN IF NOT EXISTS content_sha256 text,
    ADD COLUMN IF NOT EXISTS validation_status text NOT NULL DEFAULT 'valid';

CREATE TABLE IF NOT EXISTS assistant.model_execution_profiles (
    model_execution_profile_id uuid PRIMARY KEY,
    project_id text REFERENCES assistant.projects(project_id),
    task_id uuid REFERENCES assistant.tasks(task_id),
    stage_id text,
    harness text NOT NULL,
    provider text,
    model text,
    reasoning_effort text,
    max_output_tokens integer,
    timeout_seconds integer,
    sandbox_policy text NOT NULL,
    approval_policy text NOT NULL,
    context_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant.approval_requests (
    approval_id uuid PRIMARY KEY,
    project_id text REFERENCES assistant.projects(project_id),
    task_id uuid REFERENCES assistant.tasks(task_id),
    workflow_run_id uuid REFERENCES assistant.workflow_runs(workflow_run_id),
    graph_thread_id text,
    approval_type text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    payload jsonb NOT NULL,
    required_user_id text,
    expires_at timestamptz,
    decided_at timestamptz,
    resume_enqueued_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approval_requests_pending_idx
    ON assistant.approval_requests(status, created_at);

CREATE TABLE IF NOT EXISTS assistant.approval_decisions (
    approval_decision_id uuid PRIMARY KEY,
    approval_id uuid NOT NULL UNIQUE REFERENCES assistant.approval_requests(approval_id),
    decision text NOT NULL,
    edited_payload jsonb,
    decided_by text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE assistant.workflow_versions
    ADD COLUMN IF NOT EXISTS project_id text REFERENCES assistant.projects(project_id),
    ADD COLUMN IF NOT EXISTS spec_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS parent_workflow_version_id uuid REFERENCES assistant.workflow_versions(workflow_version_id),
    ADD COLUMN IF NOT EXISTS tests_uri text,
    ADD COLUMN IF NOT EXISTS review_uri text,
    ADD COLUMN IF NOT EXISTS activated_at timestamptz,
    ADD COLUMN IF NOT EXISTS retired_at timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS workflow_versions_project_version_unique
    ON assistant.workflow_versions(project_id, workflow_id, version)
    WHERE project_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS assistant.workflow_current_versions (
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    workflow_id text NOT NULL,
    workflow_version_id uuid NOT NULL REFERENCES assistant.workflow_versions(workflow_version_id),
    pointer_version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, workflow_id)
);

ALTER TABLE assistant.tasks
    ADD COLUMN IF NOT EXISTS parent_task_id uuid REFERENCES assistant.tasks(task_id),
    ADD COLUMN IF NOT EXISTS task_type text NOT NULL DEFAULT 'generic',
    ADD COLUMN IF NOT EXISTS status_version bigint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS acceptance_json jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE assistant.workflow_runs
    ADD COLUMN IF NOT EXISTS status_version bigint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS error_json jsonb,
    ADD COLUMN IF NOT EXISTS executor_ref text;

CREATE TABLE IF NOT EXISTS assistant.artifact_versions (
    artifact_version_id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    artifact_id text NOT NULL,
    task_id uuid REFERENCES assistant.tasks(task_id),
    stage_id text,
    artifact_type text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL DEFAULT 'candidate',
    storage_uri text NOT NULL,
    content_sha256 text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    parent_artifact_version_id uuid REFERENCES assistant.artifact_versions(artifact_version_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, artifact_id, version)
);

CREATE TABLE IF NOT EXISTS assistant.artifact_links (
    artifact_link_id uuid PRIMARY KEY,
    from_artifact_version_id uuid NOT NULL REFERENCES assistant.artifact_versions(artifact_version_id),
    to_artifact_version_id uuid NOT NULL REFERENCES assistant.artifact_versions(artifact_version_id),
    link_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_artifact_version_id, to_artifact_version_id, link_type)
);

CREATE TABLE IF NOT EXISTS assistant.change_requests (
    change_request_id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    task_id uuid REFERENCES assistant.tasks(task_id),
    target_type text NOT NULL,
    target_id text NOT NULL,
    impact text NOT NULL,
    confidence double precision,
    status text NOT NULL DEFAULT 'proposed',
    proposal jsonb NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    evaluator_profile_id uuid REFERENCES assistant.model_execution_profiles(model_execution_profile_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS assistant.source_records (
    source_id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    source_type text NOT NULL,
    storage_uri text NOT NULL,
    content_sha256 text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, storage_uri, content_sha256)
);

CREATE TABLE IF NOT EXISTS assistant.claim_records (
    claim_id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    claim_key text NOT NULL,
    statement text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    confidence double precision,
    supersedes_claim_id uuid REFERENCES assistant.claim_records(claim_id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claim_records_project_key_idx
    ON assistant.claim_records(project_id, claim_key, status);

CREATE TABLE IF NOT EXISTS assistant.claim_sources (
    claim_id uuid NOT NULL REFERENCES assistant.claim_records(claim_id),
    source_id uuid NOT NULL REFERENCES assistant.source_records(source_id),
    relation text NOT NULL DEFAULT 'supports',
    evidence_locator text,
    PRIMARY KEY (claim_id, source_id, relation)
);

CREATE TABLE IF NOT EXISTS assistant.claim_conflicts (
    conflict_id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES assistant.projects(project_id),
    left_claim_id uuid NOT NULL REFERENCES assistant.claim_records(claim_id),
    right_claim_id uuid NOT NULL REFERENCES assistant.claim_records(claim_id),
    status text NOT NULL DEFAULT 'open',
    resolution jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS assistant.control_events (
    control_event_id uuid PRIMARY KEY,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS control_events_aggregate_idx
    ON assistant.control_events(aggregate_type, aggregate_id, created_at);

ALTER TABLE assistant.outbox_events
    ADD COLUMN IF NOT EXISTS dedupe_key text,
    ADD COLUMN IF NOT EXISTS worker_id text,
    ADD COLUMN IF NOT EXISTS worker_instance_id uuid,
    ADD COLUMN IF NOT EXISTS lease_until timestamptz,
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 10,
    ADD COLUMN IF NOT EXISTS last_error text,
    ADD COLUMN IF NOT EXISTS acknowledged_by text,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE UNIQUE INDEX IF NOT EXISTS outbox_events_dedupe_unique
    ON assistant.outbox_events(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS outbox_events_claim_idx
    ON assistant.outbox_events(status, next_attempt_at, created_at);

INSERT INTO assistant.agents(agent_id, agent_type, display_name, tool_policy)
VALUES ('system-butler', 'butler', 'System Butler', '{"sandbox":"read-only"}'::jsonb)
ON CONFLICT (agent_id) DO NOTHING;

INSERT INTO assistant.schema_migrations(version, description)
VALUES ('0002', 'V2 durable control plane')
ON CONFLICT (version) DO NOTHING;

COMMIT;
