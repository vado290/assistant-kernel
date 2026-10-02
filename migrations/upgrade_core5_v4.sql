BEGIN;

ALTER TABLE assistant.runs
    ADD COLUMN IF NOT EXISTS harness text,
    ADD COLUMN IF NOT EXISTS provider text,
    ADD COLUMN IF NOT EXISTS model text,
    ADD COLUMN IF NOT EXISTS reasoning_effort text,
    ADD COLUMN IF NOT EXISTS auth_mode text;

ALTER TABLE assistant.sessions
    ADD COLUMN IF NOT EXISTS harness text,
    ADD COLUMN IF NOT EXISTS provider text,
    ADD COLUMN IF NOT EXISTS model text,
    ADD COLUMN IF NOT EXISTS reasoning_effort text,
    ADD COLUMN IF NOT EXISTS auth_mode text;

UPDATE assistant.runs
SET harness=COALESCE(harness,executor_json->>'harness'),
    provider=COALESCE(provider,executor_json->>'provider'),
    model=COALESCE(model,executor_json->>'model'),
    reasoning_effort=COALESCE(reasoning_effort,executor_json->>'reasoning_effort')
WHERE executor_json <> '{}'::jsonb;

WITH latest AS (
    SELECT DISTINCT ON (session_id)
        session_id,harness,provider,model,reasoning_effort,auth_mode
    FROM assistant.runs
    WHERE session_id IS NOT NULL
    ORDER BY session_id,updated_at DESC
)
UPDATE assistant.sessions s
SET harness=COALESCE(s.harness,r.harness),
    provider=COALESCE(s.provider,r.provider),
    model=COALESCE(s.model,r.model),
    reasoning_effort=COALESCE(s.reasoning_effort,r.reasoning_effort),
    auth_mode=COALESCE(s.auth_mode,r.auth_mode)
FROM latest r
WHERE r.session_id=s.session_id
  AND (s.harness IS NULL OR s.provider IS NULL OR s.model IS NULL);

CREATE TABLE IF NOT EXISTS assistant.model_invocations (
    invocation_id text PRIMARY KEY,
    project_id text NOT NULL,
    run_id uuid REFERENCES assistant.runs(run_id) ON DELETE SET NULL,
    session_id uuid REFERENCES assistant.sessions(session_id) ON DELETE SET NULL,
    actor_role text NOT NULL,
    harness text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    reasoning_effort text,
    auth_mode text,
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled')),
    native_session_id text,
    input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_input_tokens bigint NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens bigint NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    trace_uri text,
    trace_sha256 text,
    error_type text,
    error_message text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS model_invocations_run_created
    ON assistant.model_invocations(run_id,started_at DESC);
CREATE INDEX IF NOT EXISTS model_invocations_project_actor_created
    ON assistant.model_invocations(project_id,actor_role,started_at DESC);
CREATE INDEX IF NOT EXISTS model_invocations_provider_model_created
    ON assistant.model_invocations(provider,model,started_at DESC);

INSERT INTO assistant.kernel_meta(key,value)
VALUES ('schema_version','"core5.v4"'::jsonb)
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

COMMIT;
