# Phase 3 Report — Voice and Audio (ElevenLabs TTS + local audio engine)

Branch `arena/a59ed87d-ai-studio` @ Phase 2 base `4fdf904`. Step-1..6 work committed in `18592e6`; Step-7 additions (S5/S6 integration tests, README, this report, CI wiring) are on top of `18592e6`.
Date: 2026-11-17. Operator: AI Director pipeline (this build).

**Result: all §I tests pass on Linux (this sandbox), coverage 92.97% (gate ≥ 90%), ruff clean, mypy strict clean (94 files). One test skipped (T-EL-06, opt-in live spend). One test deselected in CI (T-MIX-05, slow; passes on demand in 105.68 s). Windows is covered by CI, not executed here. No live ElevenLabs call has been made; every live-API claim below is marked unverified.**

## 1. Discovery Report (§C)

1. **Gate after which Gate 1 is complete.** Gate 1 (`G1_SCRIPT`) pauses the run; stages after a gated stage simply do not start until the gate clears (the engine never enumerates past a pending gate). In the **demo DAG**, `G1_SCRIPT` is attached to `demo_script`; `s5a_tts_preflight` depends on `demo_script`, so S5a never spends before Gate 1 approval — structurally, with **no gate field on S5a**. In the **text (production) DAG**, S3 `shot_plan` carries `G1_SCRIPT`; the audio stages are deliberately **not** wired into the text DAG — production stays S1–S4 until Phase 4 (same precedent as Phase 2's D7). S6/S7/S7b therefore run only on the sandbox/demo profile today.
2. **`narration_lines` at Gate 1.** Lines are inserted by the voice/script stage with `status='pending'` and all asset/conform columns NULL; `text_final` is NULL (it is written only by `studio narration edit` after Gate 1). S5 sets `status='voiced'` via `mark_line_voiced` on success. Arabic lines carry tashakeel from S2/S3 unchanged (Phase 3 never edits text).
3. **`sfx_tags`.** The shot schema has **no `sfx_tags`** (not in `shots`, not in S3 output). Per §H8 the S7 plan therefore emits **transition-tagged SFX at scene boundaries only** and **never invents per-shot SFX** — reported here as required, S2 prompts untouched. `sfx_tags`/`ambience_tags` (≥ 6 each, incl. `transition`) and `mood_affinity` were added to `vocab.yaml` for **library** tagging instead.
4. **Adapters.** Anthropic/OpenAI LLM adapters use their vendor SDKs (lazy imports, Phase 2). The ElevenLabs TTS adapter uses raw `httpx.AsyncClient` with an **injectable transport** (`httpx.MockTransport` in tests); it is the only new `httpx` consumer in `src/`. Mock providers remain in-process.
5. **Migration number.** `004` (003 = `series_bible`). `vocab.yaml`: only additions (`sfx_tags`, `ambience_tags`, `mood_affinity`); existing keys untouched. `settings.yaml`: `timing:` **merged** (existing `words_per_second`, `lead_in_ms`, `line_gap_ms`, `tail_ms` kept; added `max_stretch=1.15`, `max_hold_ms=500`, `j_cut_ms=0`, `prefer_stretch_to_save_cost=true`, `estimate_drift_warn_pct=25`), `tts:`/`audio:`/`doctor:` blocks added per §G. `j_cut_ms != 0` raises `ConfigError` at settings load (`Settings._check_cross_field`) **and** at the S6 entry point. No conflicts found; no existing key was overwritten.
6. **S4 estimator / Gateway.tts.** Phase 2's S4 stores **no TTS character estimate at all** (`production.py` projects LLM tokens only). D4 was therefore implemented as: S5a's preflight reservation sums `len(text_to_send)` exactly as S5 will send (post `tts.keep_diacritics`) and enforces it against the remaining budget (`BudgetExceeded`, before any call); S6 emits `projected_video_seconds`/`projected_video_cost_musd` into its output JSON and compares against an **optional** baseline (`metadata_safety` stage output keys `projected_video_cost_musd` / `gate1_video_estimate_musd`) — absent in this codebase, so the drift comparison is skipped unless Phase 2 is later extended (tested via a crafted S4 output, T-S6-08). For single-call providers (LLM, TTS), `ProviderGateway` persists `submit_started_at` (one statement) before the HTTP call; on resume a `reserved` call with `submit_started_at` and `attempt >= 1` reuses the reservation, appends a `potential_orphan` audit event, and re-invokes — a documented duplicate-risk residual, identical in kind to job providers.
7. **`G3_FINAL_CUT` rounds (report only; unchanged).** Gate approval is per gate **row** (`gates approve <gate-id>`), and `(run_id, stage, round)` + `snapshot_hash` of the stage's outputs is the identity. `gates.latest(run_id, stage)` and `_maybe_gate_stage` key on **stage name, not gate kind**: approving the `metadata_safety` round can never satisfy a final-cut round — they are different `stage` rows with different snapshots. Same-kind sharing is therefore safe **for approval**, with one caveat: any tooling/query that filters only by `gate='G3_FINAL_CUT'` sees both kinds of rounds (Phase 5 note). Additionally, observed in the sandbox DB (documented, not changed): jobs enqueued while a gate is pending can still execute, and on gate approval the engine invalidates speculatively-completed stage_runs and re-arms them (`staleness_invalidated`). The finding's recommended hard test ("final-cut approval requires a round whose subject is a render") was **not** added — a final-cut stage does not exist until Phase 5; it belongs to the Phase 5 prompt.

### Finding #6 (16 vs 14 crash scenarios)

Composition today: **14 matrix scenarios** (7 job crash points × image/video mocks, `test_crash_matrix.py`) + **8 defined crash points** in `core/faults.CRASH_POINTS` — the 8th, `during_single_call`, is the Phase 3 D2 addition for single-call providers (covered by `T-REC-02`: `before_submit`/`during_single_call` × LLM/TTS, plus the cache-hit resume test). I cannot reconstruct the Phase 2 "16" exactly from this tree; the 14-scenario matrix + 2 extra resume-path tests is the likely shape. The Phase 3 delta is deliberate: one new crash point, single-call providers only, zero double-submit except the two documented points (`after_submit_before_persist`, `during_single_call`), each logging exactly one `potential_orphan`.

## 2. Test results by ID (§I)

Legend: ✅ pass in this sandbox (Linux, ffmpeg 9.0.2 + ffprobe stand-in — see §6) · ⏭️ opt-in/skipped · ≈ composed of the listed tests (no single test with that exact ID) · ✗ changed scope (see §4/§5).
All offline: zero real provider spend; `STUDIO_LIVE_TTS` unset. Full suite: **412 collected → 410 passed, 1 skipped (T-EL-06), 1 deselected (`-m "not slow"`: T-MIX-05); T-MIX-05 passes on demand (105.68 s).**

| ID | File / test | Result |
|---|---|---|
| T-EL-01 | tests/test_elevenlabs.py (request built: URL, `xi-api-key` header, body fields, injectable transport) | ✅ |
| T-EL-02 | ::error mapping incl. `detail.status` rows (`too_many_concurrent_requests`, `system_busy`, `quota_exceeded` → `ProviderQuotaExceeded`, 4xx → permanent, empty-200 → `ZeroByteAssetError`, timeout/conn → transient) | ✅ (mapping as specified; **live error bodies unverified**, §6) |
| T-EL-03 | ::key never in `request_json`, audit rows, caplog, or exception messages | ✅ |
| T-EL-04 | ::`billed_chars` from `character-cost` header when integer, else `len(text)`; settled cost = `ceil(chars × price/1000)` integer µ$ | ✅ |
| T-EL-05 | ::quota → run `halted_budget`, audit `provider_quota`, no retry | ✅ |
| T-EL-06 | ::live Arabic sentence, $0.50 cap, request-id print | ⏭️ skipped (`STUDIO_LIVE_TTS=1` + key required) — **never executed** |
| T-LIB-01 | test_audio_engine.py::test_library_scan_registers_and_validates + ::test_library_scan_idempotent_rename_and_delete | ✅ |
| T-LIB-02 | test_audio_engine.py::test_library_loudness_within_half_lu | ✅ |
| T-TL-01 | test_audio_engine.py::test_timeline_deterministic_and_spans_contiguous + ::test_timeline_hash_changes_when_line_changes (`effective_frames` formula covered in table cases) | ✅ |
| T-TL-02 | test_audio_engine.py::test_timeline_spans_identical_across_languages (deliverables of one track) | ✅ |
| T-TL-03 | test_audio_engine.py::test_timeline_overflow_raises_exact_numbers + ::test_timeline_fps_must_divide_sample_rate | ✅ |
| T-CONF-01 | test_audio_engine.py (silence trim ±10 ms, gain clamp, peak ≤ −1 dBFS, DB samples == WAV header) | ✅ |
| T-S5-01 | test_timing_s6.py::test_t_s5_01_preflight_lists_all_missing_voices + ::test_t_s5_01_preflight_halts_over_budget_before_first_call | ✅ |
| T-S5-02 | test_timing_s6.py::test_t_s5_02_crash_mid_s5_restart_repays_nothing (`tts_nonce` persists across crash) | ✅ |
| T-S5-03 | test_timing_s6.py::test_t_s5_03_quality_failure_retries_then_raises (exactly `auto_retries`=1 paid regeneration w/ new nonce, then `TtsQualityError`; earlier lines unpaid) | ✅ |
| T-S5-04 | test_timing_s6.py::test_t_s5_04_context_truncated_and_neighbor_keys + ::test_t_s5_04_keep_diacritics_false_strips_and_changes_key | ✅ |
| T-S5-05 | ≈ T-ENG-12 (`--languages ar,en` doubles TTS + mix calls) + T-LANG-01/02 (no literal language codes; `src/studio/audio/**` clean) | ✅ (composed; no single test ID) |
| T-S6-01…05 | test_timing_s6.py::test_t_s6_01_to_05_duration_selection (params need 4.2/5.6 s-squeeze/6.2 s-floor-hold→d=5@1/1.15/6.4 s→d=10) + ::test_t_s6_02_squeeze_uses_speed_ratio + ::test_t_s6_03_prefer_stretch_false_uses_plain_duration + ::test_t_s6_04_speed_floor_and_bounded_hold | ✅ |
| T-S6-06 | test_timing_s6.py::test_t_s6_06_overflow_reports_all_shots (one `TimingOverflowError` listing every overflowing shot; narration text byte-identical after) | ✅ |
| T-S6-07 | test_timing_s6.py::test_t_s6_07_longer_language_drives_duration | ✅ |
| T-S6-08 | test_timing_s6.py::test_t_s6_08_estimate_drift_audit_event (crafted S4 baseline; projected seconds + cost in output JSON; `estimate_drift` past 25%) | ✅ |
| T-S6-09 | test_timing_s6.py::test_t_s6_09_narration_edit_and_resume_revoices_one_line (`narration edit --file`, UTF-8 Arabic, `post_gate_edit` audit w/ old/new sha256, `resume` re-voices only the edited line; UTF-8 round-trip on Windows not executed here) | ✅ (Linux) |
| T-S6-10 | test_timing_s6.py::test_t_s6_10_j_cut_ms_rejected (`ConfigError` at settings load; defense-in-depth re-check at S6 entry) | ✅ |
| T-SEL-01 | test_audio_plan.py::test_rank_tracks_is_deterministic_and_hash_tiebroken (no RNG; seed is per episode → same music across languages) | ✅ |
| T-SEL-02 | ::test_recent_use_penalty_steers_away_from_recent_tracks + ::test_mood_neighbor_affinity_outranks_zero_match + ::test_any_mood_match_negative_when_all_zero + ::test_empty_music_library_raises (`LibraryEmptyError`) | ✅ |
| T-SEL-03 | ::test_loopable_music_covers_full_segment_and_fades + ::test_nonloopable_chain_overlaps_and_coverage_warning + ::test_music_segments_merge_rules + ::test_ambience_segments_merge_equal_tag_sets + final fade at exactly `total_samples` | ✅ |
| T-SEL-04 | ::test_select_transition_sfx_picks_transition_tagged_deterministically + ::test_transition_sfx_placed_at_later_scene_boundaries (no per-shot SFX invented without `sfx_tags`) + ::test_no_mood_match_uses_closest_intensity_and_warns | ✅ |
| T-ENV-01 | test_audio_mix.py::test_env_01_envelope_2000_points_matches_reference_within_0_3db (balanced O(log n) decision tree; `asetnsamples=n=480`) | ✅ |
| T-DUCK-01 | test_audio_mix.py::test_duck_01_envelope_mode_attenuates_speech_windows + ::test_duck_01_short_gap_stays_ducked | ✅ |
| T-DUCK-02 | test_audio_mix.py::test_duck_02_sidechain_mode_attenuates_speech_windows | ✅ |
| T-STEM-01 | test_audio_engine.py/test_audio_mix.py (`T-STEM-01`): stem exactly `total_samples`; each line byte-compared at `start_sample`; else zeros; stdlib `wave` only | ✅ |
| T-MIX-01/02 | test_audio_mix.py::test_mix_01_and_02_sandbox_master_row_flacs_and_loudness (exact samples, stereo 24-bit FLAC, `audio_masters` row, −14 ± 1 LUFS, TP ≤ −1.0 dBTP via ebur128) | ✅ |
| T-MIX-03 | ::test_mix_03_resume_with_unchanged_inputs_reuses_same_master + ::test_mix_03_changed_cue_supersedes_master | ✅ |
| T-MIX-04 | ::test_mix_04_oversized_graph_renders_via_filtergraph_file (>40 k-char graph via file-option path) | ✅ |
| T-MIX-05 | ::test_mix_05_synthetic_20_minute_timeline_mixes_and_passes_qc — `@pytest.mark.slow`, **excluded from CI**, run on demand: **PASSES (105.68 s)** | ✅ (on demand) |
| T-QCA-01 | test_audio_mix.py::test_qca_01_* (unreadable/empty, exact length, narration presence, loudness, true peak + clip peak, duck depth, speech-to-music, plan coverage (beyond-timeline / empty-plan), license completeness) — each purpose-built bad input fails and persists `qc_results` | ✅ |
| T-ENG-12 | test_audio_mix.py::test_eng_12_sandbox_ar_vs_ar_en_counts (sandbox `['ar']` end-to-end; `['ar','en']` doubles TTS + mix counts; image/video counts unchanged; video timelines identical) | ✅ |
| T-ENG-13 | test_audio_mix.py::test_eng_13_crash_in_s7b_resumes_with_zero_tts_repaid (`SimulatedCrash` in S7b → restart → completes, zero TTS repay) | ✅ |
| T-CLI-03 | test_mocks_cli.py::test_cli_03_library_scan_voice_set_run_preview_doctor (playable preview + doctor checks pass) | ✅ |
| T-CAS-03 (D1) | test_phase3_hardening.py (retention upgrade both orders, keep_n_days later `purge_after` wins, never downgrade) | ✅ |
| T-REC-01 (D2 matrix) | test_crash_matrix.py (14 scenarios, zero re-paid except the two documented orphans; `during_single_call` excluded from job matrix) | ✅ |
| T-REC-02 (D2) | test_phase3_hardening.py::test_t_rec_02_single_call_crash_resume[llm,tts × before_submit,during_single_call] (+ cache-hit replay) | ✅ |
| T-PRICE-02 (D4) | ✗ not implemented as specified — **S4 has no TTS estimate to reconcile** (see §4 D1/D4). Guard-invariant covered by test_phase3_coverage.py::test_worst_case_tts_musd_matches_estimate + T-S5-01 over-budget halt. | changed scope |
| T-REGR | test_regression_phase2.py unchanged (Phase 1/2 guards still green) | ✅ |

**Phase-3 additions beyond the ID matrix** (not counted above): e.g. arabic TTS normalization helpers, voice-settings parsing/snapshot, ffmpeg capability probe, QC parsers, ffmpeg-caps script. Total tests after Phase 3: **412** (Phase 2 baseline: 225).

## 3. Coverage

CI gate command (`.github/workflows/ci.yml`, also what was run here):

```
python -m pytest tests/ -q -m "not slow" \
  --cov=studio.core --cov=studio.db --cov=studio.pipeline \
  --cov=studio.audio --cov=studio.providers.tts --cov-fail-under=90
```

Result: **410 passed, 1 skipped, 1 deselected — TOTAL 4995 statements, 351 missed → 92.97%** (gate ≥ 90% ✓).
Per-module: `providers/tts/elevenlabs.py` 96%, `pipeline/stages_audio.py` 89%, `pipeline/engine.py` 88%, audio engine: `analysis` 97%, `conform` 100%, `ducking` 98%, `envelope` 89%, `library` 91%, `mastering` 85%, `mixer` 97%, `plan` 96%, `selector` 90%, `stem` 100%, `timeline` 98%, `voice` 100%; `db/repos/audio.py` 95%; `qc/audio.py` fully exercised via T-QCA-01 against persisted `qc_results`.
ruff: **All checks passed.** mypy (strict, per `[tool.mypy]`): **no issues in 94 source files**.
Phase 1/2 coverage thresholds unchanged. `cli.py` presentation coverage remains partial (audio CLI is covered through T-CLI-03 + doctor tests, not line-by-line).

## 4. Deviations and why

- **D1. S4 stores no TTS/video projection; D4 re-scoped.** Spec D4/T-PRICE-02 assume an S4 estimate exists. It does not (Phase 2 counts LLM tokens only). Instead: S5a preflight reserves/verifies Σ `len(text_to_send)`×price exactly; S6 emits `projected_video_seconds` + `projected_video_cost_musd`; drift comparison activates only when an S4 output carries a baseline (T-S6-08 crafts one). No Phase 2 code path was reinterpreted.
- **D2. Audio stages are wired into the demo DAG only.** Spec H14 ("register S5a…S7b in the existing DAG") — the demo DAG is the sandbox pipeline; the production text DAG deliberately stays S1–S4 (no media verification until Phase 4; consistent with Phase 2 D7 and T-CFG-04). `studio run --demo --profile sandbox` exercises S1–S7b; `--production` stops at S4.
- **D3. `audio_duration_ms` → `duration_ms`.** The existing `narration_lines` column is `duration_ms`; spec H13 refers to `audio_duration_ms`. No duplicate column was added; `studio audio calibrate` reads `duration_ms` (documented in code; T-CLI/mocks exercise it).
- **D4 (as required by spec) is above, in §C6.** The reporter is "skipped-with-note" instead of a fail.
- **D5. CI coverage command runs `-m "not slow"`.** T-MIX-05 needs ~106 s and ffmpeg; it is marked `slow` and excluded from CI/default runs, verified on demand (105.68 s PASS). This matches the spec's own `@pytest.mark.slow` annotation for T-MIX-05.
- **D6. `j_cut_ms` enforcement is dual.** Settings load raises `ConfigError` (spec) **and** S6 re-checks as defense-in-depth; T-S6-10 asserts the load-time behavior.
- **D7. QC ratio bounds are inclusive `[0.4, 2.5]`** with `silent_line_rms_db` from config; on final failure the stage raises `TtsQualityError` naming the line, expected vs conformed seconds, ratio, bounds, RMS — verified format in T-S5-03.
- **D8. Envelope mode is the default duck**; sidechain compress is opt-in (`audio.duck.mode`), both must pass the same QC (T-DUCK-01/02).
- **D9. `loudnorm` re-conform.** After pass 2, `aresample`+`apad`+`atrim` re-force `total_samples` exactly (loudnorm shifts length by a few samples); verified by T-MIX-01/02.
- **D10. Spec's `-/filter_complex` (FFmpeg ≥ 7.0) vs `-filter_complex_script`** — selected by a one-time cached version probe (`video/ffmpeg.py`); CI installs ffmpeg via pinned `FedericoCarboni/setup-ffmpeg` on both OSes.
- **D11. `ffprobe` in this sandbox is a Python stand-in** (`/tmp/bin/ffprobe`, parsing `ffmpeg -i` stderr; imageio-ffmpeg ships no ffprobe). CI uses a real ffprobe. Every ffmpeg-marked test here ran through the stand-in and passed; this is the one tool-path caveat on the Linux numbers above.
- **D12. `estimate_drift` is a no-op unless a baseline exists** (see D1); S4 was not modified to fabricate one.
- **D13. Raw TTS assets retention.** S5 stores the raw ElevenLabs payload with `retention=keep_n_days` (`keep_narration_audio_days`); the conformed stem and mixes are `intermediate`; CAS retention never downgrades (D1/T-CAS-03).
- **D14. `T-S5-05` has no single test ID** — composed from T-ENG-12 + T-LANG-01/02 (§2).
- **D15. No new gate kinds.** Phase 3 adds zero gates; `final` never auto-approves (unchanged).

## 5. Decisions on ambiguities

- **A1. Overflow fix UX.** S6 fails closed; narration text is never modified (spec H7). The operator fixes the line with `studio narration edit <line_id> --file <utf-8 file>` (writes `text_final`, leaves `tts_nonce` unchanged — new text ⇒ new cache key), then `studio resume <run_id>` re-voices **only that line** (T-S6-09 verifies re-payment of exactly one line).
- **A2. `prefer_stretch_to_save_cost`.** When the smallest clip even with stretch is cheaper than the next plain clip, choose it (T-S6-02/04). `speed = max(1/max_stretch, clip/need)`, `hold ≤ max_hold_ms`; overflow still fails closed.
- **A3. Nonce-retry naming.** QC-failure regenerations use `tts_nonce = f"{base}.q{n}"` (spec H6); the marker `.q` is parsed case-insensitively for resume idempotency (cached good lines are never repaid — T-S5-03).
- **A4. Worst-case vs expected reservation.** Budgets reserve worst-case (Σ `len(text_to_send)` × price) — never an estimate below what could be billed; S5's actual settle uses `ceil(billed_chars × price)` from the provider header when present.
- **A5. Timeline before clips exist.** S6 locks durations from conformed samples alone; Phase 4's clip regeneration must honor the locked `clip_duration_ms` (documented in module docstring; Phase 4 invariant, not enforced here).
- **A6. `j_cut_ms` reserved but rejected** (Phase 5); `lra`/`tp_ceiling_db`/`clip_peak_db` are enforced in mastering verification (MasteringError), not in QC reporting.
- **A7. Duck depth audit.** `duck_depth_db` is measured pre/post duck on the music bus (100 ms RMS windows, speech windows, onset excluded) and persisted on `audio_masters` + `qc_results`; `min_duck_db` is a hard QC fail.
- **A8. Losing `--production` determinism is out of scope**: audio stages are not reachable from `--production` (D2).
- **A9. When ambiguous, spend less and fail closed** throughout (preflight halts before any spend; library scan zeroes out unverifiable tracks; empty music library → `LibraryEmptyError`).

## 6. Known limitations (read before believing anything about ElevenLabs)

- **No live ElevenLabs call has ever been made from this repo.** T-EL-06 (opt-in, `STUDIO_LIVE_TTS=1` + `ELEVENLABS_API_KEY`, $0.50 cap) is the gate; it is skipped here and in CI.
- **Every ElevenLabs wire detail is unverified**: endpoint `POST /v1/text-to-speech/{voice_id}?output_format=…`, header `xi-api-key`, body fields `model_id`, `voice_settings` (`stability`, `similarity_boost`, `style`, `use_speaker_boost`, `speed`), `seed`, `previous_text`/`next_text` (only with `supports_context`), `language_code` (only with `supports_language_code`), response header `character-cost` (used when an integer; else `len(text)`), header `request-id` → `provider_calls.remote_request_id`, error `detail.status` strings (`too_many_concurrent_requests`, `system_busy`, `quota_exceeded`), and the whole 429/5xx transient taxonomy. All are implemented from the prompt's documentation and are **structurally tested only against `httpx.MockTransport`**. If any of these is wrong, the adapter must be revisited before Phase 4/anything touches production.
- **Model IDs and pricing in `configs/model_registry.yaml` are placeholders** marked unverified (e.g. `tts.main`). No price was invented; nothing was `commercial_use_verified`. `studio doctor` reports them as unverified by design.
- **`pricing_verified_on` unset** for TTS entries; no dashboard has been consulted.
- **Windows was not executed in this sandbox** (Linux-only here). CI covers windows-latest × 3.11/3.12; ffmpeg capability check runs there (real ffmpeg + real ffprobe).
- **`ffprobe` here is a stand-in** (D11). Loudness/trim/peak QC numbers on this machine were produced through it; CI's real ffprobe is the reference.
- **Arabic quality is unmeasured** — tashakeel on/off is an audition decision for the operator (`studio tts audition`); nothing here demonstrates quality.
- **`cli.py` coverage** remains partial; `live_smoke.py` unchanged from Phase 2.
- **S4↔S5 cost reconciliation (T-PRICE-02) is not implemented** — there is no S4 TTS estimate in this codebase to reconcile against (D1/D4, §4).
- **Gate staleness semantics** (speculative execution before a pending gate; `staleness_invalidated` on approval) observed and left unchanged; documented in §C7.
- **NVENC note** and all Phase 1/2 limitations stand.

## 7. Commands — the audition → benchmark flow (Linux and Windows)

Prereqs: ffmpeg + ffprobe on PATH; `pip install -e .[dev,providers]`; scratch `STUDIO_HOME`.

Linux / macOS:
```bash
export STUDIO_HOME="$PWD"
studio init
studio library scan              # registers your library (library/{music,sfx,ambience}/index.yaml)
studio library check             # warns on empty mood x intensity cells
# 1) audition (opt-in, spends money; prints worst-case total first, requires --yes):
export ELEVENLABS_API_KEY=...     # when you have one
studio tts audition --voice-id <id> --model-keys tts.main[,tts.candidate.flash] \
  --lines-file fusha_lines.txt [--no-diacritics-variant] --cap-usd 0.50
#    -> data/previews/audition/<ts>/ + README.txt with per-file µ$ costs; listen.
# 2) A/B tashakeel: keep_diacritics true vs false (settings.yaml), re-audition.
# 3) set the chosen model/price in configs/model_registry.yaml, pricing_verified_on, then:
STUDIO_LIVE_TTS=1 python -m pytest tests/test_elevenlabs.py -k t_el_06 -q
studio run --demo --profile sandbox --languages ar
studio worker --until-idle
studio gates approve <gate-id>   # G1_SCRIPT; worker; approve G2; worker
studio audio preview <deliverable_id>        # playable FLAC in data/previews/
studio audio qc <deliverable_id>             # persisted qc_results
studio audio calibrate                       # measured w/s per language
studio doctor                                # ffmpeg, library, voices, TTS entries
python scripts/check_ffmpeg.py               # the CI capability check
python -m pytest tests/ -q -m "not slow" --cov=studio.core --cov=studio.db \
  --cov=studio.pipeline --cov=studio.audio --cov=studio.providers.tts --cov-fail-under=90
python -m pytest tests/test_audio_mix.py -q -m slow   # T-MIX-05 on demand
```

Windows (PowerShell):
```powershell
$env:STUDIO_HOME = "$PWD"
studio init; studio library scan; studio library check
$env:ELEVENLABS_API_KEY = "..."
studio tts audition --voice-id <id> --model-keys tts.main --lines-file fusha_lines.txt --cap-usd 0.50
studio run --demo --profile sandbox --languages ar
studio worker --until-idle
studio gates approve <gate-id>
studio audio preview <deliverable_id>; studio audio qc <deliverable_id>; studio audio calibrate; studio doctor
python -m pytest tests\ -q -m "not slow" --cov=studio.core --cov=studio.db --cov=studio.pipeline --cov=studio.audio --cov=studio.providers.tts --cov-fail-under=90
python -m pytest tests\test_audio_mix.py -q -m slow
```

**Observed in this sandbox (2026-11-17):** full suite 410 passed/1 skipped/1 deselected; coverage 92.97%; ruff clean; mypy clean (94 files); `studio init` applies migrations [1,2,3,4]; sandbox run Arabic-only parks at G1/G2/G3 and completes with the audio DAG (`s5a/s5/s6/s7/s7b`) — exercised via T-CLI-03/T-ENG-12/T-S6-09; master FLAC exactly `total_samples`, −14 ± 1 LUFS, duck ≥ 8 dB (mock TTS, mock-free local ffmpeg path); T-MIX-05 on demand 105.68 s PASS. **Not observed here:** Windows CI, real ffprobe (stand-in used), any live ElevenLabs response.

## 8. Files

New (Step 7): `tests/test_timing_s6.py`, `PHASE3_REPORT.md`, `scripts/check_ffmpeg.py` (earlier), plus Step-1..6 files committed in `18592e6` (`src/studio/providers/tts/elevenlabs.py`, `src/studio/audio/*`, `src/studio/qc/audio.py`, `src/studio/pipeline/stages_audio.py`, `configs/*` merges, migration `004_audio.sql`, `README.md` §Audio engine — this turn).

Modified (this step): `README.md`, `.github/workflows/ci.yml` (ffmpeg capability step + `-m "not slow"` + audio cov paths), `pyproject.toml` (coverage paths), `src/studio/cli.py` (doctor audio checks; earlier), `src/studio/qc/audio.py`, `src/studio/pipeline/stages_audio.py`, tests `test_mocks_cli.py`, `test_audio_mix.py`.

Phase 1/2 contracts: untouched. `BEGIN_PROMPT.md` / `PHASE2_IMPLEMENTATION_PROMPT.md` / S2 prompts / schemas: untouched. No new runtime dependencies; no pydub/numpy.
