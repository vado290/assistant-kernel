-- Rebuilt control core (docs/REBUILD_PLAN.md section 2).
-- Lives in its own schema so it never collides with the legacy assistant.* tables.
CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE core.projects (
    project_id text PRIMARY KEY,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    kind text NOT NULL DEFAULT 'business' CHECK (kind IN ('kernel', 'business')),
    settings jsonb NOT NULL DEFAULT '{}',
    operational_baseline jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.messages (
    message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES core.projects,
    channel text NOT NULL,                  -- discord|api|kernel
    chat_id text,
    user_id text,
    direction text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    role text NOT NULL DEFAULT 'pm' CHECK (role IN ('pm', 'supervisor', 'kernel')),
    body jsonb NOT NULL DEFAULT '{}',       -- {"text": ...} plus channel extras
    task_id uuid,
    consumed_by_run_id uuid,
    interaction_id uuid,
    external_event_id text,
    delivery_status text NOT NULL DEFAULT 'received'
        CHECK (delivery_status IN ('received', 'pending', 'sent', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX messages_external_event ON core.messages(channel, external_event_id)
    WHERE external_event_id IS NOT NULL;
CREATE INDEX messages_task_pending ON core.messages(task_id) WHERE consumed_by_run_id IS NULL;
CREATE INDEX messages_outbox ON core.messages(created_at) WHERE delivery_status = 'pending';

CREATE TABLE core.tasks (
    task_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES core.projects,
    kind text NOT NULL CHECK (kind IN ('delivery', 'operation', 'evolution', 'bootstrap')),
    origin text NOT NULL CHECK (origin IN ('user', 'supervisor', 'schedule', 'handoff', 'evolution', 'system')),
    priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal', 'urgent')),
    goal text NOT NULL,
    source_message_id uuid REFERENCES core.messages,
    parent_task_id uuid REFERENCES core.tasks,
    stage text NOT NULL DEFAULT 'requirements'
        CHECK (stage IN ('requirements', 'design', 'build', 'review', 'release', 'operate')),
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'pm', 'supervising', 'awaiting_user', 'validating', 'publishing',
                         'suspended', 'completed', 'cancelled', 'failed')),
    state_revision integer NOT NULL DEFAULT 0,
    turn_count integer NOT NULL DEFAULT 0,
    max_turns integer NOT NULL DEFAULT 24,
    documents jsonb NOT NULL DEFAULT '{}',
    approvals jsonb NOT NULL DEFAULT '{}',
    pm_session jsonb NOT NULL DEFAULT '{}',
    close_reason text,
    idempotency_key text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz
);
CREATE UNIQUE INDEX tasks_one_active ON core.tasks(project_id)
    WHERE state IN ('pm', 'supervising', 'awaiting_user', 'validating', 'publishing');
CREATE INDEX tasks_queue ON core.tasks(project_id, created_at) WHERE state = 'queued';

CREATE TABLE core.runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES core.projects,
    task_id uuid REFERENCES core.tasks,
    run_type text NOT NULL CHECK (run_type IN ('pm_turn', 'supervisor', 'validation', 'publish', 'python', 'bootstrap')),
    purpose text NOT NULL DEFAULT 'default',   -- supervisor: evaluate|review_input|consult
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'reconciling')),
    attempt integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    available_at timestamptz NOT NULL DEFAULT now(),
    input jsonb NOT NULL DEFAULT '{}',
    base jsonb NOT NULL DEFAULT '{}',          -- revisions observed at enqueue; stale results are discarded
    result jsonb,
    error jsonb,
    effects jsonb NOT NULL DEFAULT '[]',
    mutex_key text,
    worker_id text,
    claim_token uuid,
    lease_until timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    ended_at timestamptz
);
CREATE UNIQUE INDEX runs_one_running_per_mutex ON core.runs(mutex_key)
    WHERE status IN ('running', 'reconciling') AND mutex_key IS NOT NULL;
CREATE UNIQUE INDEX runs_one_running_per_task ON core.runs(task_id)
    WHERE status IN ('running', 'reconciling') AND task_id IS NOT NULL;
CREATE INDEX runs_claimable ON core.runs(available_at, created_at) WHERE status = 'queued';

CREATE TABLE core.interactions (
    interaction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES core.projects,
    revision integer NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    items jsonb NOT NULL DEFAULT '[]',
    drafts jsonb NOT NULL DEFAULT '{}',
    supplements jsonb NOT NULL DEFAULT '{}',
    display jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz
);
CREATE UNIQUE INDEX interactions_one_open ON core.interactions(project_id) WHERE status = 'open';

CREATE TABLE core.task_events (
    event_id bigserial PRIMARY KEY,
    project_id text NOT NULL,
    task_id uuid,
    run_id uuid,
    interaction_id uuid,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX task_events_task ON core.task_events(task_id, event_id);
