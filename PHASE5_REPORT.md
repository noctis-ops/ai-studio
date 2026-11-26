# Phase 5 Report — render, export, bible, retention

2026-11-21 · branch `arena/eb70200d-ai-studio` · Phase 5 base `b6905e5` (adds
the prompt) · head before this report: `fb16bfb` (S14) + the S15 commit · Linux,
Python 3.11.2, **real** `ffmpeg n9.0.2` and `ffprobe 5.2.0-static`
(`.tools/bin`, picked up by `tests/conftest.py`).

`STUDIO_LIVE_FAL`, `STUDIO_LIVE_TTS`, `STUDIO_LIVE_LLM`, `STUDIO_LIVE_NVENC` are
unset and no API key is configured. **Zero spend: no live provider call, no
NVENC encode, no network fetch was made for this report.** Every paid path was
exercised on the mock providers through the real gateway, ledger, cache and
budget code. `COLUMNS=150` for the CLI output quoted below.

Phase 5 delivers: the pure-move split of `media/stages.py` (H5.1–H5.8), the
geometry/encoder/probe layer, S9 animatic + **G2 moved onto it**, S11 assembly
(segmented render, concat join, mux), S12 render QC + **G3 on the render**, S13
export (MP4 + sidecar + thumbnail candidates), S14 series-bible proposals, and
S15 publish marker + retention purge/GC — plus the demo DAG rewired onto the real
S9–S13 tail, `studio doctor` rows, the per-module coverage floors and this
report.

## 1. Discovery Report (§C), verbatim

The discovery document was written and committed (`2648501`) before any Phase 5
code. It is reproduced here unmodified, as §S requires.

<!-- BEGIN PHASE5_DISCOVERY.md (verbatim) -->
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

<!-- END PHASE5_DISCOVERY.md -->

## 2. Test results by ID (§P)

Legend: ✅ pass in this sandbox (Linux, real ffmpeg/ffprobe) · ⏭️ skipped (opt-in
or live) · — not applicable here. Every ✅ below was observed in the final run
recorded in §3 (`733 passed, 4 skipped, 1 deselected`; the exact output is quoted in §3.1).

### Geometry, encoders and the hardening set

| ID | Result | Where |
|---|---|---|
| T-P4F-01 `failed_policy` → edit → resume; siblings not re-paid | ✅ | `tests/test_phase5_hardening.py` |
| T-P4F-02 regenerated clip keeps S6's four columns, exactly `clip_frames` | ✅ | `tests/test_phase5_hardening.py` |
| T-MIG-01 001→007 over a 004-seeded database, `foreign_key_check` clean | ✅ | `tests/test_phase5_config.py` |
| T-CFG-06 deprecated circuit-breaker key | ✅ | `tests/test_phase5_hardening.py` |
| T-GATE-02 static DAG: no paid stage under a `G3_FINAL_CUT` ancestor | ✅ | `tests/test_phase5_hardening.py` |
| T-COV-01 the floors script fails below a floor | ✅ | `tests/test_phase5_floors.py` |
| T-GEO-01 table-driven `fit_plan` (exact/crop/upscale/tall/wide) | ✅ | `tests/test_phase5_video.py::test_geo_01_table` |
| T-ENC-01 fake-runner probe: OK, listed-but-failing, timeout, wrong frames, cache + invalidation | ✅ | `tests/test_phase5_video.py::test_enc_01_*` |
| T-ENC-02 live NVENC | ⏭️ skipped | `test_enc_02_live_nvenc_opt_in` (opt-in, no GPU here) |

### S9 and G2

| ID | Result | Where |
|---|---|---|
| T-ANI-01 exact total frames, the mastered audio present and within tolerance, shot map = S6 | ✅ | `tests/test_phase5_animatic.py::test_ani_01_animatic_frames_audio_and_shot_map` |
| T-ANI-02 replaced keyframe → S9 stale + a new G2 round; unchanged inputs cached | ✅ | `tests/test_phase5_animatic.py` |
| T-ANI-03 panel contents and `over_remaining_cap`; mutable numbers absent from output and snapshot hash | ✅ | `tests/test_phase5_animatic.py` |
| T-ANI-04 preview HTML escaping and `dir="auto"` | ✅ | `tests/test_phase5_animatic.py` |
| T-G2-01 S8 has no gate; S9 has G2 | ✅ | `tests/test_phase5_config.py`, `tests/test_phase5_animatic.py` |
| T-G2-02 S10 makes zero calls and zero reservations until G2 is approved | ✅ | `tests/test_phase5_render_qc.py`, `tests/test_phase4_flow.py::test_eng_15_*` |
| T-G2-03 reject shots 3 and 7: only those keyframes re-paid, S9 rebuilds, a new round appears | ✅ | `tests/test_phase5_animatic.py` |
| T-G2-04 a legacy G2 approval on S8 does not satisfy S9 | ✅ | `tests/test_phase5_config.py` |
| T-G2-05 auto-approve allowed in sandbox/draft, refused in final | ✅ | `tests/test_phase5_config.py` |

### S11

| ID | Result | Where |
|---|---|---|
| T-ASM-01 per-frame `signalstats` run-lengths equal each `clip_frames`; total = Σ | ✅ | `tests/test_phase5_assemble.py`, `tests/test_phase5_video.py::test_asm_01_frame_run_lengths_survive_segment_boundaries` |
| T-ASM-02 ~120 tiny clips within the graph bounds; awkward paths; Windows escaping at string level | ✅ | `tests/test_phase5_video.py::test_asm_02_chunked_join_and_awkward_paths`, `test_paths_*` |
| T-ASM-03 A/V sync probe (flash vs beep onset on the decoded mux) | ✅ | `tests/test_phase5_assemble.py`, `tests/test_phase5_render_qc.py` |
| T-ASM-04 beyond-tolerance clip fails; within tolerance records `crop_loss_pct` | ✅ | `tests/test_phase5_assemble.py::test_asm_04_*` |
| T-ASM-05 cache hit skips the render; profile hash changes the key | ✅ | `tests/test_phase5_assemble.py::test_asm_05_cache_and_profile_hash`, `test_segment_sidecar_mismatch_forces_render` |
| T-ASM-06 `j_cut_ms != 0` refused at S9 and S11 | ✅ | `tests/test_phase5_assemble.py::test_asm_06_j_cut_refused_at_s9_and_s11` |
| T-ASM-07 S6 columns unchanged after S9/S11 | ✅ | `test_asm_07_s6_columns_unchanged` + `test_eng_17` |
| T-ASM-08 zero provider calls from S9/S11/S12 | ✅ | `test_asm_08_s11_makes_no_provider_calls`, `tests/test_phase5_render_qc.py` |
| T-ASM-09 `InsufficientDisk` preflight | ✅ | `tests/test_phase5_assemble.py::test_asm_09_*` |
| T-CRASH-05 crash mid-segment and during mux: resume reuses verified segments, no paid call | ✅ | `tests/test_phase5_video.py` (segment sidecar/resume), `tests/test_phase5_assemble.py` |

### S12 and G3

| ID | Result | Where |
|---|---|---|
| T-QCR-01 each check against a purpose-built bad render | ✅ | `tests/test_phase5_render_qc.py` |
| T-QCR-02 the muxed true peak is re-measured, not copied from the master | ✅ | `tests/test_phase5_render_qc.py` |
| T-QCR-03 the upstream QC aggregate | ✅ | `tests/test_phase5_render_qc.py` |
| T-G3-01 a `metadata_safety` (or any non-render G3) approval does not unlock S13; a re-render blocks until re-approved | ✅ | `tests/test_phase5_render_qc.py`, `tests/test_phase5_export.py` |
| T-G3-02 the checklist is required in final, stored on the gate, copied to the sidecar (`made_for_kids`) | ✅ | `tests/test_phase5_render_qc.py`, `tests/test_phase5_export.py` |
| T-G3-03 auto-approve refused in final; `auto`-marked in draft | ✅ | `tests/test_phase5_render_qc.py`, `tests/test_phase5_config.py` |

