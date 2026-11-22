# PHASE 5 DISCOVERY REPORT (§C) + H5.0 ASSERTION AUDIT

Repo: `noctis-ops/ai-studio`, branch `arena/eb70200d-ai-studio`, base commit
`b6905e5` (single squashed commit in this checkout). Toolchain used for this
discovery and for all Phase 5 verification: Python 3.11.2, real `ffmpeg`
n9.0.2 and real `ffprobe` 5.2.0-static on Linux. `STUDIO_LIVE_NVENC`,
`STUDIO_LIVE_FAL`, `STUDIO_LIVE_TTS` and `STUDIO_LIVE_LLM` were unset for every
command below.

Baseline re-measured before writing any Phase 5 code:

```
python -m pytest tests/ -q -m "not slow"   -> 532 passed, 3 skipped, 1 deselected
ruff check src tests                        -> clean
mypy (strict, 110 files)                    -> clean
TOTAL coverage (Phase 4 CI command)         -> 90.15%  (target >= 91%)
```

Every item below is answered from source inspection, not from the Phase 4
report. Where the prompt and the code disagree, the code wins and the
resolution is fail-closed.

---

## C1 Gates and paid ancestry

**Production DAG** (`src/studio/pipeline/specs.py`):

| # | stage | scope | depends_on | gate_after | spends_money |
|---|---|---|---|---|---|
| S1 | `bible_upsert` | series | — | — | true |
| S2 | `narration_script` | episode | S1 | — | true |
| S3 | `shot_plan` | track | S2 | `G1_SCRIPT` | true |
| S4 | `metadata_safety` | deliverable | S3 | **`G3_FINAL_CUT`** | true |
| S4p | `s4p_media_preflight` | episode | S4 | — | false |
| S5a | `s5a_tts_preflight` | episode | S4p | — | false |
| S5 | `s5_tts` | line | S5a | — | true |
| S6 | `s6_timing` | track | S5 | — | false |
| S7 | `s7_audio_plan` | deliverable | S6 | — | false |
| S7b | `s7b_audio_mix` | deliverable | S7, S5 | — | false |
| S8a | `s8a_character_sheets` | series | S7b | `G0_CHARACTERS` | true |
| S8 | `s8_keyframes` | shot | S8a | `G2_KEYFRAMES` | true |
| S10p | `s10p_clip_preflight` | episode | S8 | — | false |
| S10 | `s10_clips` | shot | S10p | — | true |

Gate kinds are created from `StageSpec.gate_after` in
`Engine._maybe_gate_stage` (`src/studio/pipeline/engine.py`), so the table above
is the complete gate inventory.

**Demo DAG** (`src/studio/demo/stages.py::demo_specs`) mirrors it:
`demo_script(G1)` → `demo_voice` → `s4p` → `s5a` → `s5` → `s6` → `s7` → `s7b` →
`s8a(G0)` → `s8(G2)` → `s10p` → `s10` → `demo_finalize(G3)`.
`demo_script`, `s8a`, `s8`, `s10` are the only `spends_money=True` specs; the
rest default to `False` (`src/studio/pipeline/stage.py`).

**Where `metadata_safety` sits.** `text_specs()` (`src/studio/pipeline/stages_text.py`,
tail of the function) puts `metadata_safety` at the end of S1–S4 with
`gate_after=GateKind.G3_FINAL_CUT`, and `preflight_specs()` in `specs.py`
declares `s4p_media_preflight` as depending on `metadata_safety`. Every paid
media stage (S8a, S8, S10) and every paid audio stage (S5) is therefore a
descendant of a `G3_FINAL_CUT`-kind round.

**What an operator must approve today, in order, before S5 can run:**
1. `G1_SCRIPT` on `shot_plan` (S3).
2. `G3_FINAL_CUT` on `metadata_safety` (S4) — a "final cut" approval demanded
   before any media, audio or render exists.

**Is a `G3_FINAL_CUT`-kind gate an ancestor of a paid stage?** Yes: the
`metadata_safety` round is an ancestor of S5, S8a, S8 and S10. This is exactly
the R4/F5 problem.

