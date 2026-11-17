-- Phase 3: audio engine.
-- The Phase 3 spec migration ALTERed audio_cues, audio_library and
-- character_voices as if they already existed. They never landed in 001-003,
-- so they are created here with their final shape (documented in PHASE3_REPORT.md).
-- No comment below may contain a semicolon: the migrator splits on semicolons.

-- D1: retention upgrade needs a purge timestamp for keep_n_days entries.
ALTER TABLE assets ADD COLUMN purge_after TEXT;

-- S5: operator post-gate edit (NULL until edited, then COALESCE(text_final, text) is authoritative),
-- conform outputs, persisted TTS nonce, voice snapshot, and line status.
ALTER TABLE narration_lines ADD COLUMN text_final TEXT;
ALTER TABLE narration_lines ADD COLUMN conformed_sha256 TEXT REFERENCES assets(sha256);
ALTER TABLE narration_lines ADD COLUMN conformed_samples INTEGER;
ALTER TABLE narration_lines ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
  CHECK (status IN ('pending','voiced'));
ALTER TABLE narration_lines ADD COLUMN tts_nonce TEXT NOT NULL DEFAULT '0';
ALTER TABLE narration_lines ADD COLUMN voice_snapshot_json TEXT NOT NULL DEFAULT '{}'
  CHECK (json_valid(voice_snapshot_json));

-- S6: locked clip timing. clip_duration_ms is the REQUESTED generation duration
-- (a supported value), speed_factor is PLAYBACK SPEED in (0, 1]
-- (effective_frames = ceil(clip_frames / speed_factor) + hold_frames).
-- min_duration_ms is the floor contributed by the shot plan (NULL means none).
ALTER TABLE shots ADD COLUMN clip_duration_ms INTEGER;
ALTER TABLE shots ADD COLUMN speed_factor REAL NOT NULL DEFAULT 1.0
  CHECK (speed_factor > 0 AND speed_factor <= 1.0);
ALTER TABLE shots ADD COLUMN hold_frames INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shots ADD COLUMN min_duration_ms INTEGER;
ALTER TABLE shots ADD COLUMN timing_status TEXT NOT NULL DEFAULT 'pending'
  CHECK (timing_status IN ('pending','locked','overflow'));

CREATE TABLE scenes(
  id TEXT PRIMARY KEY,
  track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  scene_no INTEGER NOT NULL,
  shot_from INTEGER NOT NULL,
  shot_to INTEGER NOT NULL,
  music_mood TEXT,
  intensity INTEGER NOT NULL DEFAULT 3 CHECK (intensity BETWEEN 1 AND 5),
  ambience_tags_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(ambience_tags_json)),
  created_at TEXT NOT NULL,
  UNIQUE(track_id, scene_no),
  CHECK (shot_from >= 1 AND shot_to >= shot_from)
);
CREATE INDEX ix_scenes_track ON scenes(track_id, scene_no);

CREATE TABLE character_voices(
  id TEXT PRIMARY KEY,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  character_slug TEXT NOT NULL,
  language TEXT NOT NULL,
  voice_id TEXT NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(settings_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(series_id, character_slug, language)
);

-- moods_json is the single controlled-vocabulary tag column: it holds moods for
-- music and ambience/sfx tags for the other two kinds (documented in audio/library.py).
CREATE TABLE audio_library(
  sha256 TEXT PRIMARY KEY,
  rel_path TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('music','ambience','sfx')),
  title TEXT NOT NULL,
  moods_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(moods_json)),
  intensity INTEGER CHECK (intensity BETWEEN 1 AND 5),
  bpm INTEGER,
  loopable INTEGER NOT NULL DEFAULT 0 CHECK (loopable IN (0,1)),
  duration_ms INTEGER NOT NULL,
  loudness_lufs REAL,
  license TEXT,
  license_url TEXT,
  source TEXT,
  attribution_required INTEGER NOT NULL DEFAULT 0 CHECK (attribution_required IN (0,1)),
  attribution_text TEXT,
  active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0,1)),
  scan_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX ix_audio_library_kind ON audio_library(kind, active);

CREATE TABLE audio_cues(
  id TEXT PRIMARY KEY,
  deliverable_id TEXT NOT NULL REFERENCES deliverables(id) ON DELETE CASCADE,
  cue_index INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('music','ambience','sfx')),
  library_sha256 TEXT NOT NULL REFERENCES audio_library(sha256),
  scene_id TEXT,
  start_frame INTEGER NOT NULL,
  end_frame INTEGER NOT NULL,
  source_offset_ms INTEGER NOT NULL DEFAULT 0,
  gain_db REAL NOT NULL DEFAULT 0,
  fade_in_ms INTEGER NOT NULL DEFAULT 0,
  fade_out_ms INTEGER NOT NULL DEFAULT 0,
  params_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_json)),
  plan_hash TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(deliverable_id, kind, cue_index),
  CHECK (end_frame > start_frame)
);
CREATE INDEX ix_audio_cues_deliverable ON audio_cues(deliverable_id);

CREATE TABLE qc_results(
  id TEXT PRIMARY KEY,
  deliverable_id TEXT REFERENCES deliverables(id) ON DELETE CASCADE,
  subject_scope TEXT NOT NULL DEFAULT 'deliverable'
    CHECK (subject_scope IN ('series','episode','track','deliverable','shot','line')),
  subject_id TEXT NOT NULL,
  check_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pass','warn','fail')),
  detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
  created_at TEXT NOT NULL
);
CREATE INDEX ix_qc_results_subject ON qc_results(subject_scope, subject_id, check_name);

CREATE TABLE audio_masters(
  id TEXT PRIMARY KEY,
  deliverable_id TEXT NOT NULL REFERENCES deliverables(id),
  input_hash TEXT NOT NULL,
  timeline_hash TEXT NOT NULL,
  master_sha256 TEXT NOT NULL REFERENCES assets(sha256),
  narration_stem_sha256 TEXT REFERENCES assets(sha256),
  total_samples INTEGER NOT NULL,
  loudness_i REAL,
  true_peak REAL,
  lra REAL,
  duck_depth_db REAL,
  stats_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(stats_json)),
  status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready','superseded','failed')),
  created_at TEXT NOT NULL,
  UNIQUE(deliverable_id, input_hash)
);
CREATE INDEX ix_audio_masters_deliverable ON audio_masters(deliverable_id, status);