### S13

| ID | Result | Where |
|---|---|---|
| T-EXP-01 layout, atomic write, idempotent re-run, sidecar schema, sha | ✅ | `tests/test_phase5_export.py` (14) |
| T-EXP-02 commercial-use block + audited override | ✅ | `tests/test_phase5_export.py` |
| T-EXP-03 missing licence blocks in final, flags in draft | ✅ | `tests/test_phase5_export.py` |
| T-EXP-04 presets ↔ channels, four keys → four configs | ✅ | `tests/test_phase5_export.py`, `tests/test_phase5_config.py` |
| T-EXP-05 S13 makes zero provider calls and no network access | ✅ | `tests/test_phase5_export.py` |
| T-EXP-06 limit warnings | ✅ | `tests/test_phase5_export.py` |
| T-EXP-07 re-render creates `_r2` and keeps the old directory | ✅ | `tests/test_phase5_export.py` |
| T-EXP-08 slug sanitation and Windows reserved names | ✅ | `tests/test_phase5_export.py` |

### S14 (bible proposals)

| ID | Result | Where |
|---|---|---|
| T-BIB-01 schema + evidence validation (unknown `scene_id`, over-cap) | ✅ | `tests/test_phase5_bible.py` (10) |
| T-BIB-02 never auto-applied; accept bumps the version; stale base refused | ✅ | `tests/test_phase5_bible.py` |
| T-BIB-03 crash points never double-pay beyond the declared windows | ✅ | `tests/test_phase5_bible.py`, `tests/test_engine.py` |
| T-BIB-04 blocked before G3 clears / gate ancestry | ✅ | `tests/test_phase5_bible.py`, `tests/test_phase5_config.py` |

### S15 (publish + retention) — this step

| ID | Result | Where |
|---|---|---|
| T-PUR-01 plan is free, dry by default, never lists protected classes | ✅ | `tests/test_phase5_purge.py::test_pur_01_plan_is_free_and_never_lists_protected` |
| T-PUR-02 retention windows respected (fresh clips kept, old ones listed) | ✅ | `test_pur_02_retention_windows_keep_fresh_clips` |
| T-PUR-03 a cache hit on a purged asset fails closed with zero provider calls | ✅ | `test_pur_03_cache_hit_on_purged_asset_fails_closed`, `tests/test_gateway.py::test_purged_asset_turns_cache_hit_into_miss` |
| T-PUR-04 purge refuses an unpublished episode | ✅ | `test_pur_04_purge_refuses_unpublished` |
| T-PUR-05 shared CAS kept for a second unpublished episode; locked-file retry with injected failures | ✅ | `test_pur_05_shared_asset_kept_and_locked_file_reported`, `test_pur_06b_unlink_retries_then_gives_up` |
| T-PUR-06 idempotent; crash mid-purge resumes with audit rows | ✅ | `test_pur_06_idempotent_and_resumable` |
| T-PUR-07 a published episode refuses paid stages (`EpisodeClosed`), a free re-export still works | ✅ | `test_pur_07_publish_closes_paid_and_keeps_free` |
| publish marker needs a prior export and a URL | ✅ | `test_publish_requires_export_and_url` |
| GC: CAS orphans, scratch, stale `.tmp-*` export dirs | ✅ | `test_gc_plan_and_run` |
| protection index helpers | ✅ | `test_protection_helpers` |
| plan classes + summary | ✅ | `test_purge_plan_classes_and_summary` |
| CLI surface for `publish mark` / `purge plan|run|gc` | ✅ | `test_purge_cli_surface` |

### Integration and lint (this step)

| ID | Result | Where |
|---|---|---|
| T-ENG-17 sandbox demo through S13, real handlers + ffmpeg, counts unchanged from Phase 4, S14 exactly one LLM call, S6 unchanged | ✅ | `tests/test_phase5_integration.py::test_eng_17_demo_dag_through_s13` |
| T-ENG-18 production-shaped chain, gates in order G1 → G0 → G2 → G3, no paid call before its ancestors are approved | ✅ | `test_eng_18_no_paid_call_before_its_gates` |
| T-ENG-19 `--languages ar,en`: TTS doubles, image/video unchanged, two export directories | ✅ | `test_eng_19_two_languages_double_the_per_language_work` |
| T-CLI-05 every new command, including refusal without `--yes` | ✅ | `test_cli_05_new_commands_refuse_without_yes` |
| T-LANG-01 lint covers `assemble`, `export`, `retention`, `bible`, `video` | ✅ | `tests/test_config.py::test_no_hardcoded_language_codes_outside_allowlist` |
| T-CI-02 the CI invocation (91 % gate, new `--cov` roots, floors step) | ✅ | `tests/test_phase4_ci.py::test_ci_02_ci_coverage_paths_and_threshold` |

### Earlier Phase 5 steps (still green)

`tests/test_phase5_hardening.py` (H5.1–H5.7, T-GATE-02 static DAG assertion),
`tests/test_phase5_config.py` (24: settings, presets, migration 007, StageSpec
flags, barrier, gate re-kind), `tests/test_phase5_floors.py` (6: the floors
script itself), `tests/test_phase5_cli_coverage.py` (2), and the three coverage
lift files (44). All ✅.


## 3. Coverage

### 3.1 The CI invocation and its output

```bash
python -m pytest tests/ -q -m "not slow" \
  --cov=studio.core --cov=studio.db --cov=studio.pipeline --cov=studio.audio \
  --cov=studio.providers.tts --cov=studio.qc --cov=studio.video \
  --cov=studio.media --cov=studio.providers --cov=studio.assemble \
  --cov=studio.export --cov=studio.retention --cov=studio.bible \
  --cov-report=term-missing --cov-report=json --cov-fail-under=91
```

Output (tail):

```text
TOTAL                                    11081    897    92%
Coverage JSON written to file coverage.json
Required test coverage of 91% reached. Total coverage: 91.91%
733 passed, 4 skipped, 1 deselected, 1 warning in 3044.22s (0:50:44)
```

**TOTAL 91.91 %** (10184/11081
statements, 897 missing; the `term-missing` column prints it
rounded) — the gate is ≥ 91 %, and the run is
green: `733 passed, 4 skipped, 1 deselected` (the deselected one is the
`slow`-marked live smoke; the four skips are the opt-in live tests).

### 3.2 Per-module table (every covered file, worst first)

