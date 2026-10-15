-- Applied transactionally by migrate_core5_v8.py, after source and wait-state checks.
CREATE TABLE assistant.project_file_changes (
  change_id text PRIMARY KEY,
  project_id text NOT NULL,
  batch_id text NOT NULL,
  file_id text,
  target_uri text NOT NULL,
  path text NOT NULL,
  scope_kind text NOT NULL,
  operation text NOT NULL,
  before_sha256 text,
  after_sha256 text,
  occurred_at timestamptz,
  observed_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL,
  reason text NOT NULL,
  cause_status text NOT NULL,
  reason_source_refs jsonb NOT NULL,
  diff_ref jsonb,
  actor jsonb NOT NULL,
  task_ref jsonb,
  run_ref jsonb,
  lines_added bigint,
  lines_deleted bigint,
  file_lines_added bigint,
  file_lines_deleted bigint,
  diff_basis text,
  effect_status text NOT NULL,
  map_applied_generation text,
  -- Immutable complete fact; indexed columns are generated from this same write.
  -- Historical source bytes and paging identity are retained, not re-inferred.
  record_json jsonb NOT NULL,
  legacy_message_id uuid UNIQUE REFERENCES assistant.messages(message_id),
  legacy_message_created_at timestamptz,
  UNIQUE(project_id,batch_id,target_uri,scope_kind),
  CHECK(cause_status IN ('source_backed','unknown')),
  CHECK(before_sha256 IS NULL OR before_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK(after_sha256 IS NULL OR after_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX project_file_changes_project_time
  ON assistant.project_file_changes(project_id,observed_at DESC);
CREATE INDEX project_file_changes_project_path
  ON assistant.project_file_changes(project_id,path,observed_at DESC);