**Resolution (H5.7):** re-kind `metadata_safety` to `G1_SCRIPT` (it is a text
approval, not a cut approval), move the real `G3_FINAL_CUT` round to S12 (a
render), and add the static DAG test T-GATE-02. Migration 007 carries the
data-migration `UPDATE approval_gates SET gate='G1_SCRIPT' WHERE stage='metadata_safety'`
(pre-release, expected zero rows). Fail-closed default: any future stage that
sets a `G3_FINAL_CUT` gate and is an ancestor of a paid stage fails T-GATE-02.

## C2 Gate machinery

* `GateService.create_gate_round` (`src/studio/pipeline/gates.py`) computes
  `round = repos.gates.count_rounds(run_id, stage) + 1`, stores `gate`, `stage`,
  `scope`, `subject_id`, `snapshot_hash`, and auto-approves only when the run
  has `auto_approve=1` **and** the profile is in `gates.allow_auto_approve_in`
  (default `sandbox`, `draft`). `final` raises `GatePolicyError` even before the
  allow-list check.
* `snapshot_hash` is derived by the engine from the gated stage's succeeded
  rows: `barrier.stage_outputs_snapshot_hash` = sha256 of the canonical JSON of
  sorted `(stage_run_id, output_json)` pairs (`engine._gate_snapshot_hash`).
  The barrier recomputes the same value and blocks a descendant whose ancestor
  round is approved against a stale snapshot.
* `gate_after` is consumed in two places only: `Engine._maybe_gate_stage`
  (create/park) and `barrier.ancestor_gate_problems` (paid-stage proof). A
  pending round parks the run in `awaiting_approval` and the engine enumerates
  nothing downstream.
* `request_changes` (shots) bumps the shot attempt (`attempt_no + 1`, fresh
  ULID nonce), stales the gated stage and every downstream row related to the
  shot subject (`subjects_related`), and enqueues `advance_run`.
  `_request_changes_sheets` is the per-subject G0 path: it marks the named
  `character_sheets` rows `rejected` and stales only their own S8a rows.
* `auto_approve` rules: see above; `GateSettings.allow_auto_approve_in`.
* **Where approval metadata could be stored:** `approval_gates` has only
  `note TEXT`. There is no JSON column and no "presented state" column. Phase 5
  adds `approval_gates.checklist_json` and `approval_gates.presented_json`
  in migration 007.

## C3 S6 / timeline

* `build_timeline(*, shots, scenes, lines, sample_rate, fps, ...) -> Timeline`
  (`src/studio/audio/timeline.py:133`). `Timeline` is a frozen dataclass with
  `fps`, `sample_rate`, `shots: tuple[ShotSpan, ...]`, `scenes`, `lines`,
  `total_frames`, `total_samples`, `timeline_hash`, and the property
  `samples_per_frame = sample_rate // fps`.
* fps and sample rate live in `settings.render.fps` and
  `settings.audio.sample_rate` (validated to divide exactly in
  `Settings._check_cross_field`). Frame totals are per **track**
  (`Timeline.shots`, `ShotSpan.start_frame` / `effective_frames`); sample
  totals and `timeline_hash` are the deliverable's S6 output.
* Exact relation: `total_samples = total_frames * (sample_rate // fps)`
  (`timeline.py:189-190`), i.e. one integer number of samples per frame. S11
  asserts the master's sample count equals `total_samples` and that the render's
  decoded frame count equals `total_frames`.
* Per track: shot spans, scene spans, `total_frames`. Per deliverable: line
  placements (per language), `total_samples`, `timeline_hash`, and the S6
  stage-run output (`shot_plan`, `locked` timings).

## C4 S7b / audio QC

* Master format: FLAC, `settings.audio.sample_rate` (48 000), stereo, written to
  a temp file and moved into the CAS as kind `audio`
  (`src/studio/pipeline/stages_audio.py::AudioMixHandler`, "success" block).