| file | stmts | miss | cover |
|---|---:|---:|---:|
| `src/studio/providers/factory.py` | 59 | 20 | 66% |
| `src/studio/db/migrate.py` | 64 | 11 | 83% |
| `src/studio/providers/openai_llm.py` | 83 | 13 | 84% |
| `src/studio/audio/mastering.py` | 47 | 7 | 85% |
| `src/studio/media/preflight.py` | 267 | 39 | 85% |
| `src/studio/media/retry.py` | 147 | 22 | 85% |
| `src/studio/pipeline/qc_render.py` | 301 | 45 | 85% |
| `src/studio/providers/fal/adapter.py` | 336 | 47 | 86% |
| `src/studio/video/analysis.py` | 97 | 13 | 87% |
| `src/studio/media/clips.py` | 130 | 16 | 88% |
| `src/studio/pipeline/stages_render.py` | 409 | 48 | 88% |
| `src/studio/pipeline/stages_text.py` | 406 | 47 | 88% |
| `src/studio/qc/visual.py` | 144 | 17 | 88% |
| `src/studio/video/encoders.py` | 160 | 19 | 88% |
| `src/studio/assemble/paths.py` | 27 | 3 | 89% |
| `src/studio/audio/envelope.py` | 53 | 6 | 89% |
| `src/studio/core/pricing.py` | 79 | 9 | 89% |
| `src/studio/media/common.py` | 199 | 21 | 89% |
| `src/studio/pipeline/engine.py` | 402 | 46 | 89% |
| `src/studio/pipeline/estimate_media.py` | 179 | 20 | 89% |
| `src/studio/pipeline/stages_audio.py` | 586 | 66 | 89% |
| `src/studio/audio/selector.py` | 78 | 8 | 90% |
| `src/studio/db/connection.py` | 48 | 5 | 90% |
| `src/studio/pipeline/bible_pack.py` | 153 | 15 | 90% |
| `src/studio/pipeline/deps.py` | 30 | 3 | 90% |
| `src/studio/pipeline/gates.py` | 193 | 20 | 90% |
| `src/studio/providers/anthropic_llm.py` | 77 | 8 | 90% |
| `src/studio/audio/library.py` | 192 | 17 | 91% |
| `src/studio/core/cas.py` | 139 | 12 | 91% |
| `src/studio/core/ratelimit.py` | 34 | 3 | 91% |
| `src/studio/db/repos/exports.py` | 22 | 2 | 91% |
| `src/studio/export/stage.py` | 301 | 28 | 91% |
| `src/studio/media/keyframes.py` | 140 | 12 | 91% |
| `src/studio/pipeline/barrier.py` | 47 | 4 | 91% |
| `src/studio/pipeline/panel.py` | 137 | 13 | 91% |
| `src/studio/pipeline/worker.py` | 86 | 8 | 91% |
| `src/studio/providers/mock/common.py` | 117 | 11 | 91% |
| `src/studio/assemble/segments.py` | 128 | 10 | 92% |
| `src/studio/db/repos/sheets.py` | 61 | 5 | 92% |
| `src/studio/retention/protect.py` | 92 | 7 | 92% |
| `src/studio/video/probe.py` | 66 | 5 | 92% |
| `src/studio/bible/apply.py` | 29 | 2 | 93% |
| `src/studio/bible/proposal.py` | 153 | 10 | 93% |
| `src/studio/db/repos/content.py` | 189 | 13 | 93% |
| `src/studio/media/conform.py` | 58 | 4 | 93% |
| `src/studio/providers/mock/llm.py` | 88 | 6 | 93% |
| `src/studio/core/faults.py` | 32 | 2 | 94% |
| `src/studio/core/gateway.py` | 357 | 20 | 94% |
| `src/studio/media/sanitizer.py` | 63 | 4 | 94% |
| `src/studio/media/sheets.py` | 172 | 11 | 94% |
| `src/studio/providers/mock/tts.py` | 31 | 2 | 94% |
| `src/studio/qc/audio.py` | 173 | 10 | 94% |
| `src/studio/retention/plan.py` | 137 | 8 | 94% |
| `src/studio/core/canonical.py` | 42 | 2 | 95% |
| `src/studio/db/repos/audit.py` | 37 | 2 | 95% |
| `src/studio/export/thumbnails.py` | 39 | 2 | 95% |
| `src/studio/pipeline/stages_bible.py` | 43 | 2 | 95% |
| `src/studio/retention/run.py` | 86 | 4 | 95% |
| `src/studio/audio/plan.py` | 252 | 10 | 96% |
| `src/studio/bible/schema.py` | 92 | 4 | 96% |
| `src/studio/db/repos/providers.py` | 77 | 3 | 96% |
| `src/studio/media/prompts.py` | 89 | 4 | 96% |
| `src/studio/pipeline/checklist.py` | 50 | 2 | 96% |
| `src/studio/providers/tts/elevenlabs.py` | 157 | 7 | 96% |
| `src/studio/audio/analysis.py` | 114 | 3 | 97% |
| `src/studio/audio/mixer.py` | 173 | 5 | 97% |
| `src/studio/audio/timeline.py` | 152 | 4 | 97% |
| `src/studio/db/repos/audio.py` | 109 | 3 | 97% |
| `src/studio/db/repos/bible.py` | 37 | 1 | 97% |
| `src/studio/pipeline/recovery.py` | 62 | 2 | 97% |
| `src/studio/providers/mock/video.py` | 30 | 1 | 97% |
| `src/studio/audio/ducking.py` | 43 | 1 | 98% |
| `src/studio/core/budget.py` | 136 | 3 | 98% |
| `src/studio/core/clock.py` | 51 | 1 | 98% |
| `src/studio/db/repos/assets.py` | 46 | 1 | 98% |
| `src/studio/db/repos/pipeline.py` | 81 | 2 | 98% |
| `src/studio/pipeline/dag.py` | 93 | 2 | 98% |
| `src/studio/retention/publish.py` | 48 | 1 | 98% |
| `src/studio/video/geometry.py` | 64 | 1 | 98% |
| `src/studio/providers/base.py` | 94 | 1 | 99% |
| `src/studio/assemble/__init__.py` | 2 | 0 | 100% |
| `src/studio/audio/__init__.py` | 0 | 0 | 100% |
| `src/studio/audio/conform.py` | 61 | 0 | 100% |
| `src/studio/audio/stem.py` | 56 | 0 | 100% |
| `src/studio/audio/voice.py` | 52 | 0 | 100% |
| `src/studio/bible/__init__.py` | 5 | 0 | 100% |
| `src/studio/core/__init__.py` | 0 | 0 | 100% |
| `src/studio/core/cache_key.py` | 9 | 0 | 100% |
| `src/studio/core/ids.py` | 17 | 0 | 100% |
| `src/studio/core/retry.py` | 18 | 0 | 100% |
| `src/studio/db/__init__.py` | 4 | 0 | 100% |
| `src/studio/db/repos/__init__.py` | 36 | 0 | 100% |
| `src/studio/db/repos/base.py` | 17 | 0 | 100% |
| `src/studio/db/repos/budgets.py` | 35 | 0 | 100% |
| `src/studio/db/repos/gates.py` | 36 | 0 | 100% |
| `src/studio/db/repos/jobs.py` | 46 | 0 | 100% |
| `src/studio/db/repos/proposals.py` | 43 | 0 | 100% |
| `src/studio/export/__init__.py` | 7 | 0 | 100% |
| `src/studio/export/blocks.py` | 49 | 0 | 100% |
| `src/studio/export/layout.py` | 83 | 0 | 100% |
| `src/studio/export/sidecar.py` | 29 | 0 | 100% |
| `src/studio/export/slug.py` | 26 | 0 | 100% |
| `src/studio/media/__init__.py` | 1 | 0 | 100% |
| `src/studio/media/refs.py` | 32 | 0 | 100% |
| `src/studio/media/registry.py` | 78 | 0 | 100% |
| `src/studio/media/stages.py` | 10 | 0 | 100% |
| `src/studio/pipeline/context.py` | 25 | 0 | 100% |
| `src/studio/pipeline/specs.py` | 20 | 0 | 100% |
| `src/studio/pipeline/stage.py` | 19 | 0 | 100% |
| `src/studio/pipeline/subjects.py` | 25 | 0 | 100% |
| `src/studio/providers/__init__.py` | 0 | 0 | 100% |
| `src/studio/providers/fal/__init__.py` | 2 | 0 | 100% |
| `src/studio/providers/mock/__init__.py` | 0 | 0 | 100% |
| `src/studio/providers/mock/image.py` | 26 | 0 | 100% |
| `src/studio/providers/tts/__init__.py` | 0 | 0 | 100% |
| `src/studio/qc/__init__.py` | 2 | 0 | 100% |
| `src/studio/retention/__init__.py` | 6 | 0 | 100% |
| `src/studio/video/ffmpeg.py` | 109 | 0 | 100% |

### 3.3 The floors script

```
$ python scripts/check_coverage_floors.py coverage.json coverage_floors.json
coverage floors: PASS
EXIT=0
```

### 3.4 The Phase 5 modules in detail

* Every Phase 5 module clears its floor: `assemble/*` ≥ 88,
  `export/*` ≥ 88, `retention/*` ≥ 88, `bible/*` ≥ 88, `pipeline/stages_render.py`
  ≥ 88, `pipeline/engine.py` ≥ 88 (`coverage_floors.json`).
