-- Round 4 / R4.1 (docs/ROUND4_PLAN.md section 2): every change and its reason, as one stream per project.
-- Artifacts are updated in place; their history lives here (one row per finished task, review, or other change).

CREATE TABLE IF NOT EXISTS core.iterations (
    iteration_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES core.projects,
    task_id uuid REFERENCES core.tasks,
    kind text NOT NULL CHECK (kind IN ('task', 'review', 'bootstrap', 'shared_context', 'grant', 'imported')),
    title text NOT NULL,
    source text NOT NULL DEFAULT 'user',
    source_ref text,
    reason text NOT NULL DEFAULT '',
    changes jsonb NOT NULL DEFAULT '[]',
    artifacts text[] NOT NULL DEFAULT '{}',
    significance text NOT NULL DEFAULT 'minor' CHECK (significance IN ('major', 'minor')),
    verification text NOT NULL DEFAULT '',
    expected_effect text NOT NULL DEFAULT '',
    measure text NOT NULL DEFAULT '',
    followups text NOT NULL DEFAULT '',
    outcome text NOT NULL DEFAULT '',
    facts jsonb NOT NULL DEFAULT '{}',
    complete boolean NOT NULL DEFAULT true,
    effect_status text CHECK (effect_status IN ('effective', 'ineffective', 'pending')),
    effect_note text,
    effect_reviewed_in uuid,
    search_text text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS iterations_one_per_task ON core.iterations(task_id, kind) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS iterations_recent ON core.iterations(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS iterations_artifacts ON core.iterations USING gin (artifacts);

-- Where a task came from in a weekly review (e.g. review:<task8>:P2), so the record and its effect review link up.
ALTER TABLE core.tasks ADD COLUMN IF NOT EXISTS source_ref text;