* Mastering targets: `settings.audio.master.target_lufs = -14.0`,
  `true_peak_db = -1.5`, `lufs_tolerance = 1.0`, `tp_ceiling_db = -1.0`,
  `clip_peak_db = -0.1` (`configs/settings.yaml` + `config/settings.py`).
* `qc/audio.py::run_audio_qc(...)` accepts an `AudioQcInputs` bundle pointing at
  an arbitrary decoded WAV and reuses the Phase 3 analyzers (`ebur128`,
  `astats`, `rms_series_db`, `wav_header_samples`), so S12 can point it at the
  AAC-decoded render. Clip/duck-depth checks need stems and are not applicable
  to a muxed render; S12 uses the loudness/peak/clipping subset.
* S7b results are stored twice: `qc_results` rows (`deliverable_id`,
  `check_name`, `status`, `detail_json`) and the `audio_masters.stats_json`
  `qc` array, plus the stage output (`master_sha256`, `total_samples`,
  `timeline_hash`, `loudness_i`, `duck_depth_db`).

## C5 S10 outputs

* Per-shot accepted attempt: the **latest** `shot_attempts` row with
  `kind='clip'`, `qc_status IN ('pass','warn')` and a non-null
  `conformed_asset_id`. `_ensure_attempt`/`_resume_state` in
  `src/studio/media/stages.py` implement the attempt lifecycle; the S10 stage
  output carries `asset_sha256`, `conformed_asset_id`, `clip_frames`,
  `clip_duration_ms`, `fps`, `qc_status`, `qc`, `model_key`, `attempt_id`.
* `shot_attempts` stores **no** frame count or geometry: only `asset_id`,
  `conformed_asset_id`, `qc_json`, `cost_musd`, `provider_call_id`. Frame counts
  are derived from the locked `shots.clip_duration_ms` / `shots.speed_factor`
  via `media/conform.py::clip_frames_for` and verified with ffprobe.
* A shot with no accepted attempt: `ClipPreflightHandler` fails closed with a
  listed problem; S11 preflight must do the same before rendering.
* Mock clip geometry (`providers/mock/video.py`): 480×270 for 16:9 and 270×480
  for 9:16, 24 fps, h264/yuv420p, no audio stream. Real fal clips will differ in
  both geometry and codec, which is why S11 re-probes every clip.

## C6 Assets / CAS / retention

* `core/cas.py::ContentStore`: `put_file(src, kind, ext, retention, move)`,
  `put_bytes(...)`, `resolve(sha) -> Path` (raises `AssetNotFoundError` when the
  file is missing), `exists_live(sha)`, `mark_purged(sha)`, `verify(sha)`.
  There is no `delete`; purge deletes the file and marks the row.
* Retention classes: free-form `assets.retention` TEXT (default
  `intermediate`); used values today are `intermediate`, `permanent`, `export`
  and `reference`. `assets.purged_at` already exists (001); `assets.purge_after`
  exists (004, retention windows). `assets.purge_reason` does **not** exist.
* `studio purge` does not exist (no CLI command, no service). Phase 5 builds it.
* Gateway cache hit (`core/gateway.py::_cache_hit`): requires every cached sha to
  pass `cas.exists_live` and to have an `assets` row; any failure returns
  `None`, i.e. a **silent cache miss** that would re-pay. This is the
  never-pay-twice hole Phase 5 (§M, T-PUR-03) must close by raising
  `AssetPurged` instead.
* References: `shot_attempts.asset_id` / `conformed_asset_id`,
  `narration_lines.asset_sha256` / `conformed_sha256`,
  `character_sheets.asset_id`, `audio_masters.master_sha256` /
  `narration_stem_sha256`, `cache_entries.assets_json`, and every
  `stage_runs.output_json` asset field. One CAS file can back many asset rows
  (content addressing); purge therefore has to check references globally, not
  per episode.

## C7 Episodes / deliverables / channels

