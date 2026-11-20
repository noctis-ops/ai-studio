# Phase 4 Discovery Report (§C)

Written **before** Phase 4 coding, 2026-11-17, from the tree at commit `9a76467`
(after the Phase 3 pre-flight audit fix in `src/studio/video/probe.py`).
Every answer names the file(s) inspected. Where the prompt and the tree
disagree, the **fail-closed** option is chosen and recorded here and in
`PHASE4_REPORT.md` §Deviations.

## C1 — DAG table (demo and production), gates, spend, R1/R4

Sources: `src/studio/demo/stages.py::demo_specs()` (lines 624-697),
`src/studio/pipeline/stages_text.py::text_specs()` (lines 921-958),
`src/studio/cli.py::_pipeline_specs()` / `_dag_for_run()` (lines 1155-1180),
`src/studio/pipeline/stage.py::StageSpec`, `src/studio/pipeline/dag.py::Dag`,
`src/studio/pipeline/engine.py::Engine.advance_run()` (lines 162-192),
`src/studio/pipeline/engine.py::_maybe_gate_stage()` (lines 340-385),
`src/studio/pipeline/gates.py`, `src/studio/db/migrations/001_init.sql`.

`spends_money` does **not** exist on `StageSpec` yet (it has `name`, `scope`,
`depends_on`, `handler`, `gate_after`). The column below is the Phase 4 target
(H0). `gate` values come from `StageSpec.gate_after`; the gate row is keyed by
`(run_id, stage, round)` (`src/studio/db/repos/gates.py::latest`).

### Demo DAG (`studio run --demo`, `Dag(demo_specs())`)

| stage id | depends_on | gate_after | scope | spends_money (H0) | what it does |
|---|---|---|---|---|---|
| `demo_script` | — | **G1_SCRIPT** | EPISODE | no | mock LLM script; creates shots + narration_lines |
| `demo_voice` | `demo_script` | — | DELIVERABLE | no | materializes narration lines per language |
| `s5a_tts_preflight` | `demo_script` | — | EPISODE | no | free worst-case TTS preflight |
| `s5_tts` | `s5a_tts_preflight`,`demo_voice` | — | LINE | **yes** | ElevenLabs/mock TTS |
| `s6_timing` | `s5_tts` | — | TRACK | no | locks `shots.clip_duration_ms` |
| `s7_audio_plan` | `s6_timing` | — | DELIVERABLE | no | deterministic cue plan |
| `s7b_audio_mix` | `s7_audio_plan`,`s5_tts` | — | DELIVERABLE | no | local ffmpeg premaster + master + QC |
| `demo_keyframes` | `demo_script` | **G2_KEYFRAMES** | SHOT | **yes** | mock image (`DemoKeyframesHandler`) |
| `demo_clips` | `demo_keyframes`,`s6_timing` | — | SHOT | **yes** | mock i2v (`DemoClipsHandler`) |
| `demo_finalize` | `demo_clips` | **G3_FINAL_CUT** | DELIVERABLE | no | manifest into CAS |

### Production DAG (`studio run --production`, `Dag(text_specs())`)

| stage id | depends_on | gate_after | scope | spends_money | what it does |
|---|---|---|---|---|---|
| `bible_upsert` | — | — | EPISODE | yes (paid LLM) | S1 real LLM |
| `narration_script` | `bible_upsert` | — | EPISODE | yes (paid LLM) | S2 + tashkeel verify |
| `shot_plan` | `narration_script` | **G1_SCRIPT** | TRACK | yes (paid LLM) | S3 shots + lines |
| `metadata_safety` | `shot_plan` | **G3_FINAL_CUT** | DELIVERABLE | yes (paid LLM) | S4 metadata/safety — **terminal gate today (R4)** |

No audio or media stage is reachable from `--production`: `cli.py::_pipeline_specs(True)` returns exactly `text_specs()`, and `Engine(llm_subset_only=True)` (`cli.py::worker --production`) resolves only LLM roles. This is R2/D2.

`G1_SCRIPT` is carried by `demo_script` (demo) and `shot_plan` (production).
The `metadata_safety` G3 round (`GateKind.G3_FINAL_CUT`, `text_specs()` line 949) is the terminal gate of the production DAG — exactly R4. Appending media after it means the operator approves "Final Cut" before TTS; §A accepts this. `demo_finalize` also carries G3, so a tooling query filtering only `gate='G3_FINAL_CUT'` sees both kinds (Phase 3 report §C7).

