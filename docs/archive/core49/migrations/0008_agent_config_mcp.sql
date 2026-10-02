BEGIN;

CREATE TABLE IF NOT EXISTS assistant.agent_scope_versions (
    agent_scope_version_id uuid PRIMARY KEY,
    agent_id text NOT NULL REFERENCES assistant.agents(agent_id),
    version integer NOT NULL CHECK (version > 0),
    parent_scope_version_id uuid REFERENCES assistant.agent_scope_versions(agent_scope_version_id),
    policy jsonb NOT NULL,
    policy_sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('candidate','active','retired')),
    change_reason text NOT NULL DEFAULT '',
    accepted_by text NOT NULL DEFAULT 'bootstrap',
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    retired_at timestamptz,
    UNIQUE (agent_id, version),
    UNIQUE (agent_id, policy_sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_scope_versions_one_active
    ON assistant.agent_scope_versions(agent_id) WHERE status='active';

CREATE TABLE IF NOT EXISTS assistant.agent_config_versions (
    agent_config_version_id uuid PRIMARY KEY,
    agent_id text NOT NULL REFERENCES assistant.agents(agent_id),
    version integer NOT NULL CHECK (version > 0),
    parent_agent_config_version_id uuid REFERENCES assistant.agent_config_versions(agent_config_version_id),
    agent_scope_version_id uuid NOT NULL REFERENCES assistant.agent_scope_versions(agent_scope_version_id),
    config_json jsonb NOT NULL,
    config_uri text NOT NULL,
    config_sha256 text NOT NULL,
    diff_uri text,
    diff_sha256 text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('candidate','active','retired','rejected')),
    change_reason text NOT NULL DEFAULT '',
    accepted_by text NOT NULL DEFAULT 'bootstrap',
    created_at timestamptz NOT NULL DEFAULT now(),
    accepted_at timestamptz,
    activated_at timestamptz,
    retired_at timestamptz,
    UNIQUE (agent_id, version),
    UNIQUE (agent_id, config_sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_config_versions_one_active
    ON assistant.agent_config_versions(agent_id) WHERE status='active';

ALTER TABLE assistant.agents
    ADD COLUMN IF NOT EXISTS active_scope_version_id uuid REFERENCES assistant.agent_scope_versions(agent_scope_version_id),
    ADD COLUMN IF NOT EXISTS active_agent_config_version_id uuid REFERENCES assistant.agent_config_versions(agent_config_version_id);

CREATE TABLE IF NOT EXISTS assistant.mcp_tool_definitions (
    tool_definition_id uuid PRIMARY KEY,
    tool_key text NOT NULL,
    server_key text NOT NULL,
    native_tool_name text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('candidate','active','retired')),
    description text NOT NULL,
    input_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    read_only boolean NOT NULL DEFAULT true,
    risk_class text NOT NULL DEFAULT 'read' CHECK (risk_class IN ('read','write','execute','external')),
    tool_release_id text NOT NULL,
    code_sha256 text NOT NULL,
    config_uri text NOT NULL,
    config_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tool_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS mcp_tool_definitions_one_active
    ON assistant.mcp_tool_definitions(tool_key) WHERE status='active';

CREATE TABLE IF NOT EXISTS assistant.invocation_tool_bindings (
    invocation_id uuid NOT NULL,
    tool_definition_id uuid NOT NULL REFERENCES assistant.mcp_tool_definitions(tool_definition_id),
    binding_mode text NOT NULL CHECK (binding_mode IN ('required','allowed')),
    effective_default_parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    effective_scope_sha256 text NOT NULL,
    selection_source text NOT NULL CHECK (selection_source IN ('system','agent','stage','task')),
    config_snapshot_uri text,
    config_snapshot_sha256 text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (invocation_id, tool_definition_id)
);

CREATE TABLE IF NOT EXISTS assistant.mcp_tool_call_audits (
    tool_call_id uuid PRIMARY KEY,
    invocation_id uuid NOT NULL,
    job_id uuid REFERENCES assistant.control_jobs(job_id),
    agent_id text NOT NULL REFERENCES assistant.agents(agent_id),
    project_id text REFERENCES assistant.projects(project_id),
    tool_key text NOT NULL,
    arguments_sha256 text NOT NULL,
    target_uri text,
    previous_sha256 text,
    result_sha256 text,
    status text NOT NULL CHECK (status IN ('started','succeeded','failed','denied')),
    error_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);
CREATE INDEX IF NOT EXISTS mcp_tool_call_audits_invocation_idx
    ON assistant.mcp_tool_call_audits(invocation_id, created_at);

CREATE INDEX IF NOT EXISTS messages_project_history_idx
    ON assistant.messages(project_id_snapshot, created_at DESC, message_id);

INSERT INTO assistant.schema_migrations(version,description)
VALUES ('0008','Versioned Agent configuration, dynamic scope and per-invocation MCP bindings')
ON CONFLICT (version) DO NOTHING;

COMMIT;
