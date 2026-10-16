-- Round 3 / S3 (docs/ROUND3_PLAN.md section 6): the shared job and service runtime.
-- core.jobs mirrors each project's published runtime/manifest.yaml; operating state (paused, failures) lives here.

ALTER TABLE core.runs DROP CONSTRAINT runs_run_type_check;
ALTER TABLE core.runs ADD CONSTRAINT runs_run_type_check
    CHECK (run_type IN ('pm_turn', 'supervisor', 'validation', 'publish', 'python', 'bootstrap', 'triage', 'job'));

CREATE TABLE IF NOT EXISTS core.jobs (
    project_id text NOT NULL REFERENCES core.projects,
    job_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('job', 'service')),
    spec jsonb NOT NULL,
    status text NOT NULL DEFAULT 'enabled' CHECK (status IN ('enabled', 'paused', 'retired')),
    next_at timestamptz,
    last_at timestamptz,
    last_status text,
    last_message text,
    failures integer NOT NULL DEFAULT 0,
    manifest_sha text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, job_id)
);
CREATE INDEX IF NOT EXISTS jobs_due ON core.jobs(next_at) WHERE status = 'enabled' AND kind = 'job';