* `episodes.status` CHECK allows `pending,running,awaiting_approval,halted_budget,completed,failed`
  (001). It has **no** `published`/`archived` values: migration 007 rebuilds the
  table (SQLite cannot alter a CHECK) and adds `published_at`.
* `pipeline_runs.status` has the same six values; Phase 5 does not add values
  there (a published episode simply refuses paid stage execution, T-PUR-07).
* `deliverables` has no published/export columns today (001 + 003 add
  `metadata_json`, `safety_status`). 007 adds `published_at`, `published_url`.
* `shot_attempts.qc_status` CHECK is `pending,pass,warn,fail,failed_policy`;
  `audio_masters.status` is `ready,superseded,failed`; `character_sheets.status`
  is `candidate,approved,superseded,rejected` (006).
* `channel_for(language, aspect)` is `ChannelsConfig.for_pair`
  (`src/studio/config/channels.py`), sourced from `configs/channel_profiles.yaml`
  with exactly four keys: `yt_ar_wide`, `yt_en_wide`, `short_ar_tall`,
  `short_en_tall`.
* Episode slug source: `episodes` has no slug column. Only `series.slug` exists
  (`001`, `UNIQUE`). Phase 5 derives the export slug deterministically from the
  episode title when it is ASCII-safe, otherwise `ep-<first 8 of episode id>`,
  then applies the `[a-z0-9-]`, 48-char and Windows-reserved-name rules.

## C8 Metadata and licenses

* `metadata_safety` output schema (`_S4_STAGE_INPUT`, `_parse_metadata` in
  `stages_text.py`): one object per **deliverable** (language-specific) with
  `title`, `description`, `tags[]`, `safety` (`clear|flagged`), `issues[]`.
  Stored as `deliverables.metadata_json` + `deliverables.safety_status`.
* Adaptation `source_ref` / `source_license`: not present anywhere in the schema
  or settings. Phase 5 reads them from an optional episode-level
  `source.json` sidecar under the projects directory and treats absence as
  "adaptation without source" only when `metadata.safety == "flagged"`? No —
  fail-closed rule: S13 requires them whenever the episode has a
  `source_ref` recorded in `episodes` metadata. Discovery result: **no column
  exists**, so 007 adds `episodes.source_ref` and `episodes.source_license`
  (nullable; an absent value is not an error because original works have no
  source).
* Library license records: `audio_library.license`, `license_url`, `source`,
  `attribution_required`, `attribution_text` (004). `audio_cues.library_sha256`
  references the library row per cue, so S13 can resolve every cue of a
  deliverable's audio plan to its license.
* Models in `provider_calls` for an episode: rows carry `capability`,
  `model_key`, `provider`, `status`. `commercial_use_verified` is a property of
  the registry entry (`config/registry.py::ModelEntry`), not of the call row, so
  S13 resolves each distinct `model_key` through the registry.

## C9 Bible

* Storage: `series_bible` (003) — `id, series_id, version, content_json,
  content_sha256, created_by, created_at`, `UNIQUE(series_id, version)`,
  content-addressed and integrity-checked on read
  (`db/repos/bible.py::SqliteBibleRepo._from_row`).
* Read path: `pipeline/bible_pack.py` + `pipeline/context.py` build the context
  pack from the pinned/last bible version.
* Write path: only `SqliteBibleRepo.create_version` (S1 upsert). No proposal,
  review or apply path exists.
* Structured-output helper: `ProviderGateway.llm(...)` with
  `LLMRequest(schema_name=..., expect_json=True, prompt_version_sha=...)` and
  `CallContext(episode_id, stage_run_id, nonce, purpose)`. Prompt files live in
  `configs/prompts/` and are sha'd by `config/prompts.py`.
* S14 will add `configs/prompts/bible_update.txt`; its sha becomes
  `prompt_version_sha`, exactly like S1–S3.

## C10 ffmpeg wrappers