**R1 / what actually runs while a gate is pending.** `Engine.advance_run()` returns immediately when `repos.gates.list_pending(run_id)` is non-empty, so *no new stages are enumerated* in that call. But enumeration is per-call: `advance_run()` walks `self.dag.order`, calls `_enumerate_stage()` (which enqueues `run_stage` jobs immediately) and only then `_maybe_gate_stage()`. Stages declared before the gated stage in topological order are therefore already **enqueued** in the same `advance_run` call (e.g. `demo_voice`, `s5a`, `s5`, `s6`, `s7`, `s7b` before `demo_keyframes`' G2). `Worker.claim()` (`db/repos/jobs.py`) does not consult gates, and `Engine._run_stage_job()` only refuses runs in `halted_budget`/`failed`/`completed` — `awaiting_approval` is explicitly allowed (engine.py lines 452-459). So queued `run_stage` jobs **do execute** while a gate is pending; `GateService.approve()` then calls `_enqueue_advance()`, whose `_invalidate_stale`/`_requeue_stale` marks speculatively-completed stage runs `staleness_invalidated` and re-arms them. That is the paid-spend-before-Gate-2 hole R1 describes. H0's barrier is enforced *inside* `_run_stage_job`, independent of the scheduler.

## C2 — Production text → audio contract (R2)

Sources: `src/studio/pipeline/stages_text.py::NarrationScriptHandler` (S2, lines 388-595), `ShotPlanHandler` (S3, lines 600-753), `src/studio/db/migrations/001_init.sql::narration_lines`, `src/studio/db/migrations/004_audio.sql` (`text_final` etc.), `src/studio/domain/models.py::NarrationLineRow.approved_text`, `src/studio/pipeline/stages_audio.py` (`_text_to_send`, `TtsPreflightHandler`, `TtsLineHandler`, lines 188-190, 251-300, 457-720), `src/studio/language/tashkeel.py`, `src/studio/language/arabic.py`.

- There is **no `S3b` stage** in this tree. S2 (`narration_script`) produces `lines[].text_tashkeel` (diacritized Arabic) in its stage `output_json`; S3 (`shot_plan`) reads that output and materializes `narration_lines.text = line["text_tashkeel"]` (stages_text.py line 744). S3 writes `narration_lines` once and **does not write back afterwards**; the only later writer is `studio narration edit` (`004_audio.sql::text_final`, `cli.py::narration_edit`).
- Columns holding the approved text: `narration_lines.text` (diacritized, from S2/S3) and `narration_lines.text_final` (NULL until a post-Gate-1 operator edit). `NarrationLineRow.approved_text` = `text_final if not NULL else text`.
- S5 reads `line.approved_text`, then `_text_to_send(ctx, language, approved_text)` → `normalize_for_tts()` honoring `tts.keep_diacritics` (default `true`). So with the shipped settings the text sent to ElevenLabs **is** the diacritized text approved at Gate 1 (stripped only if the operator sets `keep_diacritics: false`). T-INT-01 must assert exactly this. S5a preflight sums `len(text_to_send)` with the same function, so estimate and spend agree.
- `narration_lines` columns: id, deliverable_id, shot_id, line_no, text, asset_sha256, duration_ms, created_at + (004) text_final, conformed_sha256, conformed_samples, status, tts_nonce, voice_snapshot_json. The prompt's `audio_duration_ms` does not exist (Phase 3 deviation D3).

## C3 — Shot schema

Sources: `src/studio/db/migrations/001_init.sql::shots` (id, track_id, shot_no, prompt, created_at), `004_audio.sql` (`clip_duration_ms`, `speed_factor`, `hold_frames`, `min_duration_ms`, `timing_status`), `src/studio/pipeline/stages_text.py::_SHOTS_SCHEMA` / `_parse_shots()` (lines 756-793), `src/studio/db/repos/content.py::create_shot/shots_for_track`.

Persisted per shot: `id`, `track_id`, `shot_no`, `prompt` (= `visual_prompt` from S3), `created_at`, plus the S6 timing columns above. The S3 **stage output** (`shot_plan.output_json["shots"]`) carries `no`, `duration_s`, `visual_prompt`, `characters`, `camera` — these are *not* stored on the shot row.

There is **no** `image_prompt`, `motion_prompt`, `action`, `mood`, `location` or `sfx_tags` field anywhere (`PHASE3_REPORT.md` §C3 confirms `sfx_tags` absence). §G therefore composes `scene_text` and the motion sentence deterministically from exactly `visual_prompt`, `camera`, `characters`, `duration_s` and the shot number — no invented location/mood, no changes to S2 prompts or `_SHOTS_SCHEMA` (fail-closed).

## C4 — Characters, style, bible, reusable Phase 1 columns

Sources: `src/studio/domain/series_bible.py` (`BibleContent`: `title`, `premise`, `world`, `characters[CharacterBible{name, description, visual_tokens, arc}]`, `style_tokens`, `tone`, `continuity_notes`), `src/studio/db/migrations/003_series_bible.sql::series_bible` (id, series_id, version, content_json, content_sha256, …), `src/studio/db/repos/bible.py`, `src/studio/pipeline/bible_pack.py::build_context_pack` (renders `characters` + `style_tokens`), `src/studio/db/migrations/001_init.sql::characters/shot_attempts/assets`, `src/studio/db/migrations/004_audio.sql::qc_results`.

- Character visual specs live in `series_bible.content_json` → `characters[].visual_tokens` + `description` + `arc`; style lock text = `style_tokens` (+ `tone`, `world`). `characters` table only has `role`, `name`, `voices_json` — no visual spec.
- **No** `character_sheets` table, no reference-asset table and no `assets.reference`/`assets.asset_id` column. `assets` (sha256 PK, rel_path, kind, mime, size_bytes, duration_ms, width, height, retention, created_at, purged_at, purge_after) is the only asset store and can hold sheet/keyframe/clip bytes; §E3 creates `character_sheets`.
- Reusable for `shot_attempts`: it already has `id`, `shot_id`, `attempt_no`, `nonce`, `prompt_used`, `rejection_note`, `created_at` (001), so `kind`, `prompt_spec_json`, `sanitize_level`, `refusal_log_json`, `qc_status`, `qc_json`, `asset_id`, `conformed_asset_id`, `cost_musd`, `provider_call_id`, `policy_blocked` are the additive 005 columns. `qc_results` (004) is reusable for persisted visual checks; §K stores per-attempt `qc_json` too. `provider_calls` has `est_cost_musd`/`actual_cost_musd`/`remote_request_id`/`submit_started_at` — the crash-safe spine T-REC-03 reuses. `cost_ledger`/`budgets` are the cap machinery.

## C5 — Registry and gateway

Sources: `configs/model_registry.yaml`, `src/studio/config/registry.py` (`Per1mTokens`, `Per1kChars`, `PerImage`, `PerSecond`; `ModelEntry`, `ROLE_CAPABILITY`, `resolve_profile`, `resolve_llm_subset`, `_resolve_roles`), `src/studio/core/pricing.py`, `src/studio/core/gateway.py`, `src/studio/providers/base.py`, `src/studio/providers/factory.py`, `src/studio/providers/mock/*.py`.

- Existing image/video entries: `image.keyframe` (`fal-ai/flux/dev`, `per_image`, $0, `param_map {prompt, refs, aspect, seed}`, `needs_confirmation: true`), `video.i2v.draft` (`fal-ai/minimax`, `per_second`, $0, `supported_aspects`, `supported_durations_s [5,10]`, `param_map {}`), `video.i2v.final` (`fal-ai/kling`, same), plus `mock.image`/`mock.video`. All three real entries have `pricing_verified_on: null`, `commercial_use_verified: false`, `needs_confirmation: true`. `mock.*` are `simulated: true`.
- Pricing units supported: `per_1m_tokens`, `per_1k_chars`, `per_image`, `per_second` (**no** `per_megapixel`, **no** `per_video`). `ModelEntry.model_config = ConfigDict(extra="forbid")` — every new YAML key needs a typed field.
- `needs_confirmation` is informational in `registry.py` (doctor warns); `placeholder` is derived from `"<CONFIRM"` in `model_id`/`endpoint_id`; `_resolve_roles()` refuses non-simulated placeholders and, for draft/final, non-simulated zero prices unless `pricing_acknowledged_free`. `assert_exportable()` enforces `commercial_use_verified` for `final`.
- `param_map` exists (`dict[str,str]`, currently used only by `image.keyframe`). `fixed_params`, `aspect_values`, `param_formats`, `result_map`, `media` block, `supports_refs`, `max_refs`, `ref_style`, `input_transport`, `prompt_templates` do **not** exist.
- Profile roles today: `llm_creative`, `llm_light`, `tts`, `keyframe` (→ capability `image_reference_generate`), `video` (→ `image_to_video`) — `registry.py::ROLE_CAPABILITY`. `sandbox` maps them all to `mock.*`; `draft`/`final` to the real entries (and full resolution of draft/final currently fails on zero prices — T-CFG-04).
- `JobProvider` protocol (`providers/base.py`): `submit(MediaRequest) -> JobHandle`, `poll(JobHandle) -> JobState`, `fetch(JobHandle) -> list[MediaPayload]`. `MediaRequest` carries `capability`, `params`, `input_assets: list[AssetRef]`, `aspect`, `duration_s`, `seed`, `prompt_version_sha`. Input assets reach the adapter as `AssetRef`s (sha + rel_path); the gateway `_key_parts()` hashes their sha256 into the cache key, and `_request_dict()`/`redact()` persist only shas.
- Gateway (`core/gateway.py`): `_EXPECTED_CAPABILITY` maps `image→image_reference_generate`, `video→image_to_video`; `_submit_job()` persists `submit_started_at`, then `mark_submitted()` writes `remote_request_id` + `status='submitted'` in **one** statement; `_poll_until_done()` retries transients; `_fetch_and_store()` materializes `MediaPayload.local_path` or downloads `url` and stores into CAS via `put_file`. `CallContext` has `episode_id`, `stage_run_id`, `nonce`, `purpose` — **no** `gate_cleared`/`gate_exempt` yet.
- Mock wiring: `factory.py::_build_mock()` returns `MockImageProvider`/`MockVideoProvider` for those capabilities; `MockJobBase` keeps a JSON ledger so `submit_count` survives simulated restarts; `MockImageProvider.fetch()` renders a PNG via ffmpeg, `MockVideoProvider.fetch()` a `testsrc2` MP4 at `duration_s`. `build_provider()` raises `ConfigError` for `provider == "fal"` (`_FUTURE_PROVIDERS`) — Phase 4 must register the fal adapter here.

## C6 — Demo DAG media stages today

`src/studio/demo/stages.py::DemoKeyframesHandler` (lines 331-397): SHOT scope, creates a `shot_attempts` row with `prompt_used = shot.prompt`, calls `ctx.gateway.image(mock.image, MediaRequest(params={"prompt": ...}, aspect=track.aspect, seed=_seed_from_nonce(attempt.nonce)))` and returns `asset_sha256s`. `DemoClipsHandler` (lines 399-490): SHOT scope, reads `demo_keyframes` output, quantizes `narration_max_duration_ms` to a supported duration, passes the keyframe `AssetRef` as `input_assets`, calls `gateway.video(...)` and returns `duration_s` + `asset_sha256s`. `DemoFinalizeHandler` writes a JSON manifest. No character sheets, no refs, no prompt sanitizer, no visual QC, no conform, no `clip_frames` enforcement. §M replaces these with the real S8a/S8/S10 handlers while keeping the mock providers.

## C7 — S6 durations and input_hash

`src/studio/pipeline/stages_audio.py::TimingReconcilerHandler._supported_durations()` (lines 786-795) reads `ctx.resolved_profile.model_key_for("video")` / `entries["video"].supported_durations_s` from the **registry** (not settings) and raises `StudioError` if empty. `input_hash()` (lines 725-770) includes `supported_durations_s` but **not** the resolved video model key — §J's fix is to add `video_model_key`. S6 writes `shots.clip_duration_ms`, `speed_factor`, `hold_frames`, `timing_status='locked'` and emits `projected_video_seconds` / `projected_video_cost_musd`. `src/studio/audio/timeline.py` has `build_timeline()`/`TimelineShot` and is the pure duration-selection helper reused by §H2 and §B ("S10 uses build_timeline"). Settings timing knobs are in `configs/settings.yaml::timing` (`words_per_second`, `lead_in_ms`, `line_gap_ms`, `tail_ms`, `max_stretch`, `max_hold_ms`, `estimate_drift_warn_pct`).

## C8 — Gate semantics, estimate/budget commands, cap raising

- `_gate_snapshot_hash()` (`engine.py` lines 388-390) = `sha256(canonical_json(sorted((stage_run.id, stage_run.output_json or ""))))` over the **succeeded** rows of that stage; `GateService.create_gate_round()` persists it on `approval_gates.snapshot_hash` with `round = count+1`; `repos.gates.latest(run_id, stage)` returns the active row. Approval is **per gate row / per stage**, not per gate kind (`PHASE3_REPORT.md` §C7).
- `GateService.request_changes(gate_id, note, rejections)` (`gates.py` lines 132-190) is per **run gate** but operates **per shot**: it requires ≥1 `ShotRejection`, creates a new `shot_attempts` row per rejected shot, stales the gate stage + `dag.downstream()` stage runs related to that shot, decides `changes_requested`, and enqueues `advance_run`. `cli.py::gates reject --shot-id ... --prompt-file ...` exposes one rejection.
- **No `studio estimate` command exists** (`cli.py` has `init`, `doctor`, `run`, `worker`, `db`, `gates`, `budget`, `voice`, `narration`, `library`, `tts audition`, `audio`). Phase 4 does not add one; the media estimate goes into S4's output (§H2) and `studio media audition`/`smoke` print worst-case costs first.
- `studio budget` **does** exist: `status` (`cli.py` lines 893-914) and `reset <scope> <ref> --cap <usd>` (lines 917-943). Caps are raised through `LedgerService.reset_kill_switch(scope, ref, new_cap)` (`src/studio/core/budget.py`), which writes an audited `adjust` row. `budgets` rows are seeded from `settings.budgets` (`default_episode_cap_usd`, `monthly_cap_usd`, `global_cap_usd`). §L adds `budget status --by-model`.

## C9 — tts audition mechanics (reused by media audition)

`src/studio/cli.py::tts_audition()` (lines 1363-1536): `CallContext(episode_id=None, stage_run_id=None, nonce=sha256(key|voice|variant|text)[:16], purpose="tts_audition")` — deliberately no episode, so **month + global** budget scopes apply (`enums.py::budget_scopes_for_call`). Worst-case per call = `estimate_cost_musd(entry, req)`; the sum must fit `--cap-usd` before any call and a running-total guard re-checks before each call; `--yes` is mandatory. It builds a standalone `ProviderGateway` over `_ledger_for()`, copies CAS assets into `data/previews/audition/<ts>/`, writes `README.txt` with per-file µ$ cost. Phase 4 audition/smoke reuse this scope mechanism and set the explicit `gate_exempt` context from H0.

## C10 — Collected crash-test IDs (`pytest --collect-only -q -k crash`)

28 collected (of 414), from `tests/test_crash_matrix.py` (14 `test_crash_matrix[<point>-image|video]` + `test_resume_after_crash_is_a_cache_hit_for_identical_replay` + `test_crash_points_cover_the_eight_defined_points`), `tests/test_phase3_hardening.py` (8 parameterized `test_t_rec_02_single_call_crash_resume[*]`), `tests/test_engine.py::test_worker_crash_mid_stage_requeues_and_pays_zero_repaid`, `tests/test_timing_s6.py::test_t_s5_02_crash_mid_s5_restart_repays_nothing`, `tests/test_audio_mix.py::test_t_eng_13_crash_in_s7b_resumes_with_zero_tts_repaid`, `tests/test_core.py::test_crash_between_temp_write_and_replace_leaves_no_final_file`. Phase 3 Finding #6: `core/faults.py::CRASH_POINTS` has 8 points (the 8th is `during_single_call`), 7 of them appear in the image/video job matrix → 14 scenarios. T-REC-03 extends the matrix to `{fal image, fal video}` via `httpx.MockTransport`.

## C11 — ffmpeg filters already probed

`src/studio/video/ffmpeg.py::REQUIRED_FILTERS` (shared by `scripts/check_ffmpeg.py::main()` and `studio doctor`'s `_phase3_doctor_rows()` and `tests/test_ffmpeg_capabilities.py`) contains 20: `loudnorm`, `ebur128`, `astats`, `sidechaincompress`, `volume`, `amix`, `adelay`, `afade`, `atrim`, `apad`, `aresample`, `aformat`, `silenceremove`, `areverse`, `alimiter`, `anullsrc`, `asetnsamples`, `ametadata`, `pan`, `asplit`. H3 adds `signalstats`, `blackdetect`, `freezedetect`, `ssim`, `fps`, `trim`, `setpts`, `tpad`, `scale`, `setsar`, `format`.

**Path contradiction:** H3 names `tests/unit/test_ffmpeg_capabilities.py`; this repo has a flat `tests/` directory and the file is `tests/test_ffmpeg_capabilities.py` (no `tests/unit/` package). Fail-closed choice: extend the existing flat test (and add the new filter assertions there), do not create an empty `tests/unit/` tree. Reported as a deviation.

**ffprobe JSON only:** `src/studio/video/probe.py::probe()` uses `-print_format json -show_format -show_streams`; `video/ffmpeg.py::probe_duration_seconds()` / `probe_sample_rate_channels()` use `-of json`. `qc/audio.py` parses `ebur128`/`astats` from ffmpeg stderr, not ffprobe. After the pre-flight fix, `probe()` no longer passes `-f <ext>` — auto-detection is required for real ffprobe (PNG is `png_pipe`, not a `png` demuxer).

## C12 — Gate 1 video-estimate baseline keys

`src/studio/pipeline/stages_audio.py::TimingReconcilerHandler.run()` (lines 985-1033): for each deliverable it calls `_stage_output(ctx, "metadata_safety", deliverable.id)` and reads **`projected_video_cost_musd`** or **`gate1_video_estimate_musd`** (first integer ≥ 0 wins), takes `max()` across deliverables, and compares to its own `projected_video_cost_musd` (computed from Σ `clip_duration_ms` × the video model's `PerSecond.usd`). A drift past `timing.estimate_drift_warn_pct` writes audit event `estimate_drift` and adds `output["estimate_drift"]`. S6 output holds `projected_video_seconds`, `projected_video_cost_musd`, `video_note`. S4 (`MetadataSafetyHandler.run()`, stages_text.py lines 834-890) currently outputs only `deliverable_id`, `language`, `safety`, `title`, `tags` — **no baseline**, so drift is a no-op today (Phase 3 D1/D12). §H2 adds exactly those two keys (additive) using `gate1_video_estimate_musd` as the expected value.

## Contradictions resolved fail-closed

1. §C3/§G assume shot fields that do not exist (`motion`, `action`, `mood`, `location`, `image_prompt`, `motion_prompt`) → compose from the five real S3 fields only; never invent content.
2. §E1 suggests wiring new placeholder endpoints into shipped profile roles; doing so changes `registry_snapshot()` role sets (breaking `tests/test_config.py::test_registry_snapshot_deterministic_and_persisted`, T-CFG-06) and the exact `RegistryError` message asserted by `tests/test_regression_phase2.py::test_draft_still_refuses_without_verifying_media_models`. Fail-closed choice: add the five entries and register the three optional roles in `ROLE_CAPABILITY`, but leave shipped profile role maps unchanged except H4; the operator wires the confirmed endpoints after the audition (§Part 2).
3. §M says append media stages after `metadata_safety` in `text_specs()`. Doing that directly breaks Phase 2 E2E tests (`tests/test_stages_text.py` builds `Dag(text_specs())` with `llm_subset_only=True` and expects the run to complete at G3). Fail-closed choice: keep `text_specs()` as the frozen S1–S4 text subset and add `production_specs()` (new) that extends it; `cli.py::_pipeline_specs(True)`/`_dag_for_run()` and `worker --production` use the extended DAG. No Phase 1–3 test is weakened.
4. §R/H3's `tests/unit/...` path does not exist (see C11).
5. §D H0 asks for `spends_money` on "the LLM sanitizer path" — S1/S2/S3/S4 are already paid LLM stages but are *not* gated ancestors of themselves; the barrier applies to S5/S8a/S8/S10, and the sanitizer runs inside those stages (so it inherits the barrier). Marking S1–S4 `spends_money=True` would deadlock them (their own G1/G3 ancestors), so only the four media stages are marked; documented.
