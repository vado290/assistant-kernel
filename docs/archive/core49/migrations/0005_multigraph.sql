BEGIN;

-- Reuse workflow versions/runs; do not create a second business-state owner.
ALTER TABLE assistant.workflow_runs
    ADD COLUMN IF NOT EXISTS checkpoint_ns text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS checkpoint_id text,
    ADD COLUMN IF NOT EXISTS checkpoint_token text,
    ADD COLUMN IF NOT EXISTS pending_interrupts jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS last_job_id uuid,
    ADD COLUMN IF NOT EXISTS last_job_result jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS workflow_runs_checkpoint_identity
    ON assistant.workflow_runs(graph_thread_id, checkpoint_ns);

ALTER TABLE assistant.approval_requests
    ADD COLUMN IF NOT EXISTS interrupt_id text,
    ADD COLUMN IF NOT EXISTS checkpoint_id text,
    ADD COLUMN IF NOT EXISTS checkpoint_token text;
CREATE UNIQUE INDEX IF NOT EXISTS approvals_graph_wait_identity
    ON assistant.approval_requests(workflow_run_id, checkpoint_token, interrupt_id)
    WHERE interrupt_id IS NOT NULL;

ALTER TABLE assistant.approval_decisions
    ADD COLUMN IF NOT EXISTS resume_value jsonb,
    ADD COLUMN IF NOT EXISTS resume_job_id uuid REFERENCES assistant.control_jobs(job_id);

INSERT INTO assistant.schema_migrations(version, description)
VALUES ('0005', 'Version-pinned multi-graph runtime, approval generations and recovery receipts')
ON CONFLICT (version) DO NOTHING;
COMMIT;
