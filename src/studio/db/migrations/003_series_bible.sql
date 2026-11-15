-- Phase 2: series bible (content-addressed) + deliverable metadata/safety.
-- Money stays INTEGER micro-USD and *_json columns carry CHECK(json_valid(...)).

CREATE TABLE IF NOT EXISTS series_bible(
  id TEXT PRIMARY KEY,
  series_id TEXT NOT NULL REFERENCES series(id),
  version INTEGER NOT NULL,
  content_json TEXT NOT NULL CHECK(json_valid(content_json)),
  content_sha256 TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 's1',
  created_at TEXT NOT NULL,
  UNIQUE(series_id, version)
);
CREATE INDEX IF NOT EXISTS ix_series_bible_series ON series_bible(series_id, version DESC);

ALTER TABLE deliverables ADD COLUMN metadata_json TEXT
  CHECK (metadata_json IS NULL OR json_valid(metadata_json));

ALTER TABLE deliverables ADD COLUMN safety_status TEXT NOT NULL DEFAULT 'pending'
  CHECK (safety_status IN ('pending','clear','flagged'));
