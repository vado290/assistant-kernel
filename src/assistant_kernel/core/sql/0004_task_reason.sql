-- Round 2 / R1 (docs/ROUND2_PLAN.md): why a task exists, and a baseline for the iteration log.
ALTER TABLE core.tasks ADD COLUMN IF NOT EXISTS reason text;

-- Tasks that ended before the iteration log existed are not written to changes/iterations.md.
INSERT INTO core.task_events(project_id, task_id, kind, payload)
SELECT t.project_id, t.task_id, 'iteration_logged', '{"baseline": true}'::jsonb
FROM core.tasks t
WHERE t.closed_at IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM core.task_events e WHERE e.task_id = t.task_id AND e.kind = 'iteration_logged');

CREATE INDEX IF NOT EXISTS task_events_kind ON core.task_events(kind, event_id);
