-- Phase 1 foundation schema (Blueprint v1.0 §17 + Amendments C1-C9).
-- Money is INTEGER micro-USD. *_json columns carry CHECK(json_valid(...)).
-- Tables in dependency order: referenced tables first.

CREATE TABLE series(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE characters(
  id TEXT PRIMARY KEY,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role <> ''),
  name TEXT NOT NULL,
  voices_json TEXT NOT NULL CHECK (json_valid(voices_json)),
  created_at TEXT NOT NULL
);
CREATE INDEX ix_characters_series ON characters(series_id);

CREATE TABLE episodes(
  id TEXT PRIMARY KEY,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  concept TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','awaiting_approval','halted_budget','completed','failed')),
  languages_json TEXT NOT NULL CHECK (json_valid(languages_json)),
  aspects_json TEXT NOT NULL CHECK (json_valid(aspects_json)),
  created_at TEXT NOT NULL
);
CREATE INDEX ix_episodes_series ON episodes(series_id);

CREATE TABLE tracks(
  id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  aspect TEXT NOT NULL CHECK (aspect IN ('16:9','9:16')),
  created_at TEXT NOT NULL,
  UNIQUE (episode_id, aspect)
);

CREATE TABLE deliverables(
  id TEXT PRIMARY KEY,
  track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  language TEXT NOT NULL CHECK (language IN ('ar','en')),
  channel_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (track_id, language)
);

CREATE TABLE shots(
  id TEXT PRIMARY KEY,
  track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  shot_no INTEGER NOT NULL CHECK (shot_no >= 1),
  prompt TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (track_id, shot_no)
);

CREATE TABLE assets(
  sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
  rel_path TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  mime TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  width INTEGER CHECK (width IS NULL OR width > 0),
  height INTEGER CHECK (height IS NULL OR height > 0),
  retention TEXT NOT NULL DEFAULT 'intermediate',
  created_at TEXT NOT NULL,
  purged_at TEXT
);
CREATE INDEX ix_assets_retention ON assets(retention);

CREATE TABLE narration_lines(
  id TEXT PRIMARY KEY,
  deliverable_id TEXT NOT NULL REFERENCES deliverables(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  line_no INTEGER NOT NULL CHECK (line_no >= 1),
  text TEXT NOT NULL,
  asset_sha256 TEXT REFERENCES assets(sha256) ON DELETE SET NULL,
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  created_at TEXT NOT NULL,
  UNIQUE (deliverable_id, shot_id)
);
CREATE INDEX ix_narration_shot ON narration_lines(shot_id);

CREATE TABLE shot_attempts(
  id TEXT PRIMARY KEY,
  shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
  nonce TEXT NOT NULL,
  prompt_used TEXT NOT NULL,
  rejection_note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (shot_id, attempt_no)
);

CREATE TABLE budgets(
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('episode','month','global')),
  scope_ref TEXT NOT NULL,
  cap_musd INTEGER CHECK (cap_musd IS NULL OR cap_musd >= 0),
  kill_switch_tripped INTEGER NOT NULL DEFAULT 0 CHECK (kill_switch_tripped IN (0,1)),
  tripped_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (scope, scope_ref)
);

