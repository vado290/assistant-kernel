-- Additive scheduler ledger. This migration never enables a schedule.
CREATE TABLE IF NOT EXISTS assistant.project_schedules (
    project_id text NOT NULL,
    schedule_id text NOT NULL,
    definition_ref jsonb NOT NULL,
    unit_id text NOT NULL,
    state text NOT NULL DEFAULT 'disabled' CHECK(state IN ('disabled','enabled','paused','exhausted','retired')),
    control_revision bigint NOT NULL DEFAULT 0 CHECK(control_revision>=0),
    authorization_refs jsonb NOT NULL DEFAULT '[]',
    next_fire_at timestamptz,
    control_json jsonb NOT NULL DEFAULT '{}',
    last_tick_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,schedule_id),
    FOREIGN KEY(project_id,unit_id) REFERENCES assistant.project_runtime_units(project_id,unit_id)
);
CREATE INDEX IF NOT EXISTS project_schedules_due ON assistant.project_schedules(next_fire_at,project_id,schedule_id) WHERE state='enabled';
CREATE INDEX IF NOT EXISTS project_schedules_unit ON assistant.project_schedules(project_id,unit_id);
CREATE TABLE IF NOT EXISTS assistant.schedule_ticks (
    tick_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    schedule_id text NOT NULL,
    origin text NOT NULL CHECK(origin IN ('scheduled','manual')),
    scheduled_for_utc timestamptz NOT NULL,
    definition_ref jsonb NOT NULL,
    control_revision bigint NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    disposition text NOT NULL CHECK(disposition IN ('submitted','skipped')),
    reason text,
    run_id uuid UNIQUE REFERENCES assistant.runs(run_id),
    control_request jsonb,
    control_receipt_ref jsonb,
    settlement_status text NOT NULL DEFAULT 'pending' CHECK(settlement_status IN ('pending','succeeded','failed','cancelled','skipped')),
    result_ref jsonb,
    notification_id uuid REFERENCES assistant.messages(message_id),
    admitted_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    FOREIGN KEY(project_id,schedule_id) REFERENCES assistant.project_schedules(project_id,schedule_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS schedule_ticks_slot ON assistant.schedule_ticks(project_id,schedule_id,scheduled_for_utc) WHERE origin='scheduled';
CREATE INDEX IF NOT EXISTS schedule_ticks_history ON assistant.schedule_ticks(project_id,schedule_id,scheduled_for_utc DESC,tick_id);
CREATE INDEX IF NOT EXISTS schedule_ticks_unsettled ON assistant.schedule_ticks(admitted_at) WHERE settlement_status='pending';
INSERT INTO assistant.kernel_meta(key,value) VALUES ('schema_version','"core5.v11"'::jsonb)
ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();
