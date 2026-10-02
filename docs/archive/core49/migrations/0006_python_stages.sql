BEGIN;

-- This cutover intentionally refuses in-flight legacy executions. Archive first.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM assistant.control_jobs
        WHERE job_type IN ('graph_start','graph_resume','project_build','project_build_resume')
          AND status IN ('queued','running'))
       OR EXISTS (SELECT 1 FROM assistant.tasks WHERE task_type='project_build'
          AND status IN ('queued','running','waiting_user'))
       OR EXISTS (SELECT 1 FROM assistant.approval_requests WHERE status='pending'
          AND (graph_thread_id IS NOT NULL OR interrupt_id IS NOT NULL))
    THEN RAISE EXCEPTION 'Live legacy work must be resolved before Python stage cutover'; END IF;
END $$;

ALTER TABLE assistant.workflow_runs
    ADD COLUMN current_stage text,
    ADD COLUMN current_stage_attempt integer NOT NULL DEFAULT 0;
ALTER TABLE assistant.node_runs ADD COLUMN job_id uuid UNIQUE REFERENCES assistant.control_jobs(job_id);
ALTER TABLE assistant.approval_requests ADD COLUMN node_run_id uuid REFERENCES assistant.node_runs(node_run_id);
CREATE UNIQUE INDEX approval_per_stage_run ON assistant.approval_requests(node_run_id) WHERE node_run_id IS NOT NULL;
ALTER TABLE assistant.approval_decisions RENAME COLUMN resume_value TO answer_json;
ALTER TABLE assistant.approval_decisions RENAME COLUMN resume_job_id TO next_job_id;
ALTER TABLE assistant.approval_requests RENAME COLUMN resume_enqueued_at TO next_job_enqueued_at;

UPDATE assistant.outbox_events e SET status='cancelled'
WHERE e.event_type='approval.requested' AND e.status IN ('pending','delivering')
  AND NOT EXISTS (SELECT 1 FROM assistant.approval_requests a WHERE a.status='pending'
      AND (a.approval_id::text=e.aggregate_id OR a.approval_id::text=e.payload->>'approval_id'));
UPDATE assistant.workflow_versions SET status='retired',retired_at=COALESCE(retired_at,now())
WHERE spec_json->'executor'->>'type'='langgraph_factory';
DELETE FROM assistant.workflow_current_versions c USING assistant.workflow_versions v
WHERE c.workflow_version_id=v.workflow_version_id AND v.spec_json->'executor'->>'type'='langgraph_factory';

DROP INDEX IF EXISTS assistant.workflow_runs_checkpoint_identity;
DROP INDEX IF EXISTS assistant.approvals_graph_wait_identity;
ALTER TABLE assistant.workflow_runs DROP COLUMN checkpoint_ns, DROP COLUMN checkpoint_id,
    DROP COLUMN checkpoint_token, DROP COLUMN pending_interrupts, DROP COLUMN graph_thread_id;
ALTER TABLE assistant.approval_requests DROP COLUMN interrupt_id, DROP COLUMN checkpoint_id,
    DROP COLUMN checkpoint_token, DROP COLUMN graph_thread_id;
ALTER TABLE assistant.tasks DROP COLUMN current_graph_thread_id;
ALTER TABLE assistant.structured_checkpoints DROP COLUMN graph_thread_id;

-- Exact retired tables only; no CASCADE, so unexpected dependencies stop the migration.
DROP TABLE IF EXISTS assistant_checkpoints.checkpoint_writes;
DROP TABLE IF EXISTS assistant_checkpoints.checkpoint_blobs;
DROP TABLE IF EXISTS assistant_checkpoints.checkpoints;
DROP TABLE IF EXISTS assistant_checkpoints.checkpoint_migrations;
DROP SCHEMA IF EXISTS assistant_checkpoints;

INSERT INTO assistant.schema_migrations(version,description)
VALUES ('0006','Replace graph continuation with explicit Python stage jobs and business approvals');
COMMIT;