* `video/ffmpeg.py`: `run_ffmpeg(args, *, timeout_s)` and
  `run_ffprobe(args, *, timeout_s)` — binary located by `find_binary`
  (`STUDIO_FFMPEG`/`STUDIO_FFPROBE` env override then PATH), stderr captured and
  truncated into `MediaToolError`, `ffmpeg_version()`, `probe_duration_s()`,
  `probe_audio_format()`, `filtergraph_script_args(path, version)` (uses
  `-filter_complex_script` and swaps to `-/filter_complex` on Windows ffmpeg ≥ 7).
  Not injectable today: the runner is module-level. S9/S11/S12 add an injectable
  runner parameter (`Callable[[list[str]], Awaitable[tuple[bytes, bytes]]]`) with
  the module functions as defaults, which keeps every existing call site intact.
* `scale2ref` appears exactly once in `src/`: `qc/visual.py:380`
  (`_ssim_first_frame`). No other use.
* Windows path escaping: there is none today. `audio/mixer.py` passes a
  `-filter_complex_script` **file** whose body interpolates absolute paths, and
  `filtergraph_script_args` already handles the `-/filter_complex` spelling.
  Phase 5 adds one escaping helper (`assemble/paths.py`) used by both filter
  scripts and concat list files, tested at string level on Linux with
  Windows-style inputs.

## C11 CLI inventory

Existing commands (`src/studio/cli.py`): `init` (`@app.command` at 97),
`doctor` (495), `db migrate` (650), `db bible` (660), `run` (704), `resume`
(911), `worker` (933), `gates list/approve/reject` (959/977/994),
`budget status/reset` (1020/1044), `voice set/list` (1080/1151),
`narration show/edit` (1188/1232), `library scan/list/check` (1344/1388/1425),
`tts audition` (1490), `audio preview/qc/calibrate` (1676/1717/1762),
`shot list/regenerate` (1848/1950), `media preview/qc/audition/smoke`
(2199/2393/2568/2855).

There is **no** `purge`, `export`, `publish`, `render`, `bible` or `encode`
command today. `gates approve` takes `--note` only; `gates reject` takes
`--shot/--sheet` style arguments (see the command body at 994) and has no
`--shots` filtering, no `--all`, no `--deliverable`, and no cost display.
`media preview` supports only the keyframe/sheet stages. Phase 5 adds every §N
command and extends `gates approve/reject`, `media preview`, and `doctor`.

## C12 Phase 4 carry-over audit (H5.0)

`git diff 952c74c --stat -- tests/` **cannot be run**: commit `952c74c` is not
in this checkout (`git log --all` contains exactly one commit, `b6905e5`, which
is the Phase 5 prompt itself). The Phase 4 branch history was squashed before
hand-off. This is a discovery finding, reported as Deviation D-1; the audit
below is therefore an inventory of the current test corpus rather than a diff.

Inventory method: grep for the assertion-weakening markers (`pytest.skip`,
`pytest.mark.skipif`, `xfail`, `assert True`), for the known Phase 4 edits
(`gpt-4o-mini`, mock geometry, `demo_finalize`, T-ENG-08 formula), and for
`# pragma: no cover`.

| Test file | Phase-4 touch | Assertion status |
|---|---|---|
| `tests/test_engine.py:606-660` | T-ENG-08 rewritten | Formula documented inline; asserts counts derived from repo state (shots + characters-without-approved-sheets × roles), not a bare constant. Not weakened: a constant would be weaker. |
| `tests/test_engine.py:730,1025` | demo DAG includes `demo_finalize` | Still asserts the Phase 4 stage list; will be updated in Step 9 when `demo_finalize` is replaced (§O-1), reported as a Deviation. |
| `tests/test_llm_adapters.py:98,232,266,408,421` | H4 removed the `gpt-4o-mini` assumption from *pipeline* tests | These are adapter-level tests and legitimately pin the configured alias; the registry still ships `gpt-4o-mini`. No pipeline test asserts a model id. |
| `tests/test_mocks_cli.py`, `tests/test_phase4_*.py` | mock geometry/size changes | Now derive geometry from `settings.mock` (480×270) and from the registry; no hard-coded magic size outside the adapter tests. |
| `tests/test_config.py` (T-LANG-01) | lint package list extended in H4 | Asserts the scanned package list; Phase 5 extends it to `assemble`, `export`, `retention`, `bible`. |
| All `tests/` | `pytest.mark.skipif` occurrences | Four: ElevenLabs opt-in, live LLM smoke, two fal live tests. All are live/paid opt-ins already documented in Phases 2–4; none silently skips a Phase 1–4 assertion. |
| All `tests/` | `assert True` / `xfail` / `pragma: no cover` | Zero occurrences. |

