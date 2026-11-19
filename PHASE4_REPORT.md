# Phase 4 Report — Media pipeline (fal.ai), visual QC, gates G0/G2

2026-11-17 · Branch `arena/bddd030b-ai-studio` · base `952c74c` (adds this prompt) · Linux, Python 3.11.2.
Environment note: this sandbox carries a **real ffmpeg 7.1.1 / ffprobe 7.1.1** pair
(`.venv/ffmpeg/bin`, static build) — unlike Phase 3's `ffprobe` Python stand-in —
so every ffmpeg-marked test exercised the real tools, including the PNG probe path
fixed in Phase 3 (`src/studio/video/probe.py` no longer passes `-f`).
`STUDIO_LIVE_FAL`, `STUDIO_LIVE_TTS`, `STUDIO_LIVE_LLM` are unset; no API keys are set.
**Zero spend. No live fal call was made for this report.** `COLUMNS=220` for CLI tests.

Phase 4 delivers: the raw-httpx fal adapter (image/video job lifecycle), the s4p and
s10p fail-closed preflights, the S8a/S8/S10 stages (SERIES `ref_full` sheets gated by
**G0_CHARACTERS**, per-shot keyframes gated by **G2_KEYFRAMES**, per-shot clips with
locked duration), ffmpeg-backed visual QC (`qc/visual.py`), deterministic clip conform
(`media/conform.py`), the prompt system + sanitizer (`media/prompts.py`,
`media/sanitizer.py`), a persistent `shot_attempts`/`character_sheets` schema
(migrations 005–006), media CLI commands (`preview`, `qc`, `audition`, `smoke`,
`shot list/regenerate`), H0 spend barriers in the engine (`pipeline/barrier.py`) and
DAG wiring (`pipeline/specs.py`).

## 1. Discovery Report (§C)

Written before any Phase 4 code, committed with this report as `PHASE4_DISCOVERY.md`
(134 lines; every answer names the files inspected; disagreements resolved
fail-closed). Headline findings that shaped the build:

- **Demo DAG gains `spends_money` (H0).** `demo_voice` stays free; `s5_tts`, `demo_keyframes`, `demo_clips` are paid; `demo_script`/`demo_finalize` free. Demo QC is persisted per asset as before.
- **Production DAG** now `text_specs() → preflight_specs() → audio_specs() → media_specs()`: `s4p → s5a → s5 → s6 → s7 → s7b → s8a(G0) → s8(G2) → s10p → s10` (`pipeline/specs.py`).
- **`StageSpec.spends_money` did not exist** before Phase 4 (H0); the engine refuses to execute a paid stage whose ancestor gate is pending — before `input_hash`/`run` — and the gateway carries an independent guard (`core/gateway.py`). `pipeline/barrier.py::ancestor_gate_problems` is the shared check used by the engine, crash recovery and the advance path.
- **`gate_after` existed but was unused for media**; Phase 4 attaches G0 (sheets) and G2 (keyframes) and reuses the Phase 2 gate round/snapshot machinery unchanged.
- **`provider_calls` gained `remote_handle_json`** (request/status/response/cancel URLs persisted in the same single statement as `remote_request_id`), and `shot_attempts`/`character_sheets` were added in 005; 006 widens the sheets `status` CHECK to allow `rejected`.
- **Sheets key off the *pinned bible character name*** (the identity S3 continuity validates), not the `characters` table — production episodes may have bible characters with no `characters` rows.

## 2. Test results by ID (§P)

Legend: ✅ pass in this sandbox (Linux) · ⏭️ skipped (opt-in/live) · ≈ composed of the listed tests · ✗ none.
Full suite (background run, ffmpeg-marked tests included): **533 passed, 3 skipped**
in 872 s. §-by-§ mapping:

### Hardening
| ID | Result | Where / notes |
|---|---|---|
| T-GATE-01 | ✅ 10/10 | `tests/test_phase4_gates.py` — parametrized over S5, S8a, S8, S10: pending ancestor gate ⇒ zero provider calls AND zero reservations; approved-then-ancestor-changed (snapshot mismatch) blocks again; gateway-level direct-call guard + exempt-context still works. |
| T-INT-01 | ✅ 1/1 | `tests/test_phase4_int.py` — fake-LLM S1–S4 → G1 approval (diacritized text) → S5a–S7b; TTS receives the G1-approved diacritized text; lines keyed; master exists. |
| T-CFG-05 | ✅ | `tests/test_config.py` — draft-profile creative role resolves to `llm.creative` (28/28 in file). |
| T-PRICE-02 | ✅ 4/4 | `tests/test_phase4_price.py` — S4 media estimate exact (TTS chars + video low/expected/high bands); unconfirmed ⇒ unavailable, never 0; S6 `estimate_drift` event past warn pct; per-second projection from locked durations. |
| T-CI-02 | ✅ 7/7 | `tests/test_phase4_ci.py` — capability list incl. new filters; `scripts/check_ffmpeg.py` shares the check with doctor; CI runs the ffmpeg gate before tests; coverage paths + 90% gate present (parsed from `ci.yml`); matrix/quality steps; `slow.yml` weekly + dispatch; markers declared. |
| T-REC-03 | ✅ 15/15 | `tests/test_phase4_rec.py` — crash matrix 7 points × {fal image, fal video} via `httpx.MockTransport`; only `after_submit_before_persist` re-pays (exactly one `potential_orphan` logged, nonce resume); resume reuses `remote_handle_json`, never the fallback construction. |

