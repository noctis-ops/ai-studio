-- Widen character_sheets.status to allow 'rejected' (operator rejection at G0).
-- The reject() path writes status='rejected' but the original CHECK only allowed
-- candidate, approved and superseded, so a G0 rejection crashed with an
-- IntegrityError. Migration comments must not contain a semicolon.
-- SQLite cannot alter a CHECK in place, so rebuild the table.

CREATE TABLE character_sheets_new(
  id TEXT PRIMARY KEY,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  character_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role <> ''),
  version INTEGER NOT NULL CHECK (version >= 1),
  asset_id TEXT NOT NULL REFERENCES assets(sha256),
  status TEXT NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate','approved','superseded','rejected')),
  spec_hash TEXT NOT NULL,
  nonce TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_at TEXT
);
INSERT INTO character_sheets_new SELECT * FROM character_sheets;
DROP TABLE character_sheets;
ALTER TABLE character_sheets_new RENAME TO character_sheets;
CREATE UNIQUE INDEX ux_character_sheets_approved
  ON character_sheets(character_id, role) WHERE status = 'approved';
CREATE INDEX ix_character_sheets_series ON character_sheets(series_id, character_id, role);
