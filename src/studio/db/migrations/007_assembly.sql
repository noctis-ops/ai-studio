-- Phase 5: assembly, final cut, export, bible proposals, retention.
-- migrate: foreign_keys=off
-- (the episodes rebuild below drops a parent table that pipeline_runs cascades
-- from, so foreign keys are off for this file and verified before COMMIT)
-- Statements are separated by semicolons. Migration comments must never contain
-- one (migrate.py splits the file on it), and no string literal may either.
-- Money stays INTEGER micro-USD and every *_json column is JSON-validated.
--
-- CHECK audit (the lesson from 006): the enum values this migration needs are
--   episodes.status  : + 'published', 'archived'   -> table rebuild (SQLite)
--   approval_gates   : gate kind set unchanged, but existing 'G3_FINAL_CUT'
--                      rows on metadata_safety are re-kinded to 'G1_SCRIPT'
--   assets.retention : free TEXT, no CHECK -> nothing to widen
--   exports.status   : new table, CHECK ('pending','written','failed')
--   bible_proposals  : new table, CHECK (pending|accepted|rejected|stale)
-- No other CHECK constraint is touched, so no other table needs a rebuild.

-- 1. H5.7: the metadata text review is a script approval, not a cut approval.
--    No paid stage may have an ancestor gate whose kind is G3_FINAL_CUT.
UPDATE approval_gates SET gate = 'G1_SCRIPT' WHERE stage = 'metadata_safety' AND gate = 'G3_FINAL_CUT';

-- 2. Gate checklist + frozen presented-state panel (both optional until used).
ALTER TABLE approval_gates ADD COLUMN checklist_json TEXT CHECK (checklist_json IS NULL OR json_valid(checklist_json));

ALTER TABLE approval_gates ADD COLUMN presented_json TEXT CHECK (presented_json IS NULL OR json_valid(presented_json));

-- 3. Retention: why an asset was purged (the timestamp already exists in 001).
ALTER TABLE assets ADD COLUMN purge_reason TEXT;

-- 4. Publish markers on the deliverable.
ALTER TABLE deliverables ADD COLUMN published_at TEXT;

ALTER TABLE deliverables ADD COLUMN published_url TEXT;

-- 5. Adaptation provenance (nullable: original works have no source) and the
--    episode-level publish marker.
ALTER TABLE episodes ADD COLUMN source_ref TEXT;

ALTER TABLE episodes ADD COLUMN source_license TEXT;

ALTER TABLE episodes ADD COLUMN published_at TEXT;

-- 6. episodes.status must accept published|archived. SQLite cannot alter a
--    CHECK in place, so rebuild the table and copy every row verbatim.
CREATE TABLE episodes_new(
  id TEXT PRIMARY KEY,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  concept TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','awaiting_approval','halted_budget','completed','failed',
                      'published','archived')),
  languages_json TEXT NOT NULL CHECK (json_valid(languages_json)),
  aspects_json TEXT NOT NULL CHECK (json_valid(aspects_json)),
  source_ref TEXT,
  source_license TEXT,
  published_at TEXT,
  created_at TEXT NOT NULL
);

INSERT INTO episodes_new(id, series_id, title, concept, status, languages_json,
                         aspects_json, source_ref, source_license, published_at, created_at)
  SELECT id, series_id, title, concept, status, languages_json,
         aspects_json, source_ref, source_license, published_at, created_at FROM episodes;

DROP TABLE episodes;

ALTER TABLE episodes_new RENAME TO episodes;

CREATE INDEX ix_episodes_series ON episodes(series_id);

-- 7. Exports: one row per (deliverable, approved render) export directory.
CREATE TABLE exports(
  id TEXT PRIMARY KEY,
  deliverable_id TEXT NOT NULL REFERENCES deliverables(id) ON DELETE CASCADE,
  render_asset_id TEXT NOT NULL REFERENCES assets(sha256),
  dir_path TEXT NOT NULL,
  profile TEXT NOT NULL CHECK (profile IN ('sandbox','draft','final')),
  status TEXT NOT NULL DEFAULT 'written'
    CHECK (status IN ('pending','written','failed')),
  created_at TEXT NOT NULL,
  UNIQUE(deliverable_id, render_asset_id)
);

CREATE INDEX ix_exports_deliverable ON exports(deliverable_id);

-- 8. Bible proposals: never auto-applied. base_bible_version makes staleness
--    checkable without parsing the payload.
CREATE TABLE bible_proposals(
  id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  base_bible_version INTEGER NOT NULL CHECK (base_bible_version >= 0),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','accepted','rejected','stale')),
  proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json)),
  provider_call_id TEXT REFERENCES provider_calls(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT
);

CREATE INDEX ix_bible_proposals_episode ON bible_proposals(episode_id, status);
