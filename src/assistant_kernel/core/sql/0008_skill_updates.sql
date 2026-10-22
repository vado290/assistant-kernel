-- Skill adoption batches; task stages, approvals and iterations keep their existing owners.
CREATE TABLE core.skill_updates (
    update_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES core.projects,
    release_key text NOT NULL,
    plan_sha text NOT NULL,
    plan jsonb NOT NULL,
    status text NOT NULL DEFAULT 'prepared' CHECK (status IN
        ('prepared','blocked','applying','applied','reconciling','rolling_back','rolled_back','deferred')),
    reason text NOT NULL DEFAULT '',
    receipt jsonb NOT NULL DEFAULT '{}',
    revision integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, release_key)
);
CREATE UNIQUE INDEX skill_updates_one_writer ON core.skill_updates(project_id)
    WHERE status IN ('applying','rolling_back','reconciling');
