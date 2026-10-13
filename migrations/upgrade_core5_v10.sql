CREATE TABLE IF NOT EXISTS assistant.service_instances (
    instance_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    unit_id text NOT NULL,
    generation bigint NOT NULL CHECK(generation>=1),
    definition_ref jsonb NOT NULL,
    observed_state text NOT NULL CHECK(observed_state IN ('starting','ready','degraded','stopping','stopped','failed','unknown')),
    cleanup_status text NOT NULL CHECK(cleanup_status IN ('pending','verified','failed','unknown')),
    state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id,unit_id,generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_unclean_service_instance ON assistant.service_instances(project_id,unit_id)
    WHERE cleanup_status<>'verified';
INSERT INTO assistant.kernel_meta(key,value) VALUES ('schema_version','"core5.v10"'::jsonb)
ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();