**Re-measured Phase 4 per-module coverage** (`--cov-report=term-missing`,
`-m "not slow"`, same command as CI):

| module | cover |
|---|---|
| `media/sanitizer.py` | 78% |
| `providers/fal/adapter.py` | 79% |
| `video/ffmpeg.py` | 77% |
| `media/stages.py` | 82% |
| `qc/visual.py` | 83% |
| `media/registry.py` | 83% |
| `video/probe.py` | 82% |
| **TOTAL** | **90.15%** |

No Phase 1–3 assertion was found weakened without documentation. The one
assertion set that Phase 5 must change by design is the demo DAG stage list
(§O-1); it is logged in the final report with old line, new line and reason.

## C13 Demo DAG

`demo_finalize` (`src/studio/demo/stages.py::DemoFinalizeHandler`) is a
deliverable-scope, free handler that collects the S10 clip map, requires every
shot to have a clip, and writes a JSON manifest (`manifest_version: 1`, clips,
narration lines) into the CAS with retention `export`. It carries the
`G3_FINAL_CUT` gate.

To run real S9–S13 the demo path needs:
1. S9 animatic (real ffmpeg) after G2 on `s8_keyframes`;
2. S11 assemble (real ffmpeg) after S10;
3. S12 render QC + `G3_FINAL_CUT` on a render;
4. S13 export (free, no LLM) instead of the manifest;
5. S14 (mock LLM in demo) as an episode-scope paid stage.

`demo_finalize` is therefore replaced by the real S9/S11/S12/S13/S14 specs with
mock providers throughout (`§O-1`). The demo seed already produces all-shots
clips in 480×270 / 270×480, which is a valid input for the geometry plan
(same-aspect upscale, allowed).

---

## H5.0 assertion audit — conclusion

* No unjustified weakening found; D-1 (missing base commit) prevents a literal
  diff and is reported.
* Demo-stage-list assertions will change in Step 9 by design; listed in the
  final report as a Deviation with old/new lines.
* The seven sub-85% modules are confirmed at the numbers F1 gives.

## Discovery decisions (fail-closed)

| # | Decision | Why |
|---|---|---|
| D-1 | Report the missing `952c74c` commit instead of faking a diff. | Honesty rule §B-13. |
| D-2 | Re-kind `metadata_safety` to `G1_SCRIPT`; move `G3_FINAL_CUT` to S12. | R4/H5.7, minimal diff. |
| D-3 | Add `episodes.source_ref` / `source_license` (nullable) in 007. | No existing column; absence of a source is legal for original works. |
| D-4 | Add `assets.purge_reason` in 007. | `purged_at` exists, the reason does not. |
| D-5 | Add `approval_gates.checklist_json` + `presented_json` in 007. | No JSON column exists on gates; both §G4 and §J need one. |
| D-6 | S13's commercial-use superset is computed from `provider_calls` ⋈ registry, not from a new table. | No new table needed; the registry is the source of `commercial_use_verified`. |
| D-7 | Gateway must raise `AssetPurged` (new error) instead of a silent miss. | §M never-pay-twice; the current silent miss is the hole T-PUR-03 pins. |
| D-8 | Episode slug = ASCII title, else `ep-<id[:8]>`, then §K sanitation. | `episodes` has no slug column and Arabic titles are not valid slugs. |
| D-9 | Rebuild `episodes` in 007 for the status CHECK. | SQLite cannot widen a CHECK; lesson from 006. |
