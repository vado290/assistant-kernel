CREATE TABLE IF NOT EXISTS assistant.project_runtime_units (
    project_id text NOT NULL,
    unit_id text NOT NULL,
    effective_definition_ref jsonb NOT NULL,
    desired_state text NOT NULL DEFAULT 'stopped' CHECK(desired_state IN ('stopped','running','paused','retired')),
    control_revision bigint NOT NULL DEFAULT 0 CHECK(control_revision>=0),
    enable_authorization_ref jsonb,
    resource_policy_ref jsonb,
    control_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    available_at timestamptz,
    last_control_receipt_ref jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,unit_id)
);
INSERT INTO assistant.kernel_meta(key,value) VALUES ('schema_version','"core5.v9"'::jsonb)
ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();