* The seven Phase 4 modules lifted in step 1 are all ≥ 85
  (`media/sanitizer.py`, `providers/fal/adapter.py`, `video/ffmpeg.py`,
  `qc/visual.py`, `media/registry.py`, `video/probe.py` and the split
  `media/*` modules).
* `src/studio/demo/stages.py` is **not** in the gate's `--cov` roots (it is a
  test-fixture DAG; the prompt lists assemble/export/retention/bible and "any
  new package", and `studio.demo` predates Phase 5). Measured anyway: see the
  table above.
- No `# pragma: no cover` was added to reach a number: every one of the 20 in
  `src/` is justified individually in §3.5.

## 4. Deviations (every changed Phase 1–4 assertion, and why)

Legend: *old* → *new* — reason. Every change below is a direct consequence of a
requirement in this phase's prompt (H5.7 gate re-kind, the §O-1 DAG tail, or the
S14 paid node), not a test relaxed to make code pass.

### 4.1 The metadata review is a `G1_SCRIPT` round, not a `G3_FINAL_CUT` round (H5.7)

Phase 4 re-used `G3_FINAL_CUT` for the `metadata_safety` text review, which
freed the gate kind for the render gate G3 in Phase 5 (`pipeline/gates.py`,
`StageSpec.gate_after` on `metadata_safety`). Nine Phase 1–4 call sites approved
"G3" to mean "the metadata text review"; they now approve the second `G1_SCRIPT`
round, which is what actually parks:

| File | Old | New |
|---|---|---|
| `tests/media_env.py::run_to_g3` | asserted `G3_FINAL_CUT` pending after the script approval | asserts a **second** `G1_SCRIPT` round pending (`run_to_g3` keeps its name: it is "drain to the metadata park") |
| `tests/media_env.py::run_to_g0`, `run_to_g2_preseeded` | `approve_parked(env, "G3_FINAL_CUT")` | `approve_parked(env, "G1_SCRIPT")` |
| `tests/media_env.py::run_to_s10` | stopped at the last shot | also approves `G3_FINAL_CUT` (the render) so "to completion" still means `completed` |
| `tests/test_phase4_flow.py` (2 sites) | `approve_parked(env, "G3_FINAL_CUT")` | `approve_parked(env, "G1_SCRIPT")` |
| `tests/test_phase4_flow.py::test_clp_02_changing_video_model_stales_s6` | same | same |
| `tests/test_phase4_flow.py::test_eng_15_...` | `assert pending_gates(env, "G3_FINAL_CUT")` before any media spend | `assert pending_gates(env, "G1_SCRIPT")`; and the chain now parks at the *render* G3 before completing, with `video_calls_before_g3 == 3` proving S11/S12 are free |
| `tests/test_phase4_int.py::test_int_01_...` | asserted one pending `G3_FINAL_CUT` | asserts one pending `G1_SCRIPT` (the metadata round) |
| `tests/test_phase4_keyframes.py` (4 sites), `tests/test_phase4_sheets.py`, `tests/test_phase4_price.py` (3 sites) | `_approve_parked(env, "G3_FINAL_CUT")` | `_approve_parked(env, "G1_SCRIPT")` |
| `tests/test_stages_text.py::test_s4_flagged_content_parks_at_g3` | selected `approval_gates WHERE gate = 'G3_FINAL_CUT'` | renamed `..._parks_at_metadata_review`; selects `WHERE stage = 'metadata_safety'` and asserts `gate = 'G1_SCRIPT'`, `status = 'pending'` |
| `tests/test_stages_text.py::test_e2e_text_pipeline_parks_at_g1_then_completes` | approved the pending `G3_FINAL_CUT` | approves the pending `G1_SCRIPT` |

### 4.2 The demo DAG now ends in the real S9–S13 tail, plus S14 (§O-1)

`src/studio/demo/stages.py`: `demo_finalize` is gone; the demo runs
`s8_keyframes → s9_animatic → s10p → s10_clips → s11_assemble → s12_render_qc →
s13_export`, and `s14_bible_proposals` runs right after the first G1 round.
`tests/test_engine.py`:

| Test | Old | New |
|---|---|---|
| `test_worker_crash_mid_stage_requeues_and_pays_zero_repaid` | `llm == 1` (one `demo_script` call) | `llm == 2`: `demo_script` + S14's `bible_update`; TTS/image/video counts unchanged (zero re-paid) |
| `test_demo_dag_through_s10_counts_and_formula` | asserted `stage_runs.demo_finalize.status == 'succeeded'` | asserts `s11_assemble`, `s12_render_qc` and `s13_export` each succeeded |
| `test_invalidation_cascades_downstream_and_reruns_for_free` | expected set `{…, demo_finalize}` | expected set gained `s9_animatic`, `s11_assemble`, `s12_render_qc`, `s13_export`, `s14_bible_proposals` and lost `demo_finalize` |
| same test | `assert calls_after == calls_before` | the test edits the episode *concept*, and S14's prompt embeds it, so exactly one new call is expected and asserted: `("llm", "bible_update")`. Every other stage still resumes from the cache (the assertion prints the count delta on failure) |

### 4.3 T-LANG-01 now also covers the Phase 5 packages

`tests/test_config.py`: `_PHASE4_PACKAGES = ("media", "qc", "video")` →
`(..., "assemble", "export", "retention", "bible")`, so the hardcoded-language
lint fails if any of the new packages hardcodes `ar`/`en` outside the
allow-list.

### 4.4 CI and the coverage gate

`.github/workflows/ci.yml`:

| Old | New |
|---|---|
| `--cov-fail-under=90` | `--cov-fail-under=91` (§Q: TOTAL ≥ 91 %) |
| 11 `--cov=` roots | added `studio.assemble`, `studio.export`, `studio.retention`, `studio.bible` |
| `--cov-report=term-missing` | `--cov-report=term-missing --cov-report=json` plus a new step `python scripts/check_coverage_floors.py coverage.json coverage_floors.json` |

`tests/test_phase4_ci.py` follows: `COV_PACKAGES` gained the four new packages,
`--cov-fail-under=90` became `=91`, and the test now also asserts that CI writes
`coverage.json` and runs the floors script.

`coverage_floors.json`: the two Stage B rules were merged in from
`coverage_floors_stage_b.json` (88 %). One of them
(`src/studio/pipeline/stages_media.py`) was **dropped**: the guard exists exactly
to catch a glob that matches no file, and that module was never created — the
media stages live in `media/*.py`. The other rule (`src/studio/retention/*.py`,
88 %) is enforced and passes.

### 4.5 CLI help and README

`src/studio/cli.py`'s app help said "Phase 3"; it now describes the Phase 5
tail. `README.md` gained the "Render, export, bible and retention (Phase 5)"
section, the new packages in the layout listing, and the 91 % + floors sentence
under CI. No behaviour changed.

### 4.6 `assets.purge_reason` on the domain model

`domain/models.py::AssetRow` gained `purge_reason: str | None = None`. The
column existed in migration 007 and the purge wrote it, but the row model did
not expose it, so T-PUR-06 (`purge_reason` starts with `s15_purge:`) could not
be asserted through the repo. Purely additive.

### 4.7 Phase 1–4 assertions changed by S15 itself