### fal adapter (offline; wire details unverified against the real service — see §6/§7)
| ID | Result | Where / notes |
|---|---|---|
| T-FAL-01 | ✅ 3/3 | `tests/test_phase4_fal.py` — URL/auth/param_map/formats/aspect_values/fixed_params; `fixed_params` cannot be overridden from a request; unconfirmed aspect value fails closed; a safety checker is never disabled. |
| T-FAL-02 | ✅ 2/2 | Submit/poll/fetch; handle (4 URLs + id) persisted in one statement; poll timeout ⇒ transient ⇒ requeue; resume keeps the same `request_id` with a single submit; result_map extraction; immediate download into CAS. |
| T-FAL-03 | ✅ 7/7 | Error taxonomy: 429+Retry-After / 5xx / timeout ⇒ transient; 403 balance ⇒ quota halt; 401 ⇒ permanent; 422 ⇒ policy, `billed=False`; nsfw flag ⇒ `billed=True`, nothing stored; failed job; empty download ⇒ ZeroByteAssetError; wrong content-type; COMPLETED-with-error ⇒ JobFailed. |
| T-FAL-04 | ✅ 3/3 | API key and signed query strings never appear in `provider_calls.request_json`, audit rows, `caplog` or exceptions; error-message scrubbing for signed URLs. |
| T-FAL-05 | ✅ 3/3 | `data:` URI size guard vs `max_inline_bytes`; upload transport (initiate→PUT→URL); `prepare_refs` deterministic (same bytes ⇒ same prepared sha). |
| T-FAL-06 | ✅ 2/2 | per-unit pricing math; `failed_job_billing` charge vs free. |
| T-FAL-LIVE-01/02 | ⏭️ skipped | Opt-in `STUDIO_LIVE_FAL=1`; never executed here; nothing live was called. |

### Prompts and sanitizer
| ID | Result | Where / notes |
|---|---|---|
| T-PRM-01 | ✅ 4/4 | `tests/test_phase4_prompts.py` — deterministic composition; locked parts byte-identical across sanitize levels; prompt-version sha in cache key and matches template bytes. |
| T-SAN-01 | ✅ 4/4 | refusal → level 1 → success; original+rewritten logged; cap yields `failed_policy` and parks; identical prompt never resubmitted; crash after refusal resumes at sanitized prompt with zero extra LLM calls. |
| T-SAN-02 | ✅ 4/4 | rewrite validation (empty / too long / new proper nouns) and lexicon whole-word case-insensitive match; falls through to the lexicon. |
| T-SAN-03 | ✅ 2/2 | sanitizer not invoked for transient errors or QC failures. |

### Sheets, keyframes
| ID | Result | Where / notes |
|---|---|---|
| T-SHT-01 | ✅ 2/2 | `tests/test_phase4_sheets.py` — missing (character, role) only; all-approved ⇒ zero calls, no gate; rejecting one character regenerates only it, with a cost check. |
| T-SHT-02 | ✅ 1/1 | `ref_face` conditioned on `ref_full`'s sha. |
| T-KF-01 | ✅ 3/3 | `tests/test_phase4_keyframes.py` — ref selection + clause composition; `max_refs` truncation ⇒ `refs_truncated` warning; changed sheet stales keyframes. |
| T-KF-02 | ✅ 3/3 | Native aspect via `aspect_values`; unsupported aspect fails s4p closed; aspect QC non-retryable (zero regenerations). |
| T-KF-03 | ✅ 2/2 | black image → `q1`,`q2` regenerations then fail (`qc.image.auto_retries=2`); non-retryable failures never retry; `media_circuit_open` audit written; run halts before any sibling shot is paid (fail-fast — see Deviations §4.1). |
| T-KF-04 | ✅ 2/2 | crash resume repays nothing; `chain_mode: last_frame` raises `ConfigError` before Phase 5. |
| T-KF-05 | ✅ 1/1 | `studio shot regenerate` touches only that shot; new nonce; shows cost; requires `--yes`; stales that shot's clip without purging the paid clip. |

### Clips and QC
| ID | Result | Where / notes |
|---|---|---|
| T-CLP-01 | ✅ 3/3 | `tests/test_phase4_media.py` — table-driven conform: exact / +1 frame / 3 frames short (freeze-pad) / long (trim) / 30 fps source → all 120 frames @ 24 fps; beyond tolerance ⇒ `duration_mismatch`; conformed clip audio-free. S6 invariant: locked `clip_duration_ms`, frame counts, speed/hold unchanged by regeneration (asserted via S6 columns snapshot in `T-ENG-14` + `test_clp_02_changing_video_model_stales_s6`). |
| T-CLP-02 | ✅ 2/2 | `tests/test_phase4_flow.py` — unsupported locked duration fails s10p closed ("re-run S6"); changing the video model stales S6. |
| T-CLP-03 | ✅ 2/2 | `tests/test_phase4_flow.py` — only the accepted keyframe sha feeds `input_assets[0]`/wire payload; clip regeneration does not touch the keyframe attempt. |
| T-CLP-04 | ✅ ≈ | composed: black clip (`test_qcv_01_video_checks`: `black_ratio` fail + `first_frame_luma` fail), freeze warn vs fail bands (`test_qcv_01_freeze_warn_vs_fail`, defaults 0.5/0.85), truncated file ⇒ `decode` fail, conformed audio stripped (`test_clp_01_conformed_clip_is_audio_free`), SSIM recorded against the keyframe (`test_qcv_01_ssim_recorded_against_keyframe`, plus `first_frame_ssim` block in `test_qcv_01_video_checks`). No separate `test_clp_04_*` exists. |
| T-CLP-05 | ✅ 1/1 | `tests/test_phase4_media.py` — raw clips retained `keep_raw_clips_days` (30) and conform never downgrades CAS retention (`conformed_asset_id` is a distinct asset; raw kept). |
| T-QCV-01 | ✅ 5/5 | `tests/test_phase4_media.py` — every image/video check against purpose-built bad lavfi inputs (black/wide/tiny/frozen/audio/short/fps25/truncated). |
| T-PRE-01 | ✅ 3/3 | `tests/test_phase4_flow.py` — s4p lists every problem at once (needs_confirmation, unverified price, zero price without acknowledgement, missing FAL_KEY, unsupported duration/aspect, ref-incapable); over-budget halts the run before any paid call (`halted_budget` + `budget_halted` audit); unconfirmed in final ⇒ preflight failure. |

