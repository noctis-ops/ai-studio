-- Stage enumeration markers: lets the engine distinguish
-- "stage ran, zero subjects" from "stage never enumerated".
CREATE TABLE run_stage_marks(
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  enumerated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, stage)
);