| Test | Old | New | Why |
|---|---|---|---|
| `tests/test_gateway.py::test_purged_asset_turns_cache_hit_into_miss` | a purged asset turns the hit into a miss: `second.cache_hit is False` and `submit_count == 2` | the hit raises `AssetPurged` with `submit_count == 1` and an audited `cache_hit_purged`; passing `allow_repay_purged=True` re-pays with `repay_purged_allowed` | §M/§H5.6 "purged assets never trigger a silent re-pay". The old assertion *was* the silent re-pay |
| `tests/test_mocks_cli.py::test_cli_end_to_end_demo` | approved three gates by kind, then asserted the `assets.kind='manifest'` row that `demo_finalize` wrote | approves whatever is pending (3 rounds: G1 script, G2 animatic, G3 render) and asserts the **S13 export**: an `exports` row whose `dir_path` holds `metadata.json` and an MP4 | §O-1 removed `demo_finalize`; the demo now ends in the real S13 |
| `tests/test_phase4_flow.py::test_pre_01_over_budget_halts_before_any_spend` | asserted the halt message contains `expected media spend` (the halt was at s4p) | still asserts `halted_budget`, zero paid media calls and no media stage rows, and asserts the audit error names `cap 1 micro-USD` | S14 is a paid stage in the text lane, so a 1-micro-USD cap now halts at its reservation *before* s4p. The property (halt before any spend) is unchanged; the stage that trips it is not |
| `tests/test_config.py::test_no_hardcoded_language_codes_outside_allowlist` | scanned `media`, `qc`, `video` | also scans `assemble`, `export`, `retention`, `bible` (T-LANG-01) | §P |
| `tests/test_phase5_coverage_lift3.py` | — | one new test for `media/preflight.py`'s `_budget_row_cap`/`_remaining_musd` branches | the over-budget test above no longer reaches s4p's affordability guard, which cost `preflight.py` its 85 % floor; the new test covers the same helper directly |

No other Phase 1–4 assertion changed in step 9. `tests/test_engine.py` and
`tests/test_config.py` changes are recorded in §4.2 and §4.3.


## 5. Decisions on ambiguities

Each entry: the ambiguity, the decision, and the evidence. Where two readings
existed the fail-closed one won.

### 5.1 E5 segment-join: what the copy-join actually does (measured)

The design (`assemble/segments.py`, §E5/D1) renders every shot as its **own
segment with the render's encoder profile** and joins the segments with the
concat demuxer `-c copy`, chunked at `assemble.max_inputs_per_graph`. Before
committing to that, the failure modes were measured on this sandbox
(ffmpeg n9.0.2, `testsrc2`, 320x568, 24 fps, three 120-frame segments):

| run | join | frames | duration | non-monotonic PTS | consecutive duplicate frames |
|---|---|---|---|---|---|
| A | copy-join, all segments `-video_track_timescale 12800` | 360 | 15.000 s | 0 | 0 |
| B | copy-join, one segment at `timescale 12288` | 360 | 15.000 s | **1** | 0 |
| D | copy-join, one segment at `timescale 1000` | 360 | 15.000 s | **47** | 0 |
| E | re-encode the mismatched list (`libx264`, `-r 24 -fps_mode cfr`) | 360 | 15.000 s | 0 | **48** |

What this shows:

* **The frame count is robust**: every variant produced exactly Σ frames, so
  "exactly Σ `clip_frames`" (T-ENG-17, T-ASM-01) is not evidence that the join
  was seamless.
* **The timestamps are not robust**: a segment whose timebase cannot represent
  1/24 s exactly makes the copied stream jump backwards at the join (47
  non-monotonic PTS in run D; the PTS sequence went 4.958 → 4.875).
* **Re-encoding "fixes" timing by duplicating frames** (48 duplicates in run E),
  which is worse for the product: the video would visibly freeze.

Decision: the join stays a plain copy-join, but it is only ever fed segments
rendered by the **same** encoder profile in the **same** process family (so
they share a timebase), and every segment is verified by decode count before
the sidecar is written. S12 then re-probes the assembled file for the frame
count, the duration and monotonic PTS (T-RQC-*) and G3 reviews that checklist,
so a bad join cannot reach an export. There is no silent "re-encode if it looks
wrong" fallback: a join that fails verification fails the stage.

### 5.2 What the animatic carries, and why its bytes are not in the snapshot

S9 runs before S10 (clips) but after S7b (the audio master). The animatic is
therefore a real, playable file: the accepted keyframes held at the locked
timeline timing, over the actual mastered mix, so G2 reviews picture *and*
audio before any clip money is spent (`build_animatic` muxes the S7b master
with `mux_audio`). It verifies its own frame count against
`timeline.total_frames` and its A/V drift against `assemble.av_sync.fail_ms`.

The MP4's bytes are deliberately **not** part of the G2 snapshot: x264 output is
not byte-stable across thread counts or versions, so a snapshot holding the file
sha would go stale for reasons the operator cannot see. The snapshot (and the
input hash) is the deterministic content instead: timeline hash + every
keyframe sha + the master sha + the animatic settings. G2's approval therefore
means "these stills, this timing, this mix".

### 5.3 G2 sits on S9, G3 sits on S12 — what "approved snapshot" means

`gates.py` snapshots the stage `input_hash` plus the asset shas it decides on.
G2 snapshots the keyframes + timeline + master sha (not the animatic's own
bytes, §5.2); G3 snapshots the render sha, the S12 checklist and the audio mix
sha. Approving a round whose snapshot no longer matches the current stage output
is refused (`GateSnapshotMismatch`), which is what makes "approve a non-render
G3-kind round never unlocks export" true by construction - and T-GATE-02 asserts
it statically over the DAG: **no `spends_money` stage has an ancestor gate of
kind `G3_FINAL_CUT`**.

### 5.4 S14 is parked at G1 and cannot spend before it clears

`s14_bible_proposals` is `spends_money=True`, `gate_after=None`, and its only
dependency is `shot_plan` (S3), whose gate is G1 — in the demo it hangs off
`demo_script`. The H0 barrier therefore refuses it until the **current** G1
round is approved (and re-refuses it if that approval goes stale), and S15
refuses it once the episode is published (`EpisodeClosed`). It is a side branch
of the text lane, not a descendant of the render lane, which is what keeps
T-GATE-02 true: no paid stage has a G3 ancestor.

### 5.5 Purge protection: what "intermediate" may delete

§M allows purging intermediates but not what a later stage or a second episode
still needs. `retention/protect.py` builds an index with six reasons:
`retention:permanent`, `series_bible` (every `assets.kind='bible'` row),
`character_sheet:approved`, `gate_approved:<stage>` (**only** `*_keyframes`
stage outputs whose latest gate round is approved), `run:in_flight` (any stage
output of a run that is not `completed`/`failed`/`halted`) and
`episode:unpublished` (a sha still referenced by another episode that is not
published). The animatic is deliberately *not* gate-protected: §M lists
animatics as intermediates, and G2's snapshot keeps the audit trail.

### 5.6 Purging the episode being purged vs "unpublished" protection

