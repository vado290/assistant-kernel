-- Round 2 / R2 (docs/ROUND2_PLAN.md): triage runs, the quick track, and removal of concepts never used.
-- The constraint changes fail (and the whole migration rolls back) if any row still uses a removed value.

ALTER TABLE core.runs DROP CONSTRAINT runs_run_type_check;
ALTER TABLE core.runs ADD CONSTRAINT runs_run_type_check
    CHECK (run_type IN ('pm_turn', 'supervisor', 'validation', 'publish', 'python', 'bootstrap', 'triage'));

ALTER TABLE core.tasks ADD COLUMN IF NOT EXISTS track text NOT NULL DEFAULT 'full';
ALTER TABLE core.tasks ADD CONSTRAINT tasks_track_check CHECK (track IN ('full', 'quick'));

ALTER TABLE core.tasks DROP CONSTRAINT tasks_state_check;
ALTER TABLE core.tasks ADD CONSTRAINT tasks_state_check
    CHECK (state IN ('queued', 'pm', 'supervising', 'awaiting_user', 'validating', 'publishing', 'bootstrapping',
                     'completed', 'cancelled'));

ALTER TABLE core.tasks DROP CONSTRAINT tasks_kind_check;
ALTER TABLE core.tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN ('delivery', 'evolution', 'bootstrap'));

ALTER TABLE core.tasks DROP CONSTRAINT tasks_origin_check;
ALTER TABLE core.tasks ADD CONSTRAINT tasks_origin_check CHECK (origin IN ('user', 'supervisor', 'schedule', 'evolution'));

DROP INDEX IF EXISTS core.tasks_queue;
ALTER TABLE core.tasks DROP COLUMN IF EXISTS priority;
ALTER TABLE core.tasks DROP COLUMN IF EXISTS max_turns;
CREATE INDEX tasks_queue ON core.tasks(project_id, created_at) WHERE state = 'queued';