### Integration
| ID | Result | Where / notes |
|---|---|---|
| T-ENG-14 | ✅ 1/1 | `tests/test_engine.py::test_demo_dag_through_s10_counts_and_formula` — sandbox demo DAG through S10 with real handlers + mock media; run COMPLETED; counts: TTS = approved lines, `image_reference_generate` = shots + missing_sheets×roles, `image_to_video` = shots; T-ENG-08 formula recomputed from repo state; locked `clip_duration_ms` unchanged after S10; conformed frame counts = `round(ms × fps / 1000)`; `demo_finalize` succeeded. ffmpeg-marked. |
| T-ENG-15 | ✅ 1/1 | `tests/test_phase4_flow.py` — production-shaped fake-LLM chain through S10; no paid call before its ancestor gate (S5/S8a/S8/S10 asserted). |
| T-ENG-16 | ✅ 1/1 | `tests/test_phase4_flow.py` — `--languages ar,en`: image/video/sheet counts unchanged; TTS and mix doubled. |
| T-CLI-04 | ✅ 5/5 | `tests/test_phase4_flow.py` — `shot list`/`shot regenerate` refusal without `--yes`; media preview escapes Arabic/HTML; `media qc` command; `media audition image` under mock transport; `doctor` media rows. |
| T-LANG-01 | ✅ extended | `tests/test_config.py` — lint now asserts the scan covers `studio.media`, `studio.qc`, `studio.video` in addition to prior scopes. |
| T-ENG-08 | ✅ modified | `tests/test_engine.py` — rewritten to the §M formula (image calls = shots + characters-without-approved-sheets × roles; measured from repo pre-drain state instead of a bare 3). |
| T-ENG-12 / T-MOCK-01 / T-CLI-03 etc. | ✅ | Phase 1–3 integration/demo tests still green (see full-suite counts). |

**Coverage:** see §3. **Process:** `ruff check src tests` → *All checks passed!*;
`mypy` (strict) → *Success: no issues found in 110 source files*;
`python scripts/check_ffmpeg.py` → *OK: ffmpeg 7.1.1; 31 required filters present*.
Live tests (T-FAL-LIVE-01/02, T-EL-06) are skipped; zero live calls.

## 3. Coverage

Command (the exact CI invocation):

```
python -m pytest tests/ -q -m "not slow" --cov=studio.core --cov=studio.db \
  --cov=studio.pipeline --cov=studio.audio --cov=studio.providers \
  --cov=studio.qc --cov=studio.video --cov=studio.media --cov-report=term-missing \
  --cov-fail-under=90
```

Result (this sandbox, 2026-11-17): **532 passed, 3 skipped, 1 deselected (T-MIX-05),
TOTAL 90.15% — "Required test coverage of 90% reached."** (Full suite without the
`-m "not slow"` filter: 533 passed, 3 skipped — the extra pass is T-MIX-05.)
Per-module, with the new/changed packages and the engine in full (new code highlighted):

| Module | Stmts | Miss | Cover | Module | Stmts | Miss | Cover |
|---|---|---|---|---|---|---|---|
| **media/conform.py** | 58 | 4 | **93%** | pipeline/engine.py | 377 | 42 | **89%** |
| **media/prompts.py** | 89 | 4 | **96%** | pipeline/barrier.py | 35 | 5 | **86%** |
| **media/refs.py** | 32 | 0 | **100%** | pipeline/deps.py | 30 | 3 | **90%** |
| **media/registry.py** | 78 | 13 | **83%** | pipeline/specs.py | 15 | 0 | **100%** |
| **media/sanitizer.py** | 63 | 14 | **78%** | pipeline/estimate_media.py | 179 | 20 | **89%** |
| **media/stages.py** | 982 | 181 | **82%** | qc/visual.py | 142 | 24 | **83%** |
| **providers/fal/adapter.py** | 336 | 69 | **79%** | video/analysis.py | 92 | 13 | **86%** |
| **db/repos/sheets.py** | 61 | 5 | **92%** | video/ffmpeg.py | 84 | 19 | **77%** |
| video/probe.py | 66 | 12 | **82%** | qc/audio.py | 173 | 10 | 94% |

All of the above feed a TOTAL of **90.15%**, so the CI gate passes. Modules still under
90% are dominated by (a) live/wire branches that cannot run offline (`fal/adapter.py`:
real response-body edge branches, HTTP transports) and (b) defensive error paths
(`media/stages.py`: crash-resume/refusal branches already exercised by T-KF-04/T-REC-03
but counted with their cold siblings). No per-module coverage gate is applied; the gate
is TOTAL ≥ 90 as in CI. Low-coverage files are not exempt from future ratcheting —
Phase 5 should lift `media/stages.py` over 85% before adding more stage logic.