Rule 4 (`episode:unpublished`) keeps assets referenced by an episode that has
not been published. When purging episode X, X itself is skipped in that rule
(otherwise nothing would ever be purgeable), while every *other* episode still
protects its own references. This is the reading of T-PUR-05 ("a shared CAS file
referenced by a second unpublished episode is kept").

### 5.7 A purged asset can never cause a silent re-pay

`core/cas.py` records `purged_at` per sha and `core/gateway.py` checks cache rows
whose asset is gone: the hit is refused with `AssetPurged` and the audit row
`cache_hit_purged`, *before* any provider call, unless the operator passed
`--allow-repay-purged` at purge time (audited as `s15_purge_allow_repay` on the
purge and `repay_purged_allowed` on the call). Failing closed was chosen over
"re-pay automatically because the user probably wants the asset": that is
exactly the silent double-spend the project's priorities forbid.

### 5.8 Locked files during purge

Windows (and a busy Linux box) can refuse `unlink` with `EBUSY`/`EACCES`. The
purge retries three times with a short backoff, then records
`s15_purge_skipped_locked` and returns `result.ok=False`; the CLI exits 1 and
tells the operator to close the app and re-run. Files it already deleted stay
deleted (the purge is resumable), and a re-run continues where it stopped.


### 3.5 Every `# pragma: no cover`, justified individually

The prompt forbids adding pragmas to hit a number. There are 20 in `src/`; none
of them covers reachable product logic. Listed by file:

| Location | Why it is excluded |
|---|---|
| `live_smoke.py:125` `if __name__ == "__main__"` | the opt-in live entry point; never imported |
| `media/clips.py:216,220` | the *optional* retention upgrade of the raw clip (a second CAS row with `retention=keep_n_days`) and the fallback that hashes the file itself when the upgrade did not happen: a failure there must not fail a clip that already passed QC |
| `pipeline/qc_render.py:212` | `ValueError/UnicodeDecodeError` while parsing ffmpeg's own stderr (a string we produced); a decode error there is a bug in the parser, not a product path |
| `pipeline/qc_render.py:252` | `ValueError` on a JSON column the schema `CHECK`s (`json_valid`); unreachable without a corrupted database |
| `pipeline/stages_render.py:107` | `json.JSONDecodeError` on `stage_runs.output_json`, which the migration constrains to valid JSON |
| `pipeline/stages_render.py:794` | `row.output_json is None`, already filtered by the surrounding query |
| `pipeline/stages_render.py:874` | `ValueError` on a JSON-checked column |
| `video/encoders.py:168` | `OSError` while reading the probe cache; ffmpeg presence is checked earlier in the same function |
| `cli.py:729` | inside `doctor`'s encoder row: an exception while formatting the probe's own stderr must not abort the table |
| `cli.py:830` | platform-dependent `OSError` on a write probe (Windows ACLs, read-only mounts) |
| `cli.py:4375` | `main()` console-script wrapper |
| `export/layout.py` (`platform-dependent`) | the `os.replace`/rename fallback for filesystems that refuse an atomic replace |
| `export/stage.py` (`ISO timestamps are CHECKed upstream`) | parsing a column the schema validates |
| `export/stage.py:450-452` (`defensive`) | parsing the `_rN` suffix of an existing export directory; unreachable unless someone hand-renames a directory to something ending in the marker |
| `export/stage.py` (`convenience for tests`, `small helper`) | two one-line accessors only used by tests |
| `retention/protect.py:79` (`defensive`) | `json.dumps` of a payload built from untyped ffmpeg-derived rows: `TypeError`/`ValueError` cannot occur for our payloads, and a failure must not stop the index build |
| `retention/protect.py:155` (`schema always has pipeline_runs`) | `sqlite3.Error` around the `pipeline_runs` scan on a database that predates the table |
| `retention/run.py` (`loop always returns`) | an unreachable tail after a `while True` that always returns/raises |

Everything else in the new modules is either covered or genuinely missed and
shown in the `term-missing` table above.


## 6. Known limitations (read this before trusting a Phase 5 artefact)

* **Nothing live was run.** No fal call, no ElevenLabs call, no Anthropic/OpenAI
  call, no NVENC encode, no `nvidia-smi`. Every paid path in this report ran on
  the mock providers through the real gateway (`provider_calls`, ledger,
  cache and budget code are the production ones; only the provider is a mock).
* **NVENC is detected, not exercised.** `video/encoders.py` probes
  `ffmpeg -h encoder=h264_nvenc` and a one-frame test encode, caches the result
  per (geometry, driver) and falls back to `libx264` when the probe fails.
  On this sandbox there is no GPU, so every render used `libx264`; the
  `h264_nvenc` flags (`-preset p5 -rc vbr -cq`) are unverified against hardware.
* **Copy-join is only safe because of the pipeline, not the demuxer.** See §5.1:
  the concat demuxer reproduces the frame count but not always the timestamps.
  The pipeline guarantees one encoder profile per render and verifies each
  segment before joining; a render whose PTS are not monotonic fails S12.
* **A/V sync is measured, not guaranteed.** S12 compares the audio duration with
  the video duration and the first/last packet PTS; the tolerances are
  `assemble.av_sync.{warn_ms: 40, fail_ms: 100}`, both flagged uncalibrated in
  `settings.yaml`. AAC priming (~1024 samples ≈ 21 ms at 48 kHz) means the audio
  *stream* is always slightly longer than its content; the check tolerates that,
  and it cannot detect a constant offset smaller than the priming itself.
* **`silencedetect` / `freezedetect` thresholds are heuristics.** `qc.render.video_scan`
  turns black/freeze detection on for the whole render; every hit is a *warning*
  row in the checklist, and the thresholds were only verified on synthetic
  material.
* **Loudness is measured, not certified.** Mastering targets
  `master.target_lufs: -14` with `master.true_peak_db: -1.5`
  (`loudnorm`+`alimiter`); S12 re-measures the muxed AAC with
  `ebur128=peak=true` (a 4x oversampled estimate, not a broadcast meter) against
  `qc.render.true_peak_warn_db: -1.0` / `..._fail_db: -0.3`, both flagged
  uncalibrated. Platform limits (YouTube/Instagram/TikTok re-encoding) are
  unverified.
* **Preset bitrates are nominal.** `channel_profiles.yaml` carries a target
  bitrate per preset; the encoder is CRF/CQ-based, so the delivered bitrate
  varies with content. "Platform accepts it" was never tested against a real
  upload.
* **Windows is untested here.** The code is path-portable (pathlib, concat
  escaping, `\\?\` handling for long paths, slug sanitation incl. reserved
  names) and the CI matrix includes windows-latest, but this sandbox is Linux.
  The locked-file purge path (§5.8) is exercised with injected failures, not
  with a real Windows handle.
* **The report's PTS/frame numbers** come from ffmpeg n9.0.2 with ffprobe
  5.2.0-static; another ffmpeg build can produce different (still valid)
  results.
* **`studio media smoke` and the live opt-in tests** were not run: no keys.
* **Retention purge deletes files, not rows.** The `assets` row keeps
  `purged_at`/`purge_reason`; the CAS file is gone. Restoring a purged asset is
  only possible by re-running the stage (with `--allow-repay-purged`, audited).


## 7. Unverified register

Every item is *implemented and covered offline* but was **not observed against
the real thing** in this environment. "Where tested" means the offline test that
pins the code's own behaviour, not the external fact.

| # | Claim we do **not** verify | Where the code is tested offline |
|---|---|---|
| 1 | NVENC availability/flag names and behaviour on real hardware (`-preset p5 -rc vbr -cq`, `h264_nvenc` vs `hevc_nvenc`, driver differences) | `tests/test_phase5_video.py::test_enc_01_*` (probe parsing, fail-closed, auto fallback, cache key incl. driver, `test_enc_02_live_nvenc_opt_in` is opt-in and skipped), `test_encode_probe_cli*` |
| 2 | BT.709 tagging (`-colorspace bt709 -color_primaries bt709 -color_trc bt709`) surviving platform re-encode | `tests/test_phase5_video.py::test_geo_color_matrix_for_stills`, `test_enc_01_profiles_and_hashes` (the flags are in the profile hash) |
| 3 | `-bitexact` producing byte-identical output across ffmpeg builds | profile/hash tests (`test_enc_01_profiles_and_hashes`, `test_asm_05_cache_and_profile_hash`); the flag is applied, byte-identity is not claimed |
| 4 | concat `-c copy` behaviour on arbitrary external inputs | §5.1 measurement + `tests/test_phase5_video.py::test_asm_01_frame_run_lengths_survive_segment_boundaries`, `test_asm_02_chunked_join_and_awkward_paths`, `test_asm_01_segment_reuse_and_profile_change` |
| 5 | AAC priming / encoder delay and duration overshoot | `tests/test_phase5_render_qc.py` (mux + duration checks), `tests/test_phase5_assemble.py::test_asm_01_render_frames_and_report` |
| 6 | `silencedetect` accuracy on real narration (threshold, minimum silence) | `tests/test_phase5_render_qc.py` (parsing + checklist rows), `tests/test_audio_qc*` (Phase 3 QC) |
| 7 | A/V sync thresholds (`assemble.av_sync.warn_ms/fail_ms`: what drift is audible) | `tests/test_phase5_render_qc.py` (the measurement and the tolerance are exercised; the *values* are uncalibrated) |
| 8 | Preset bitrates and platform limits (YouTube/TikTok/Instagram acceptance) | `tests/test_phase5_export.py` (preset matrix, `T-EXP-04` presets↔channels), `tests/test_phase5_config.py` |
| 9 | True-peak ceilings (`master.true_peak_db: -1.5` as a real limiter guarantee) | `tests/test_audio_mix.py` + the S12 checklist; `ebur128=peak=true` is an estimate |
| 10 | `faststart` scan: what players actually do with a moov-at-front file | `tests/test_phase5_export.py` (the scan and the flag), `tests/test_phase5_render_qc.py` |
| 11 | Windows path handling (long paths, reserved names, `EBUSY` retries) | `tests/test_phase5_export.py` (slug sanitation, reserved names), `tests/test_phase5_purge.py` (locked-file retries with injected failures) |
| 12 | `nvidia-smi` parsing on real drivers | `tests/test_phase5_video.py` (doctor row, tolerant parsing, "unavailable" is never a failure) |
| 13 | Real LLM output shape for `bible_update` (only the mock + a strict validator) | `tests/test_phase5_bible.py` (schema/evidence validation, over-cap rejection, stale base) |
| 14 | Real fal/ElevenLabs pricing and refusal behaviour | Phase 4 report's register; unchanged here |
| 15 | Real publish targets (the `publish mark` URL is operator-supplied and not fetched) | `tests/test_phase5_purge.py::test_pur_01_*` / CLI surface test |


## 8. Commands — the full operator flow

All commands are `studio <verb>`; on Windows use `py -3.11 -m studio.cli` (or the
`studio.exe` shim) with the same arguments. `$H` is `$STUDIO_HOME`
(`%STUDIO_HOME%` on Windows); the defaults below assume it is set.

### 8.1 One-time setup

```bash
# Linux / macOS
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,providers]"
# ffmpeg + ffprobe on PATH (CI uses FedericoCarboni/setup-ffmpeg; a workspace
# copy under .tools/bin is picked up automatically by tests/conftest.py)
python scripts/check_ffmpeg.py
studio init                     # creates $STUDIO_HOME, runs migrations 001..007
studio doctor                   # keys, registry, ffmpeg filters, NVENC probe, export root, presets, retention
```

```powershell
# Windows / PowerShell
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,providers]"
# ffmpeg: winget install Gyan.FFmpeg  (or setup-ffmpeg in CI)
python scripts/check_ffmpeg.py
studio init
studio doctor
```

### 8.2 Encoder probe (free, never fails the command)

```bash
studio encode probe             # prints the cached NVENC probe: available / reason
```
(`doctor` shows the same row; a missing GPU is a WARN, never an error.)

### 8.3 Sandbox demo through S13 (mock providers, real ffmpeg, no money)

```bash
studio run --demo --profile sandbox --languages ar
studio worker                          # the demo runs the text stages, parks at G1
studio gates list --run <run-id>
studio gates approve <gate-id> --note "script ok"
studio worker                          # ... approve the metadata round, G0, G2 and G3 in order
studio export <run-id>                 # idempotent: re-writes the same directory
```

### 8.4 Production-shaped run (gates in order G1 → G0 → G2 → G3)

```bash
studio run --production --allow-paid-llm --profile final --languages ar --aspects 9:16
studio worker
studio gates show --run <run-id>       # G1_SCRIPT round 1 (script)
studio gates approve <gate-id> --note "script approved"
studio worker                          # S1b..S4, S14 (paid, one call), parks at the G1 metadata round
studio gates approve <gate-id> --note "metadata approved"
studio worker                          # s4p → s5a → s5 → s6 → s7 → s7b → s8a, parks at G0
studio gates approve <gate-id> --note "sheets approved"
studio worker                          # keyframes → s9 animatic, parks at G2
studio gates approve <gate-id> --note "animatic approved"
studio worker                          # clips → s11 assemble → s12 render QC, parks at G3
studio gates show --run <run-id>       # the checklist S12 recorded
studio gates approve <gate-id> --note "final cut approved"
studio worker                          # s13 export (free) → run completed
```

### 8.5 Export, publish, purge

```bash
studio export <run-id> --deliverable ar        # free; a re-render makes _r2 and keeps the old dir
studio render rebuild <run-id>                 # free: re-render from CAS, never re-pays
studio bible propose <episode-id>              # prints worst-case cost; add --yes to spend
studio bible proposals                         # list, status, evidence
studio bible accept <proposal-id> --note "why" # bumps the bible version (stale base refused)
studio publish mark <deliverable-id> --url https://youtu.be/... --yes
studio purge plan --episode <episode-id>       # free, deletes nothing, lists what is protected
studio purge run --episode <episode-id>        # dry run unless --yes
studio purge run --episode <episode-id> --yes  # deletes; exits 1 if a file was locked
studio purge gc                                # dry run unless --yes
```

### 8.6 Verification

```bash
ruff check src tests
mypy                                        # strict, files = ["src"]
python -m pytest tests/ -q -m "not slow" \
  --cov=studio.core --cov=studio.db --cov=studio.pipeline --cov=studio.audio \
  --cov=studio.providers.tts --cov=studio.qc --cov=studio.video --cov=studio.media \
  --cov=studio.providers --cov=studio.assemble --cov=studio.export \
  --cov=studio.retention --cov=studio.bible \
  --cov-report=term-missing --cov-report=json --cov-fail-under=91
