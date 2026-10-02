BEGIN;

ALTER TABLE assistant.control_jobs
    ADD COLUMN IF NOT EXISTS dedupe_key text;
CREATE UNIQUE INDEX IF NOT EXISTS control_jobs_dedupe_unique
    ON assistant.control_jobs(dedupe_key) WHERE dedupe_key IS NOT NULL;

INSERT INTO assistant.schema_migrations(version, description)
VALUES ('0004', 'durable eager physical-session rotation job')
ON CONFLICT (version) DO NOTHING;

COMMIT;
