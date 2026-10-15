-- Phase 5: a task can wait on a deterministic project bootstrap run.
ALTER TABLE core.tasks DROP CONSTRAINT tasks_state_check;
ALTER TABLE core.tasks ADD CONSTRAINT tasks_state_check
    CHECK (state IN ('queued', 'pm', 'supervising', 'awaiting_user', 'validating', 'publishing', 'bootstrapping',
                     'suspended', 'completed', 'cancelled', 'failed'));
DROP INDEX core.tasks_one_active;
CREATE UNIQUE INDEX tasks_one_active ON core.tasks(project_id)
    WHERE state IN ('pm', 'supervising', 'awaiting_user', 'validating', 'publishing', 'bootstrapping');