## 4. Deviations (and why)

1. **Circuit breaker: fail-fast, not N-consecutive (§I/§J).** The spec's literal reading —
   `circuit_breaker_consecutive_failures` (3) consecutive shots may fail after retries
   before the stage fails with `MediaCircuitOpen` — is **not** implemented. The engine
   fails the stage_run **and the run** on the first post-retry shot failure
   (`_terminal_failure`); sibling shot jobs are then refused spend via the H0 barrier
   ("run is failed; refusing to execute …"). A single *non-retryable* QC failure raises
   `MediaCircuitOpen` directly in `_generate_with_retries` and the handlers write the
   `media_circuit_open` audit row (as spec'd). `media.circuit_breaker_consecutive_failures`
   is parsed and validated (`ge=1`) but read by no code path. Rationale, confirmed with
   the operator on 2026-11-17 (option kept: "Keep fail-fast, document deviation"): G2
   consumes keyframes and `deps_complete` blocks every downstream stage on a failed rows
   set, so a partially-failed keyframe stage has no defined G2/S10 semantics without a
   larger engine change; fail-fast is strictly cheaper on a broken endpoint, which is the
   spec's stated goal ("so a broken endpoint cannot burn money"), and already shipped
   behavior (T-KF-03: the run halts before any sibling shot is paid). The literal streak
   semantics additionally requires non-terminal SHOT-scope stages, a DB-tracked streak and
   defined G2 partial-failure behavior — deferred as follow-up work if the product wants
   to pay for sporadic-failure tolerance.
2. **`first_frame_ssim` records *luma* (Y) SSIM, compared at clip size.** x264's 420p
   chroma quantization scores U/V ≈ 0.2–0.55 even for pixel-identical content, so the
   aggregate `All` component measured ≈ 0.72–0.77 for "same frame" — inside reach of the
   `first_frame_ssim_warn: 0.6` default and useless for calibration. Luma separates cleanly:
   identical content ≈ 0.999, black frame ≈ 0.26 (both against `tests/test_phase4_media.py`
   fixtures). `parse_ssim` gained a `component` parameter (default `All`, backwards
   compatible) and `_ssim_first_frame` now (a) scales the keyframe to the clip frame with
   `scale2ref` — the spec's "frame-scaled copy" that the previous `scale=iw:ih` no-op was
   missing — and (b) returns the Y component. `first_frame_ssim_fail` remains 0.0
   (disabled) until calibrated against live output. Recorded values are not comparable to
   pre-release measurements (none exist; Phase 4 is unreleased).
3. **Conform pins `-r <render.fps>` and verifies conformed fps.** ffmpeg 7.1.1 muxes a
   filter-graph-encoded CFR stream with mdhd `duration = (n-1)/fps` (negative-DTS
   handling), so ffprobe reports `avg_frame_rate = 24.2` for a 120-frame 24 fps file and
   the `|fps-24| < 0.01` assertion could never pass. Pinning `-r` on the output gives
   exactly `24/1` and `duration = 5.000000`. Conform additionally **verifies the
   conformed fps** and raises `VisualQcError` on mismatch (previously only frame count
   was verified). The raw paid asset is still stored first and never re-generated.
4. **Mock 16:9 render size 480×270 → 960×540** (`settings.yaml mock.video_width_16x9 /
   video_height_16x9`): sandbox mock clips now satisfy `qc.image.min_short_side_px: 512`.
   No Phase 1–3 test asserted the old size.
5. **s4p clips-duration preflight tolerates unresolvable durations.** A registry video
   entry whose `supported_durations_s` raises `MediaConfigError` (or is empty) now yields
   a preflight problem line ("declares no supported durations") instead of crashing the
   stage before the problem list is shown.
6. **Migration 006 widens `character_sheets.status` to allow `'rejected'`.** The G0
   reject path writes `status='rejected'`; the original CHECK crashed with
   `IntegrityError`. Table-rebuild pattern, data copied, partial unique index and lookup
   index recreated; additive and exercised by `test_sht_01_rejecting_one_character…`.
7. **T-LANG-01 extended** to assert the scan covers `studio.media`, `studio.qc`,
   `studio.video`. No other Phase 1–3 assertion was changed or weakened. Lint-only:
   25 autofixable issues fixed (unused imports/import order/redefinitions), 5 stale
   `# noqa: S603` removed (S603 now ignored for tests), test files only.
8. **Lint configuration**: `tests/**` per-file-ignores += `S603`,`S607` (tests invoke the
   PATH-resolved `ffmpeg`/`ffprobe` by design, like the CI capability job).
9. **CI coverage paths extended** with `--cov=studio.qc --cov=studio.video
   --cov=studio.media --cov=studio.providers` (`.github/workflows/ci.yml`); the 90% gate
   is unchanged. Weekly `slow.yml` (T-MIX-05 ~105 s) from earlier steps unchanged.
10. **`PHASE3_REPORT`'s ffprobe stand-in caveat is retired for this run**: this
    environment provisions real ffprobe 7.1.1; Phase 3's probe auto-detect fix is now
    exercised against the real binary (`tests/test_probe_media.py`, 2 tests).
11. **README.md still said "Phase 3"** — it now carries a Phase 4 section (stages,
    commands, placeholders warning) and the headline/status were updated.
12. **CLI tests pin `COLUMNS=220`** (`T-CLI-04`) so the rich tables are asserted at a
    stable wide width (rich wraps at the negotiated terminal width; CI's default 80
    columns wrapped the assertions).
13. **Fail-fast for all stages is unchanged** from Phases 1–3 (`studio resume` flips
    `failed` rows back to `pending` and re-runs them).
14. **`spec.spends_money` + H0 barrier are additive**: no Phase 1–3 stage behavior
    changed; the barrier only *blocks* paid execution under a pending ancestor gate.

## 5. Decisions on ambiguities

- **G2 stays attached to `s8_keyframes`** (temporary; Phase 5 moves it to S9), per §I.
- **Sheets**: `ref_full` first (no refs, plain backdrop, full-body neutral pose, track
  aspect via `aspect_values`); later roles condition on the `ref_full` sha. One approved
  sheet per `(character_id, role)` enforced by a partial unique index; older approved
  rows supersede. Sheets key on the pinned bible character name (S3's identity), not the
  `characters` table. Rejecting one character regenerates only that character with a
  fresh nonce and a cost check; approving flips chosen rows to `approved`.
- **Keyframes**: native aspect via `aspect_values`, never crop/stretch; refs = approved
  `ref_full` (+ `ref_face` for close-ups if configured), up to `max_refs` (4);
  overflow `primary_only` + `refs_truncated` warning; output hashed into S10's inputs —
  a changed sheet stales keyframes, a regenerated keyframe stales that shot's clip but
  never purges the paid clip (`keep_raw_clips_days: 30`).
- **S6 hash includes the resolved video model key and its `supported_durations_s`** —
  changing the video model stales S6 (§J).
- **Conform**: `clip_frames = round(ms × fps / 1000)`; `fps=` normalize → trim → `tpad`
  freeze-pad when short ≤ `duration_tolerance_ms` (500) → SAR 1 → libx264 `crf 14`
  yuv420p → `-an`; already-compliant raws referenced without re-encode; frame count and
  fps verified post-encode (mismatch raises).
- **Crash resume repays nothing**: attempt nonce persisted before the first submit
  (`_ensure_attempt`, one statement with the prompt spec); the gateway adopts the
  in-flight call (cached `provider_calls` row / same nonce) instead of re-paying; a
  regeneration rotates `base.qN`; a resume keeps the nonce. Verified by T-KF-04, T-REC-03,
  T-ENG-14 (`provider_calls` cache_key uniqueness per call).
- **Sanitizer**: invoked only for policy refusals; level 1 light rewrite → level 2
  lexicon (`prompts/sanitizer_lexicon.yaml`, whole-word case-insensitive) + stronger
  rewrite; LLM called via the gateway as a single-call provider with `san{level}` nonce;
  rewrites validated (non-empty, ≤ `max_scene_chars`, no new proper nouns) — failure
  falls to the next level; after `max_levels: 2` the shot parks as `failed_policy` and a
  human edits via `studio shot regenerate --prompt-file`. Crash after a refusal resumes
  at the sanitized prompt (persisted `prompt_spec_json`) with zero extra LLM calls.
- **`first_frame_ssim_fail` stays 0.0** (disabled) until §Q data exists; warn stays 0.6.
- **`chain_mode: off`**; `last_frame` raises `ConfigError` because Phase 5 owns chaining.
- **SSIM parser**: `parse_ssim(stderr, component=…)`; first-frame QC uses `Y`
  (see Deviations §4.2). All other call sites keep the old `All` default.
- **estimate_drift reused** from the S6 helper (`_estimate_drift`) for the
  projected-vs-S4 check in the G2 output; `projected_clip_cost_musd_first_pass /
  _with_retries` computed exactly from S6's locked durations × price (per-second or
  per-video), retrying projection via `assumed_retry_rate.video` (0.30).
- **No visual-LLM validation anywhere**; aesthetically and character-wise everything is
  G0/G2/G3 human judgment (spec'd).
- **`failed_job_billing: charge`** kept as the default (unverified; reserves the worst
  case so the budget guard never under-reserves a flagged/failed job).
- **Budget semantics**: caps are never auto-lowered (`ensure` is INSERT OR IGNORE); the
  over-budget preflight test forces the halt deterministically by setting the episode cap
  to 1 micro-USD via `ensure` + `reset_kill_switch`.
- **Windows CI is configured but was never executed here** (Linux sandbox); the suite
  auto-skips ffmpeg-marked tests when binaries are absent.

## 6. Known limitations (read before believing anything about this phase)

- **Nothing live has been run.** No `FAL_KEY` exists in this environment; every fal wire
  detail (submit/poll/fetch shapes, error mapping, nsfw handling, failed-job billing,
  real prices, fal-side `max_concurrency` enforcement) is **unverified against the real
  service** and is only exercised through `httpx.MockTransport` and mock providers.
  Registry fal entries ship as `<CONFIRM_FAL_ENDPOINT_ID>` placeholders with
  `needs_confirmation: true` and no verified price, on purpose; `studio doctor` WARNs on
  each and the final profile refuses to run until an operator confirms them.
- **QC thresholds are uncalibrated starting points** (black/freeze ratios, luma, SSIM
  warn, min bytes, SQ). The §Q smoke writes QC values (incl. SSIM and freeze ratios) to
  `data/previews/media_smoke/<ts>/` precisely so an operator can calibrate them.
- **Circuit breaker is fail-fast, not N-consecutive** (Deviations §4.1; user decision).
- **SSIM luma change** invalidated cross-version comparisons (no recorded values exist).
- **ffmpeg-version sensitivity**: the `-r` conform pin and the `avg_frame_rate` behavior
  are documented for ffmpeg 7.1.1; CI's capability check pins the filter set, not the
  version. Older builds may probe slightly different `avg_frame_rate`.
- **Windows leg unobserved locally**; the capability gate runs there identically.
- **T-MIX-05 (~105 s, sox-free mastering integration)** runs weekly via `slow.yml` and
  on demand, not in the per-PR job.
- **`first_frame_ssim_fail: 0.0`** — the SSIM check only warns today.
- **`assumed_retry_rate`** affects projections only, never reservations.
- **T-FAL-LIVE-01/02, T-EL-06** never executed (opt-in flags unset); their skip is the
  expected state for CI.
- **G0/G2 snapshots**: gate snapshot hashing reuses the Phase 2 machinery; approving a
  G0 round supersedes older approved rows (audit logged), it does not diff images.


## 7. Commands — the confirmation → smoke → draft-run flow (Linux and Windows)

Linux / macOS:

```bash
# 0) offline verification (this sandbox)
ruff check src tests
mypy
python -m pytest tests/ -q
python -m pytest tests/ -q -m "not slow" --cov=studio.core --cov=studio.db \
  --cov=studio.pipeline --cov=studio.audio --cov=studio.providers \
  --cov=studio.qc --cov=studio.video --cov=studio.media --cov-fail-under=90
python scripts/check_ffmpeg.py        # OK: ffmpeg 7.1.1; 31 required filters present
studio doctor                         # FAL_KEY WARN + placeholder endpoint WARNs (by design)

# 1) confirm the registry (operator task; nothing below runs before this)
#    edit configs/model_registry.yaml: model_ref, pricing (pricing_verified_on),
#    commercial_use_verified, needs_confirmation: false — for every image.* / video.* entry
studio doctor                         # must show no media WARN before spending

# 2) audition (opt-in, spends money; prints worst-case total, requires --yes)
export FAL_KEY=...
studio media audition image --model-keys image.ref.draft --prompts-file prompts.txt --cap-usd 0.50 --yes
studio media audition clip --model-keys video.i2v.draft --image /path/to/keyframe.png --cap-usd 1.50 --yes

# 3) live smoke (§Q; never in CI) — 1 character, 2 shots, 5 s each, 9:16, draft profile
#    writes data/previews/media_smoke/<ts>/ (HTML preview, per-call costs, QC values
#    incl. SSIM and freeze ratios, conform reports) and prints remote request ids
studio media smoke --cap-usd 3.00 --yes

# 4) opt-in live unit tests
STUDIO_LIVE_FAL=1 python -m pytest tests/test_phase4_fal.py -k live -q

# 5) draft production run (mock or confirmed registry; auto-approve on draft)
STUDIO_LIVE_LLM=1 studio run --production --profile draft
studio worker --until-idle
studio gates approve <G0-gate-id>   # if sheets were generated
studio worker --until-idle
studio gates approve <G2-gate-id>
studio worker --until-idle          # through S10; demo_finalize/assembler follow
studio media preview <run-id>
```

Windows (PowerShell):

```powershell
$env:FAL_KEY = "..."
ruff check src tests; mypy
python -m pytest tests\ -q
python -m pytest tests\ -q -m "not slow" --cov=studio.core --cov=studio.db `
  --cov=studio.pipeline --cov=studio.audio --cov=studio.providers `
  --cov=studio.qc --cov=studio.video --cov=studio.media --cov-fail-under=90
python scripts\check_ffmpeg.py
studio doctor
studio media smoke --cap-usd 3.00 --yes
$env:STUDIO_LIVE_FAL="1"; python -m pytest tests\test_phase4_fal.py -k live -q
```

**Observed in this sandbox (2026-11-17):** full suite **533 passed / 3 skipped**
(872 s; ffmpeg-marked tests ran against the real ffmpeg 7.1.1); CI-flag coverage run
(§3) passed with the 90% gate; ruff clean; mypy strict clean (110 files);
`check_ffmpeg.py` OK; `studio doctor` prints the designed placeholder WARN rows
(image.*/video.* endpoints, `failed_job_billing`) and PASS rows (ffmpeg visual filters,
chain_mode, LLM rows); zero live calls; `STUDIO_LIVE_FAL` unset; nothing spent.
**Not observed here:** Windows CI, the §Q live smoke, any live fal behavior,
T-FAL-LIVE-01/02, T-EL-06.

## 8. Files

**New (this phase, excluding `__pycache__`):**
```
.github/workflows/slow.yml            # weekly T-MIX-05 + capability gate
PHASE3_AUDIT.md                       # Phase 3 pre-flight audit (probe.py fix)
PHASE4_DISCOVERY.md                   # §C Discovery Report (this file is §S)
PHASE4_REPORT.md                      # this report
configs/prompts/media/                # keyframe/motion/negative prompts, sanitizer prompt + lexicon
src/studio/db/migrations/005_media.sql    # remote_handle_json, shot_attempts, character_sheets
src/studio/db/migrations/006_sheets_rejected.sql  # status CHECK widened to 'rejected'
src/studio/db/repos/sheets.py             # sheets repo (precheck/insert/approve/supersede/reject)
src/studio/media/__init__.py
src/studio/media/conform.py               # §J conform (fps pin, trim/pad, verified)
src/studio/media/prompts.py               # deterministic composition, sha, seed, spec dataclass
src/studio/media/refs.py                  # deterministic ref prep (CAS → JPEG ≤ 1024px)
src/studio/media/registry.py              # media model entry resolution + confirmation problems
src/studio/media/sanitizer.py             # levels, lexicon, validation, refusal log
src/studio/media/stages.py                # s4p, s5-adjacent glue, S8a, S8, s10p, S10, shared retry policy
src/studio/qc/visual.py                   # ffmpeg/ffprobe visual QC (image/video)
src/studio/video/analysis.py              # parse_ffprobe_json, parse_ssim(component), signalstats, freeze/blackdetect
src/studio/pipeline/barrier.py            # ancestor_gate_problems (H0)
src/studio/pipeline/deps.py               # recursive deps_complete / direct_deps_complete
src/studio/pipeline/estimate_media.py     # S4 exact media estimate + S6 drift helper reuse
src/studio/pipeline/specs.py              # preflight_specs / audio_specs / media_specs / production_specs
src/studio/providers/fal/__init__.py
src/studio/providers/fal/adapter.py       # raw httpx; submit/poll/fetch; param_map/formats/aspect_values; MockTransport-clean
tests/media_env.py                      # shared env builder, providers (spy/black/wide), CAS helpers
tests/test_phase4_ci.py                # T-CI-02 (7)
tests/test_phase4_fal.py               # T-FAL-01..06 + live (skipped) (22)
tests/test_phase4_flow.py              # T-PRE-01, T-CLP-02/03, T-ENG-15/16, T-CLI-04 (14)
tests/test_phase4_gates.py             # T-GATE-01 (4 → 10 parametrized)
tests/test_phase4_int.py               # T-INT-01 (1)
tests/test_phase4_keyframes.py         # T-KF-01..05 (11)
tests/test_phase4_media.py             # T-CLP-01/05, T-QCV-01 (9)
tests/test_phase4_price.py             # T-PRICE-02 (4)
tests/test_phase4_prompts.py           # T-PRM-01, T-SAN-01..03 (14)
tests/test_phase4_rec.py               # T-REC-03 (2 → 15 parametrized)
tests/test_phase4_sheets.py            # T-SHT-01/02 (3)
tests/test_probe_media.py              # real-ffprobe auto-detection regressions (2)
```

**Modified:** `.github/workflows/ci.yml` (coverage paths) · `README.md` (Phase 4 section)
· `configs/model_registry.yaml` (all media entries, **placeholders by design**) ·
`configs/settings.yaml` (mock 16:9 → 960×540; fal/media/sanitizer/character_sheets keys) ·
`pyproject.toml` (test lint ignores) · `scripts/check_ffmpeg.py` (visual filter set) ·
`src/studio/audio/timeline.py` (S6 ships `clip_duration_ms` frames for media) ·
`src/studio/cli.py` (doctor media rows, `media preview/qc/audition/smoke`,
`shot list/regenerate`, `media qc`) · `src/studio/config/{registry,settings}.py`
(media resolution + validation: `chain_mode`, ratios, `black_fail_ratio`, price
units per_image/per_second/per_video/per_megapixel) · `src/studio/core/gateway.py`
(remote_handle_json persisted in one statement; paid-stage guard; media calls) ·
`src/studio/core/pricing.py` (media unit pricing + failed_job_billing) ·
`src/studio/db/repos/{__init__,content,providers}.py` (sheets access, attempts,
ledger queries) · `src/studio/demo/stages.py` (demo H0: paid flags on
keyframes/clips) · `src/studio/domain/{errors,models}.py` (MediaCircuitOpen et al.;
attempt/sheet rows) · `src/studio/pipeline/{context,dag,engine,gates,recovery,stage,
worker}.py` (spends_money wiring, barrier calls, stale/requeue via seeds, G0/G2 gate
attach, resume sweep) · `src/studio/pipeline/stages_audio.py` (S6 hash carries video
model key + durations; TimingReconcilerHandler import path) ·
`src/studio/pipeline/stages_text.py` (text-stage wiring for media: estimates, S6) ·
`src/studio/providers/{factory,mock/image,mock/video}.py` (mock media providers
honoring the media contract) · `src/studio/video/{ffmpeg,probe}.py` (image probe
auto-detect; bytes/truncation handling) · `tests/test_engine.py` (T-ENG-14 added;
T-ENG-08 → §M formula) · `tests/test_config.py` (T-LANG-01 extended) ·
`tests/test_audio_engine.py`, `tests/test_audio_mix.py`, `tests/test_llm_adapters.py`,
`tests/test_mocks_cli.py`, `tests/test_phase3_coverage.py`,
`tests/test_regression_phase2.py` (lint-only: stale noqa removal / import sorting).

Phase 1–3 contracts otherwise untouched: `BEGIN_PROMPT.md`, Phase 2 schemas/prompts, XML
configs, S1–S7 behavior, gate machinery semantics. No new runtime dependencies.

## Unverified register

Every fal wire detail is unverified against the live service (no `FAL_KEY` here). Each row
says where the *assumed* behavior is pinned by tests; those tests verify the adapter's
handling of the assumption, not fal's real behavior. Nothing on this list may be treated
as observed.

| # | Unverified item | Where pinned / expected handling |
|---|---|---|
| 1 | Submit URL shape `{queue_base_url}/{endpoint}` and auth header `Key <FAL_KEY>` (subpath endpoints "suspected to differ") | `providers/fal/adapter.py`; `T-FAL-01` (MockTransport asserts exact URL/header). Fallback URL construction logged if the response omits `status_url`/`response_url`. |
| 2 | Submit response carries `request_id, status_url, response_url, cancel_url` | `adapter.py` + `core/gateway.py` persist all four in ONE statement with `remote_request_id` (`T-FAL-02`); missing id ⇒ permanent failure. |
| 3 | Poll statuses `IN_QUEUE \| IN_PROGRESS \| COMPLETED` and an `error` field on `COMPLETED` | `adapter.py`; `T-FAL-02` (lifecycle), `T-FAL-03` (COMPLETED+error ⇒ JobFailed). |
| 4 | `max_wait_s: 900` exceeded ⇒ call stays `submitted`, job requeued, same id on resume — never cancelled, never resubmitted | `T-FAL-02` (`test_fal_02_poll_timeout_raises_transient_and_resume_keeps_request_id`); engine requeues with same request id. |
| 5 | HTTP taxonomy: 429 (Retry-After) / 5xx / timeout ⇒ transient; 403 balance ⇒ quota halt; 401 ⇒ permanent | `T-FAL-03`; gateway maps to `TransientProviderError` / `ProviderQuotaExceeded` / `PermanentProviderError`; 403 sets episode `halted_budget` with `reason: provider_quota`. |
| 6 | 422 content-policy response ⇒ `billed=False`; nsfw flag on a COMPLETED job ⇒ `billed=True`, no asset stored | `T-FAL-03` (`test_fal_03_422_policy_is_not_billed`, `test_fal_03_nsfw_flag_billed_and_nothing_downloaded`). |
| 7 | Failed/flagged job billing semantics (`failed_job_billing: charge \| free`) — "⚠ unverified how fal bills failed/flagged jobs" | `T-FAL-06` (charge vs free accounting); default `charge` reserves worst case; `studio doctor` WARNs on the setting. |
| 8 | Error message bodies must not leak the key or signed URLs (fal download URLs are signed) | `T-FAL-04` — request_json/audit/caplog/exception paths all scrubbed; `build_fal` reads the key from `fal.key_env` only. |
| 9 | Pricing units (`per_image`, `per_video`, `per_second`, `per_megapixel`) and the placeholder prices in the registry | `pricing.py`; `T-FAL-06`, `T-PRICE-02`, `T-ENG-14` (counts); registry entries are placeholders with `needs_confirmation: true` — no real price is claimed. |
| 10 | Download-once semantics: asset fetched immediately at fetch() into CAS; a missing/empty body ⇒ ZeroByteAssetError; content-type must be `image/*` or `video/*`; download retried (`fal.download_retries: 3`) with a fresh `response_url` re-GET once | `T-FAL-02`, `T-FAL-03` (empty download, wrong content-type). |
| 11 | `max_concurrency: 1`, `min_interval_s: 1.0` per model honored by fal | Declared in registry; enforced locally by rate limiter + gateway; fal-side enforcement unverified. |
| 12 | `param_map` dotted paths with integer indices, `result_map` extraction, `fixed_params` immutable | `T-FAL-01`. |
| 13 | Whether `aspect_values` covers 9:16/16:9 for the actual endpoints | Registry placeholders list `9:16,16:9` for both draft and final; unconfirmed ⇒ preflight refuses (fail-closed) in final. |
| 14 | Media QC thresholds (`min_bytes`, `min_short_side_px`, luma mean/stddev, `black_fail_ratio`, freeze warn/fail, `first_frame_*`, SSIM metric choice) | Unit-tested on synthetic lavfi inputs (`T-QCV-01`) and one encoded-baseline comparison; **not calibrated** against real model output — needs §Q smoke data. |
| 15 | `assumed_retry_rate` (image .25 / video .30) for with-retries projections | Used in G2 projection only; budget reservations use locked durations × price without retry inflation. |
| 16 | ElevenLabs TTS live behavior (T-EL-06) — out of scope this phase | Skipped here; Phase 3 register carries it. |
| 17 | Crash behavior at the 7 injection points for fal calls (`after_submit_before_persist`, `before_download`, …) | `T-REC-03` (15) — only that one point can re-pay; exactly one `potential_orphan` audit row is written; resume never re-GETs a stale signed URL without the recorded handle (`test_rec_03_resume_reuses_persisted_handle_not_fallback`). |
| 18 | Retention semantics under retention job | Conform writes a distinct intermediate asset; raw clips retained `keep_raw_clips_days`; `T-CLP-05`. |
| 19 | fal's actual behavior for `submit` when the endpoint id is a placeholder | Refused before any call: registry resolution raises `RegistryError` (surfaces as preflight problems / `MediaConfigError` at stage start); `T-PRE-01` (lists-problems test) asserts fail-closed. |
| 20 | Settlement/refund on a 429'd *paid* submission that never produced a job | Untestable offline; `failed_job_billing` and the worst-case reservation are the mitigations; **explicitly unverified**. |
| 21 | `NPX/Windows` ffmpeg builds probing `avg_frame_rate` like 7.1.1 | The `-r` pin + post-encode fps verification makes conform robust regardless; CI capability check asserts the filter set only. |
| 22 | `StudioMediaLive` end-to-end (G0 → G2 → S10) with real fal | §Q smoke; never executed here (no key); report must not claim otherwise. |