CREATE TABLE pipeline_runs(
  id TEXT PRIMARY KEY,
  episode_id TEXT REFERENCES episodes(id) ON DELETE CASCADE,
  profile TEXT NOT NULL CHECK (profile IN ('sandbox','draft','final')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','awaiting_approval','halted_budget','completed','failed')),
  auto_approve INTEGER NOT NULL DEFAULT 0 CHECK (auto_approve IN (0,1)),
  config_snapshot_json TEXT CHECK (config_snapshot_json IS NULL OR json_valid(config_snapshot_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX ix_pipeline_runs_episode ON pipeline_runs(episode_id);

CREATE TABLE stage_runs(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  subject_scope TEXT NOT NULL CHECK (subject_scope IN ('series','episode','track','deliverable','shot','line')),
  subject_id TEXT NOT NULL,
  lineage_json TEXT NOT NULL CHECK (json_valid(lineage_json)),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','succeeded','failed','stale','skipped')),
  input_hash TEXT,
  output_json TEXT CHECK (output_json IS NULL OR json_valid(output_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (run_id, stage, subject_scope, subject_id)
);
CREATE INDEX ix_stage_runs_run ON stage_runs(run_id);
CREATE INDEX ix_stage_runs_lookup ON stage_runs(run_id, stage, status);

CREATE TABLE provider_calls(
  id TEXT PRIMARY KEY,
  episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
  stage_run_id TEXT REFERENCES stage_runs(id) ON DELETE SET NULL,
  capability TEXT NOT NULL,
  model_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  cache_key TEXT NOT NULL,
  request_json TEXT CHECK (request_json IS NULL OR json_valid(request_json)),
  status TEXT NOT NULL CHECK (status IN ('reserved','submitted','completed','failed')),
  attempt INTEGER NOT NULL DEFAULT 0,
  est_cost_musd INTEGER NOT NULL CHECK (est_cost_musd >= 0),
  actual_cost_musd INTEGER CHECK (actual_cost_musd IS NULL OR actual_cost_musd >= 0),
  remote_request_id TEXT,
  submit_started_at TEXT,
  error_class TEXT CHECK (error_class IS NULL OR error_class IN ('transient','policy','permanent')),
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX ix_provider_calls_cache ON provider_calls(cache_key, status);
CREATE INDEX ix_provider_calls_stage_run ON provider_calls(stage_run_id);

CREATE TABLE cost_ledger(
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('episode','month','global')),
  scope_ref TEXT NOT NULL,
  entry_type TEXT NOT NULL CHECK (entry_type IN ('reserve','release','settle','adjust')),
  amount_musd INTEGER NOT NULL,
  call_id TEXT REFERENCES provider_calls(id) ON DELETE SET NULL
);
CREATE INDEX ix_cost_ledger_scope ON cost_ledger(scope, scope_ref);
CREATE INDEX ix_cost_ledger_call ON cost_ledger(call_id);

CREATE TABLE cache_entries(
  cache_key TEXT PRIMARY KEY,
  capability TEXT NOT NULL,
  model_key TEXT NOT NULL,
  assets_json TEXT NOT NULL CHECK (json_valid(assets_json)),
  inline_json TEXT CHECK (inline_json IS NULL OR json_valid(inline_json)),
  cost_musd INTEGER NOT NULL CHECK (cost_musd >= 0),
  created_at TEXT NOT NULL
);

CREATE TABLE jobs(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  dedupe_key TEXT,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  run_after TEXT NOT NULL,
  worker_id TEXT,
  heartbeat_at TEXT,
  stage_run_id TEXT REFERENCES stage_runs(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_jobs_dedupe ON jobs(dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('queued','running');
CREATE INDEX ix_jobs_claim ON jobs(status, run_after, created_at, id);

CREATE TABLE approval_gates(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  gate TEXT NOT NULL CHECK (gate IN ('G0_CHARACTERS','G1_SCRIPT','G2_KEYFRAMES','G3_FINAL_CUT')),
  stage TEXT NOT NULL,
  scope TEXT CHECK (scope IS NULL OR scope IN ('series','episode','track','deliverable','shot','line')),
  subject_id TEXT,
  round INTEGER NOT NULL DEFAULT 1 CHECK (round >= 1),
  status TEXT NOT NULL CHECK (status IN ('pending','approved','changes_requested','auto_approved')),
  snapshot_hash TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX ix_gates_run ON approval_gates(run_id, gate, status);
CREATE INDEX ix_gates_stage ON approval_gates(run_id, stage, round);

CREATE TABLE audit_events(
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  entity TEXT NOT NULL,
  event TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
CREATE INDEX ix_audit_event ON audit_events(event);
CREATE INDEX ix_audit_ts ON audit_events(ts);