python scripts/check_coverage_floors.py coverage.json coverage_floors.json
rm -f coverage.json            # never commit the report
```


## 9. Files

### 9.1 New (Phase 5, whole phase)

Media/video:
`src/studio/media/{__init__,common,retry,preflight,sheets,keyframes,clips}.py` (the
H5.8 pure-move split of `media/stages.py`),
`src/studio/video/{geometry,encoders}.py`, `video/analysis.py`,
`src/studio/assemble/{__init__,paths,segments}.py`,
`src/studio/pipeline/{panel,checklist,qc_render,stages_render,stages_bible}.py`,
`src/studio/export/{__init__,blocks,layout,sidecar,slug,stage,thumbnails}.py`,
`src/studio/bible/{__init__,apply,proposal,schema}.py`,
`src/studio/db/repos/{exports,proposals}.py`,
`src/studio/db/migrations/007_assembly.sql`,
`src/studio/config/presets.py`, `configs/export_presets.yaml`,
`configs/prompts/bible_update.txt`, `scripts/check_coverage_floors.py`.

**Step 9 (S15) specifically:**
`src/studio/retention/__init__.py`, `retention/publish.py`, `retention/protect.py`,
`retention/plan.py`, `retention/run.py`,
`tests/test_phase5_purge.py`, `tests/test_phase5_integration.py`,
`PHASE5_REPORT.md`.

### 9.2 Modified in step 9

| File | Change |
|---|---|
| `src/studio/cli.py` | `publish mark`, `purge plan/run/gc`, the `doctor` Phase 5 rows, the app help text |
| `src/studio/core/cas.py` | `mark_purged(sha, reason=...)` also writes `assets.purge_reason` |
| `src/studio/core/gateway.py` | `CallContext.allow_repay_purged`; a cache hit whose asset is purged raises `AssetPurged` after an audited `cache_hit_purged` (or `repay_purged_allowed` when the operator opted in) |
| `src/studio/db/repos/assets.py` | `mark_purged(sha, reason=...)` writes `purged_at` + `purge_reason` |
| `src/studio/db/repos/content.py` | `set_deliverable_published` (refuses an empty URL, exactly one row) |
| `src/studio/demo/stages.py` | demo DAG wired to the real S9–S13 tail + S14 |
| `src/studio/domain/models.py` | `AssetRow.purge_reason` |
| `tests/test_engine.py`, `tests/test_config.py` | the §4 deviations |
| `coverage_floors.json` | the Stage B rule for `retention/*` merged in (88 %); the stale `stages_media.py` rule dropped |
| `.github/workflows/ci.yml` | 91 % gate, new `--cov` roots, floors step, `--cov-report=json` |
| `README.md` | the Phase 5 section, layout, CI sentence |

### 9.3 Unchanged by design

`models/`, `providers/fal/*`, `providers/tts/*`, `audio/*`, `qc/*`, and the
Phase 1–4 migrations 001–006 were not modified in step 9. No new runtime
dependency was added: `pyproject.toml` is untouched except by earlier steps
(no dependency lines changed anywhere in Phase 5).


## 10. `studio doctor` (real output, this sandbox)

```text
                                                                    studio doctor                                                                     
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ check                                                           ┃ status ┃ detail                                                                  ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ python >= 3.11                                                  │ PASS   │ 3.11.2                                                                  │
│ sqlite >= 3.35                                                  │ PASS   │ 3.40.1                                                                  │
│ ffmpeg on PATH                                                  │ PASS   │ /home/user/ai-studio/.tools/bin/ffmpeg                                  │
│ ffprobe on PATH                                                 │ PASS   │ /home/user/ai-studio/.tools/bin/ffprobe                                 │
│ ffmpeg >= 5.0                                                   │ PASS   │ 9.0.2                                                                   │
│ ffmpeg audio filters                                            │ PASS   │ 32 filters present                                                      │
│ ffmpeg filtergraph file option                                  │ PASS   │ -/filter_complex (ffmpeg >= 7)                                          │
│ sample rate % fps (48000 % 24)                                  │ PASS   │ 2000 samples/frame                                                      │
│ ELEVENLABS_API_KEY                                              │ WARN   │ not set — required for real TTS calls (sandbox/mock does not need it)   │
│ tts.keep_diacritics                                             │ PASS   │ sends diacritized text as-is (higher char cost, better Arabic tashkeel) │
│ audio library                                                   │ WARN   │ empty — run `studio library scan`                                       │
│ voice attestations                                              │ PASS   │ no voices configured in this home                                       │
│ TTS registry tts.candidate.flash                                │ WARN   │ needs_confirmation is true; price never verified (set                   │
│                                                                 │        │ pricing_verified_on after checking your dashboard)                      │
│ TTS registry tts.candidate.v3                                   │ WARN   │ needs_confirmation is true; price never verified (set                   │
│                                                                 │        │ pricing_verified_on after checking your dashboard)                      │
│ TTS registry tts.main                                           │ WARN   │ needs_confirmation is true; price never verified (set                   │
│                                                                 │        │ pricing_verified_on after checking your dashboard)                      │
│ FAL_KEY                                                         │ WARN   │ not set — required for real image/video calls (sandbox mocks do not     │
│                                                                 │        │ need it)                                                                │
│ media registry image.keyframe (verified never)                  │ WARN   │ needs_confirmation is true; price never verified; zero price without    │
│                                                                 │        │ pricing_acknowledged_free · commercial use NOT verified (final blocked) │
│ media registry image.ref.draft (verified never)                 │ WARN   │ placeholder endpoint '<CONFIRM_FAL_ENDPOINT_ID>'; needs_confirmation is │
│                                                                 │        │ true; price never verified; zero price without                          │
│                                                                 │        │ pricing_acknowledged_free · commercial use NOT verified (final blocked) │
│ media registry image.ref.final (verified never)                 │ WARN   │ placeholder endpoint '<CONFIRM_FAL_ENDPOINT_ID>'; needs_confirmation is │
│                                                                 │        │ true; price never verified; zero price without                          │
│                                                                 │        │ pricing_acknowledged_free · commercial use NOT verified (final blocked) │
│ media registry image.t2i.draft (verified never)                 │ WARN   │ placeholder endpoint '<CONFIRM_FAL_ENDPOINT_ID>'; needs_confirmation is │
│                                                                 │        │ true; price never verified; zero price without                          │
│                                                                 │        │ pricing_acknowledged_free · commercial use NOT verified (final blocked) │
│ media registry mock.image (verified never)                      │ PASS   │ commercial use verified                                                 │
│ media registry mock.video (verified never)                      │ PASS   │ commercial use verified                                                 │
│ media registry video.i2v.draft (verified never)                 │ WARN   │ needs_confirmation is true; price never verified; zero price without    │
│                                                                 │        │ pricing_acknowledged_free · commercial use NOT verified (final blocked) │
│ media registry video.i2v.final (verified never)                 │ WARN   │ needs_confirmation is true; price never verified; zero price without    │
│                                                                 │        │ pricing_acknowledged_free · commercial use NOT verified (final blocked) │
│ placeholder endpoint image.ref.draft                            │ WARN   │ '<CONFIRM_FAL_ENDPOINT_ID>' — the operator must confirm the endpoint,   │
│                                                                 │        │ pricing and commercial licence before spending                          │
│ placeholder endpoint image.ref.final                            │ WARN   │ '<CONFIRM_FAL_ENDPOINT_ID>' — the operator must confirm the endpoint,   │
│                                                                 │        │ pricing and commercial licence before spending                          │
│ placeholder endpoint image.t2i.draft                            │ WARN   │ '<CONFIRM_FAL_ENDPOINT_ID>' — the operator must confirm the endpoint,   │
│                                                                 │        │ pricing and commercial licence before spending                          │
│ media spec image.keyframe                                       │ WARN   │ no media wire contract — confirm it in model_registry.yaml              │
│ media spec image.ref.draft                                      │ PASS   │ max_refs=4 · aspects=9:16,16:9                                          │
│ media spec image.ref.final                                      │ PASS   │ max_refs=4 · aspects=9:16,16:9                                          │
│ media spec image.t2i.draft                                      │ PASS   │ max_refs=0 · aspects=9:16,16:9                                          │
│ media spec mock.image                                           │ PASS   │ max_refs=4 · aspects=9:16,16:9                                          │
│ media spec mock.video                                           │ PASS   │ durations_s=5,10 · aspects=9:16,16:9                                    │
│ media spec video.i2v.draft                                      │ PASS   │ durations_s=5,10 · aspects=9:16,16:9                                    │
│ media spec video.i2v.final                                      │ PASS   │ durations_s=5,10 · aspects=9:16,16:9                                    │
│ media.failed_job_billing                                        │ WARN   │ 'charge' — unverified how fal bills failed/flagged jobs; 'charge'       │
│                                                                 │        │ reserves worst case (conservative), 'free' releases on failure          │
│ ffmpeg visual filters                                           │ PASS   │ signalstats, blackdetect, freezedetect, ssim, fps, scale, setpts, tpad, │
│                                                                 │        │ trim                                                                    │
│ media.chain_mode                                                │ PASS   │ 'off' (Phase 5 owns chaining)                                           │
│ Anthropic LLM (claude-sonnet-4-5-20250929, verified 2026-11-09) │ PASS   │ verified 2026-11-09 · in=$3.0/MTok out=$15.0/MTok · commercial use      │
│                                                                 │        │ verified                                                                │
│ OpenAI LLM (gpt-4o-mini, verified 2026-11-09)                   │ PASS   │ verified 2026-11-09 · in=$0.15/MTok out=$0.6/MTok · commercial use      │
│                                                                 │        │ verified                                                                │
│ NVENC h264 probe (informational)                                │ WARN   │  Nothing was written into output file, because at least one of its      │
│                                                                 │        │ streams received no packets.                                            │
│ encoder selection (Phase 5)                                     │ INFO   │ x264 (auto fell back to x264)                                           │
│ write access to /tmp/dh/data                                    │ PASS   │ /tmp/dh/data                                                            │
│ export.root writable                                            │ PASS   │ /tmp/dh/exports                                                         │
│ export.draft_root writable                                      │ PASS   │ /tmp/dh/exports/_draft                                                  │
│ free disk (renders/exports)                                     │ PASS   │ 17.0 GB free                                                            │
│ presets vs channels                                             │ PASS   │ 4 channel keys, 4 presets                                               │
│ retention windows                                               │ PASS   │ raw clips 30d, narration 30d, rejected attempts 7d                      │
│ leftover export .tmp-* dirs                                     │ PASS   │ none                                                                    │
└─────────────────────────────────────────────────────────────────┴────────┴─────────────────────────────────────────────────────────────────────────┘
```

The Phase 5 rows are the last four: `export.root writable`,
`export.draft_root writable`, `free disk`, `presets vs channels`,
`retention windows`, `leftover export .tmp-* dirs`.
