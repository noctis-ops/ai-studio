-- Phase 4 media schema (additive).
-- Migration comments must not contain a semicolon because migrate.py splits on it.
-- Money stays INTEGER micro-USD and every *_json column is JSON-validated.

-- The fal queue returns four URLs with the request id. Persist all of them in
-- the same single UPDATE statement as remote_request_id (core/gateway.py).
ALTER TABLE provider_calls ADD COLUMN remote_handle_json TEXT
  CHECK (remote_handle_json IS NULL OR json_valid(remote_handle_json));

-- shot_attempts already has id, shot_id, attempt_no, nonce, prompt_used,
-- rejection_note and created_at from 001_init.sql. Everything else is additive.
ALTER TABLE shot_attempts ADD COLUMN kind TEXT NOT NULL DEFAULT 'keyframe'
  CHECK (kind IN ('keyframe','clip'));
ALTER TABLE shot_attempts ADD COLUMN prompt_spec_json TEXT
  CHECK (prompt_spec_json IS NULL OR json_valid(prompt_spec_json));
ALTER TABLE shot_attempts ADD COLUMN sanitize_level INTEGER NOT NULL DEFAULT 0
  CHECK (sanitize_level >= 0);
ALTER TABLE shot_attempts ADD COLUMN refusal_log_json TEXT NOT NULL DEFAULT '[]'
  CHECK (json_valid(refusal_log_json));
ALTER TABLE shot_attempts ADD COLUMN qc_status TEXT NOT NULL DEFAULT 'pending'
  CHECK (qc_status IN ('pending','pass','warn','fail','failed_policy'));
ALTER TABLE shot_attempts ADD COLUMN qc_json TEXT NOT NULL DEFAULT '[]'
  CHECK (json_valid(qc_json));
ALTER TABLE shot_attempts ADD COLUMN asset_id TEXT REFERENCES assets(sha256);
ALTER TABLE shot_attempts ADD COLUMN conformed_asset_id TEXT REFERENCES assets(sha256);
ALTER TABLE shot_attempts ADD COLUMN cost_musd INTEGER NOT NULL DEFAULT 0
  CHECK (cost_musd >= 0);
ALTER TABLE shot_attempts ADD COLUMN provider_call_id TEXT REFERENCES provider_calls(id);
ALTER TABLE shot_attempts ADD COLUMN policy_blocked INTEGER NOT NULL DEFAULT 0
  CHECK (policy_blocked IN (0,1));

CREATE INDEX ix_shot_attempts_kind ON shot_attempts(shot_id, kind, attempt_no);

-- Approved reference sheets are the visual identity of a character. Only one
-- approved row may exist per (character_id, role) and older ones are superseded.
CREATE TABLE character_sheets(
  id TEXT PRIMARY KEY,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  character_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role <> ''),
  version INTEGER NOT NULL CHECK (version >= 1),
  asset_id TEXT NOT NULL REFERENCES assets(sha256),
  status TEXT NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate','approved','superseded')),
  spec_hash TEXT NOT NULL,
  nonce TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_at TEXT
);
CREATE UNIQUE INDEX ux_character_sheets_approved
  ON character_sheets(character_id, role) WHERE status = 'approved';
CREATE INDEX ix_character_sheets_series ON character_sheets(series_id, character_id, role);
