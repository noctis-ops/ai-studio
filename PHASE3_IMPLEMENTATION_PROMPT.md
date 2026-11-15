# Phase 3 Review and Implementation Prompt: Real TTS + Local Audio Engine

I have only your summaries, not the Phase 2 spec or code. The audit below is based on your report and on the Phase 1 design I wrote. The Phase 3 prompt makes the coding LLM discover the actual Phase 2 interfaces and report any mismatch instead of guessing.

---

## Part 1: Architectural Audit

| # | Finding | Severity | Action |
|---|---|---|---|
| 1 | **Live LLM smoke was never run.** Real Arabic tashkeel, structured-output reliability, and token/price assumptions are untested against real APIs. Phase 3 also needs real Arabic lines to audition voices. | High | **You:** run the live smoke with a ≤ $1 cap before Phase 3 starts (see Part 2). |
| 2 | **Single-call providers (LLM, TTS) were never crash-tested.** The T-REC-01 matrix covered only the image/video job providers. A crash mid-HTTP-call on resume re-reserves or resubmits with no `potential_orphan` record, so a double charge could go unlogged. | Medium | Phase 3 hardening H0-b: add a crash point and tests for single-call providers (TTS and LLM). |
| 3 | **CAS retention can be downgraded.** `put_file … ON CONFLICT DO NOTHING` keeps whichever retention was written first. If identical bytes are stored first as `intermediate` and later as `permanent`, the asset can still be purged. | Medium | Phase 3 hardening H0-a: on conflict, upgrade to the longer-lived retention, never downgrade. |
| 4 | **Gate 1 video estimate can be wrong by up to 2×.** Clip length is quantized to 5 s or 10 s, and the real duration is only known after TTS, which runs after Gate 1. | Medium | S6 now emits a projected-video-cost re-estimate and an estimate-drift audit event. The coding LLM must surface it at Gate 2 (Phases 5–6). |
| 5 | **Arabic diacritics inflate billable characters.** Each combining mark is a Unicode character, and ElevenLabs bills by characters sent, so tashkeel may raise TTS cost 1.3–2× over the undiacritized text. | Medium | Check that the S4 estimator counts `len(text_actually_sent)`. Add a config switch and an A/B audition for tashkeel on or off. |
| 6 | **Crash-matrix count doesn't match my spec.** I specified 7 crash points × 2 providers = 14 scenarios. You report 16 and "7 other crash points", which implies 8 points. | Low | Tell me what the extra point is. It may be fine, but I want to know it was deliberate. |
| 7 | **`metadata_safety` parks at `G3_FINAL_CUT` (D4).** A pre-render metadata review and the real final-cut review now share one gate kind. If the engine keys on `(run, gate)`, approving the metadata round could satisfy final-cut approval and let a render skip human review. | Medium | Confirm how rounds are distinguished (by `subject_type`?). I recommend a hard test that a final-cut approval requires a G3 round whose subject is a render. This belongs in the Phase 5 prompt. |
| 8 | **The `draft` profile routes creative direction to `gpt-4o-mini`.** That is a false economy. A Sonnet-class story and shot plan costs cents per Short, while one regenerated video clip costs dollars. Weak shot plans cause expensive rejections downstream. | Medium | **Your decision:** I recommend `draft.llm_creative = llm.creative`. |
| 9 | **pydub on Python ≥ 3.13.** pydub depends on `audioop`, which was removed in 3.13. | Info | **Deviation from your "Pydub/FFmpeg" decision:** Phase 3 does not use pydub. FFmpeg plus stdlib `wave` covers everything with zero new dependencies. |
| 10 | **Sidechain ducking has no lookahead**, so the first ~20 ms of each sentence leaks music and the music pumps between sentences. Narration positions are known exactly. | Design | Default to timeline-driven envelope ducking. Sidechain remains a config option, and both modes pass the same QC. |
| 11 | **Windows CI.** `ffmpeg -version` failed at the 10 s probe because ffmpeg was absent from PATH. | Low | Fixed in the prompt: a setup action, a capability check step, and a 30 s doctor probe. |
| 12 | **ElevenLabs Arabic model ID is still a placeholder**, and I can't verify current IDs or prices. | Blocker for live calls | Part 2 gives candidates to audition, not a verified answer. |
| 13 | **Unknown: does the Phase 2 shot schema carry `sfx_tags`?** | Unknown | The prompt handles both cases and requires a report. S2 prompts must not be modified in Phase 3. |

### Decisions I made that you should know about

1. **No automatic LLM narration trimming in S6.** My blueprint proposed it, but it would silently edit text you approved at Gate 1, and Arabic edits would need a fresh tashkeel pass and validation. S6 instead fails closed with a precise overflow report. You fix the line with `studio narration edit` and resume.
2. **The timeline is in integer frames at 24 fps.** At 48 kHz, one frame is exactly 2000 samples, so narration placement is sample-exact and audio/video drift is impossible. Config validation requires `48000 % fps == 0`.
3. **The narration stem is assembled with stdlib `wave`, not FFmpeg filters.** It is sample-exact by construction and trivial to test.
4. **Cost-aware clip quantization:** if a small slow-down or hold lets a shot fit the 5 s clip instead of the 10 s one, S6 takes the cheaper option (`prefer_stretch_to_save_cost`).
5. **Phase 3 adds no gates.** The first human audio review is the Phase 5 animatic at Gate 2, which comes before the expensive video spend. Until then, use `studio audio preview`.

---

## Part 2: Operator Prerequisites (before running the prompt)

1. **Run the live LLM smoke test** (`STUDIO_LIVE_LLM=1`, cap ≤ $1) and read the Arabic output. Keep that output as audition material.
2. **Decide finding #8** (draft creative routing).
3. **Prepare an audio library** (not committed to git). Minimum for the benchmark: ≥ 6 music tracks covering your mood vocabulary at varied intensities (some loopable), ≥ 3 loopable ambience beds, ≥ 6 SFX including ≥ 2 tagged `transition`. Each needs a license record. Royalty-free tracks can still trigger Content ID claims, so keep the proof.
4. **Collect 2–3 candidate Arabic (Fusha-suitable) voice IDs** from your ElevenLabs account. Check each voice's own usage terms. Your plan's license does not necessarily cover every shared voice.
5. **Registry entries.** Merge the following into `configs/model_registry.yaml`. Everything marked ⚠ is from my memory and must be verified against current ElevenLabs docs and your dashboard.

```yaml
  tts.main:
    provider: elevenlabs
    capability: tts
    model_id: "eleven_multilingual_v2"      # ⚠ believed current and Arabic-capable; confirm via audition
    pricing: {unit: per_1k_chars, usd: 0.30} # ⚠ PROVISIONAL worst case. Derive: (plan $ / included credits) x 1000
                                             #   x model credit-per-char multiplier; use the higher of included vs overage rate
    pricing_verified_on: null                # set to a date once verified
    commercial_use_verified: true
    commercial_use_note: "Operator attestation: ElevenLabs Creator plan. Re-verify, and check each voice's own terms."
    tts_params: {output_format: "mp3_44100_128", supports_context: true, supports_language_code: false}
    limits: {max_concurrency: 1, min_interval_s: 0.5}
    needs_confirmation: true
  tts.candidate.flash:                       # audition-only, not referenced by any profile
    provider: elevenlabs
    capability: tts
    model_id: "eleven_flash_v2_5"            # ⚠
    pricing: {unit: per_1k_chars, usd: 0.15} # ⚠ provisional
    pricing_verified_on: null
    commercial_use_verified: true
    tts_params: {output_format: "mp3_44100_128", supports_context: false, supports_language_code: true}
    needs_confirmation: true
  tts.candidate.v3:                          # audition-only
    provider: elevenlabs
    capability: tts
    model_id: "eleven_v3"                    # ⚠
    pricing: {unit: per_1k_chars, usd: 0.30} # ⚠ provisional
    pricing_verified_on: null
    commercial_use_verified: true
    tts_params: {output_format: "mp3_44100_128", supports_context: false, supports_language_code: false}
    needs_confirmation: true
```

Rough live spend for Phase 3 acceptance: auditions cost under $1, and the benchmark Short's narration (about 1,000–1,500 characters with diacritics) costs well under $1.

---

# ═══════════ BEGIN PROMPT ═══════════

## A. Role and Mission

You are a senior Python engineer implementing **Phase 3 (Voice and Audio)** of "AI Content Studio" in the existing `ai-studio` repository. Phases 1 (foundation) and 2 (text pipeline, S1–S4, real LLM adapters) are merged on `main`: 225 tests, `mypy --strict` and `ruff` clean. **Do not weaken anything those phases guarantee.**

Phase 3 delivers:
1. The real **ElevenLabs TTS adapter** behind the existing `ProviderGateway`.
2. A **local audio engine** (FFmpeg + stdlib only): library scan, deterministic mood-based music/ambience/SFX selection, a frame-exact timeline, narration stem, ducking, mixing, and two-pass −14 LUFS mastering.
3. Pipeline stages **S5 (TTS), S6 (timing reconciler), S7 (audio plan), S7b (audio mix)** and audio QC.
4. Hardening items from the Phase 1/2 audit (§D).

Priorities, unchanged: (1) never pay twice for the same result, (2) never exceed a budget cap, (3) any crash is recoverable, (4) clean, typed, documented code. New priority (5): **audio and video stay in sync by construction**.

## B. Scope

**In scope:** the ElevenLabs adapter and error mapping; voice assignment and audition CLI; library scan and tagging; the timeline module; line conforming; stages S5a/S5/S6/S7/S7b; envelope and sidechain ducking; mastering; audio QC; the migration; CLI and doctor additions; CI fixes; the hardening items; tests.

**Out of scope (do not create):** the video assembler, muxing, animatic (Phase 5), fal adapters, image/video stages (Phase 4), Streamlit UI (Phase 6), publishing, retention purge (Phase 7), vision QC, AI music generation, any LLM auto-editing of narration, and pydub/numpy dependencies.

## C. Discovery Protocol (do this first, before writing code)

Read the repository and write a short **"Discovery Report"** at the top of your final report. Confirm or correct each assumption below. **Where reality differs from this prompt, follow the existing code's conventions and record the difference.**

1. Phase 2 stage names, their `depends_on`, and which stage carries `gate_after=G1_SCRIPT`. S5a must depend on the stage *after which Gate 1 approval is complete*. Identify it.
2. Whether `narration_lines.text_final` holds the diacritized text for Arabic, and what `status` values lines have after Gate 1.
3. Whether the S2 shot schema emits `sfx_tags`, and where it is stored. **Do not modify S2 prompts or schemas in Phase 3.**
4. Which LLM/TTS adapters use an SDK versus raw `httpx`. The ElevenLabs adapter uses raw `httpx` with an injectable transport regardless (§H1).
5. Next migration number, existing `vocab.yaml` keys, and the existing `timing:` keys in `settings.yaml`. **Merge, don't duplicate.**
6. How the Phase 2 S4 estimator counts TTS characters (see D4), and how Phase 1 `Gateway.tts` treats `submit_started_at` for single-call providers (see D2).
7. How approval gate rounds are distinguished for `G3_FINAL_CUT` (finding #7). Report only; do not change it.

## D. Hardening Items (carry-over fixes, required)

**D1. CAS retention upgrade.** In `ContentStore.put_file/put_bytes`, on `assets` conflict the row's `retention` and `purge_after` must become the *longer-lived* of the old and new values (order: `permanent` > `keep_n_days` (later `purge_after` wins) > `intermediate`). Never downgrade. Test: put as `intermediate`, re-put as `permanent` → permanent; reverse order → still permanent.

**D2. Single-call crash semantics.** For LLM and TTS providers in `ProviderGateway`:
- Before invoking the provider, persist `submit_started_at` (a single statement), then `faults.crash_at("before_submit")`.
- After the request is sent and before the response is stored, call `faults.crash_at("during_single_call")` (new crash point).
- On resume, a `reserved` call with `submit_started_at` set and no completion reuses the reservation, appends a `potential_orphan` audit event (the provider may have billed), and re-invokes. This is a documented residual risk, as for job providers.

**D3. CI and doctor.**
- Add an FFmpeg install step to the CI matrix. Use `FedericoCarboni/setup-ffmpeg@v3` (pin to a commit SHA) on both OSes, then a **capability check step** that fails the job unless `ffmpeg -version` reports ≥ 5.0 and `ffmpeg -filters` lists: `loudnorm ebur128 astats sidechaincompress volume amix adelay afade atrim apad aresample aformat silenceremove areverse alimiter anullsrc asetnsamples ametadata pan`. Implement the same check as `tests/unit/test_ffmpeg_capabilities.py` and reuse it in `doctor`.
- `doctor`'s ffmpeg probe timeout becomes configurable (`settings.doctor.probe_timeout_s`, default 30).

**D4. Estimator character accounting.** The cost estimate for TTS must count `len(text_to_send)` exactly as S5 will send it (including diacritics, after the `tts.keep_diacritics` rule, §G). If Phase 2's S4 estimates from a different string, fix it and report. Test: S4 estimate equals S5's worst-case reservation total.

## E. Amendments: Migration `00N_audio.sql` (use the next number)

SQLite-compatible, `CHECK(json_valid(...))` on all JSON columns, money stays integer micro-USD.

```sql
ALTER TABLE narration_lines ADD COLUMN conformed_sha256 TEXT REFERENCES assets(sha256);
ALTER TABLE narration_lines ADD COLUMN conformed_samples INTEGER;           -- exact length at audio.sample_rate
ALTER TABLE narration_lines ADD COLUMN tts_nonce TEXT NOT NULL DEFAULT '0'; -- regeneration changes it; crash-resume reuses it
ALTER TABLE narration_lines ADD COLUMN voice_snapshot_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(voice_snapshot_json));

ALTER TABLE shots ADD COLUMN hold_frames INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shots ADD COLUMN timing_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(timing_status IN ('pending','locked','overflow'));
-- speed_factor (existing) = PLAYBACK SPEED in [1/max_stretch, 1.0]. effective_frames = ceil(clip_frames / speed_factor) + hold_frames.

ALTER TABLE audio_cues ADD COLUMN source_offset_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audio_cues ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(params_json));  -- loop flag, intensity envelope points
ALTER TABLE audio_cues ADD COLUMN plan_hash TEXT;

ALTER TABLE audio_library ADD COLUMN attribution_text TEXT;
ALTER TABLE audio_library ADD COLUMN scan_error TEXT;
-- For ambience and sfx, mood_tags_json holds their tags (single controlled-vocab column, documented in code).

CREATE TABLE audio_masters (
  id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL REFERENCES deliverables(id),
  input_hash TEXT NOT NULL, timeline_hash TEXT NOT NULL,
  master_sha256 TEXT NOT NULL REFERENCES assets(sha256),
  narration_stem_sha256 TEXT REFERENCES assets(sha256),
  total_samples INTEGER NOT NULL,
  loudness_i REAL, true_peak REAL, lra REAL, duck_depth_db REAL,
  stats_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(stats_json)),
  status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('ready','superseded','failed')),
  created_at TEXT NOT NULL, UNIQUE(deliverable_id, input_hash));
```

No schema change for voices. `character_voices.settings_json` is validated by a Pydantic `VoiceSettings`: `model_key` (default `tts.main`), `stability`, `similarity_boost`, `style`, `speaker_boost`, `speed`, `mix_gain_db` (default 0), `license_attested_at | null`.

## F. Engineering Rules (delta; Phase 1 rules still apply)

1. **No new runtime dependencies.** Allowed: stdlib (`wave`, `array`, `json`), `httpx`, existing deps. **Do not add pydub or numpy.**
2. FFmpeg is invoked only via `video/ffmpeg.py` (extend it as needed). Stages never import `subprocess`/`httpx`. The ElevenLabs adapter is the only new `httpx` consumer and lives in `providers/tts/`.
3. **No literal language codes** outside the allow-list (T-LANG-01 must still pass). Arabic-specific text handling lives in `language/arabic.py` and is reached through `language.normalize_for_tts(language, text, settings)`. Audio modules are language-agnostic.
4. **Long filtergraphs are written to a UTF-8 file** and passed with the version-appropriate option (`-/filter_complex <file>` on FFmpeg ≥ 7.0, otherwise `-filter_complex_script <file>`). Windows command lines cap near 32k characters. Detect the version once and cache it.
5. All FFmpeg calls use `-bitexact -fflags +bitexact -map_metadata -1` where outputs are persisted. All audio math is in **integer samples** at `audio.sample_rate`; milliseconds appear only at config and DB boundaries (round half up).
6. All new stage handlers are idempotent and obtain languages only from `ctx.languages`.
7. Temp files live in `data/projects/<slug>/work/` and are written atomically. Persisted outputs go through the CAS.

## G. Configuration

**Merge into `configs/settings.yaml`** (do not overwrite existing keys; report conflicts):

```yaml
doctor: {probe_timeout_s: 30}
render: {fps: 24}          # validated: 48000 % fps == 0 (also: 25, 30). Else ConfigError.
tts:
  request_timeout_s: 120
  use_context: true             # send previous_text/next_text when the model supports it
  context_max_chars: 300
  keep_diacritics: true         # false => send diacritics-stripped text (A/B knob; see `studio tts audition`)
  qc: {min_duration_ratio: 0.4, max_duration_ratio: 2.5, auto_retries: 1}
timing:                          # merge with existing keys
  max_stretch: 1.15
  max_hold_ms: 500
  j_cut_ms: 0                   # nonzero => ConfigError("not supported before Phase 5")
  prefer_stretch_to_save_cost: true
  estimate_drift_warn_pct: 25
audio:
  sample_rate: 48000
  library_root: library
  scan_extensions: [".wav", ".mp3", ".flac", ".ogg"]
  levels:                        # starting points, calibrate by ear
    speech_line_lufs: -20
    max_line_gain_db: 9
    music_lufs: -24
    ambience_lufs: -34
    sfx_lufs: -22
    intensity_offset_db: {1: -8, 2: -5, 3: -3, 4: -1, 5: 0}
    intensity_ramp_ms: 800
  speech_trim: {threshold_db: -50, keep_ms: 60}
  music:
    fade_in_ms: 1000
    final_fade_ms: 2000
    crossfade_ms: 1500
    recent_penalty_episodes: 3
    recent_penalty: 1.0
    loop_penalty: 0.5
    segment_intensity_break: 2
  sfx: {transition_enabled: true, transition_tag: transition, transition_offset_ms: -150}
  duck:
    mode: envelope               # envelope | sidechain
    depth_db: 10
    pre_ms: 150                  # ramp-down starts this long before speech
    attack_ms: 120               # ramp-down length; MUST be <= pre_ms (validated)
    release_ms: 450
    merge_gap_ms: 700            # speech gaps shorter than this stay ducked (no pumping)
    ambience_fraction: 0.5
    sidechain: {threshold: 0.05, ratio: 8, attack_ms: 20, release_ms: 350, makeup: 1}
  master: {target_lufs: -14, true_peak_db: -1.5, lra: 11, lufs_tolerance: 1.0, tp_ceiling_db: -1.0, clip_peak_db: -0.1}
  qc: {min_duck_db: 8, min_speech_to_music_db: 10, silent_line_rms_db: -45, window_ms: 100, exclude_onset_ms: 150}
```

**Registry:** add the entries from Part 2 to the shipped `model_registry.yaml`. Extend the registry Pydantic model with optional `tts_params` (`output_format`, `supports_context`, `supports_language_code`) and `limits`, keeping `extra="forbid"` elsewhere. Candidate entries are not referenced by profiles. `resolve_profile` rules from Phase 1 apply unchanged. **Do not guess prices or model IDs beyond what is written here.**

**`vocab.yaml`:** add `sfx_tags`, `ambience_tags` (≥ 6 each; include `transition`), and an optional `mood_affinity` map (`{mood: {neighbor_mood: 0.5}}`). Keep existing keys.

## H. Module Specifications

### H1. `providers/tts/elevenlabs.py`: real adapter (single-call `TTSProvider`)

- Raw `httpx.AsyncClient` with an **injectable transport**, so tests use `httpx.MockTransport`. The key is read from `ELEVENLABS_API_KEY` at call time and is never logged or stored.
- Request: `POST {base_url}/v1/text-to-speech/{voice_id}?output_format={tts_params.output_format}` with header `xi-api-key`, JSON body: `text`, `model_id`, `voice_settings` (`stability`, `similarity_boost`, `style`, `use_speaker_boost`, `speed`, only fields set), `seed` (from the request when present), `previous_text`/`next_text` **only if** `supports_context` and provided, `language_code` **only if** `supports_language_code`. ⚠ These field names and endpoints come from memory; verify them against the current API docs before finalizing and record what you verified in the report.
- Response: stream audio bytes to a temp file and return `TTSResult(payload=MediaPayload(local_path, ext="mp3", mime="audio/mpeg"), billed_chars)`. `billed_chars` = the `character-cost` response header if present and an integer, else `len(text)`. Capture the `request-id` header and place it in the result's `meta` so the Gateway can store it on `provider_calls.remote_request_id` (after completion, for dashboard reconciliation).
- **Error mapping** (⚠ verify against docs):

  | Condition | Raised |
  |---|---|
  | timeout, connection error, 5xx, 429, `detail.status` in {`too_many_concurrent_requests`, `system_busy`} | `TransientProviderError` |
  | `detail.status == "quota_exceeded"` | new `ProviderQuotaExceeded(PermanentProviderError)` |
  | 400, 401, 403, 404, 422, any other 4xx | `PermanentProviderError` (message includes status and `detail.status`, never the key) |
  | HTTP 200 with empty body | `ZeroByteAssetError` |

  The engine treats `ProviderQuotaExceeded` like a budget halt: run → `halted_budget`, stage_run → `pending`, an audit event with reason `provider_quota`, no retry loop.
- `TTSResult` flows through `pricing.actual_cost_musd` = `ceil(billed_chars × usd_per_1k / 1000)` in integer micro-USD.
- The factory maps `provider == "elevenlabs"` and `capability == tts` to this adapter. Other real providers still raise `ProviderNotImplementedError` as appropriate for their phase.

### H2. Voices and audition (CLI + service)

- `studio voice set <character_slug> --series <slug> --language <code> --voice-id <id> [--model-key tts.main] [--stability …] [--speed …] [--mix-gain-db …] [--attest-commercial]` upserts `character_voices` (validated via `VoiceSettings`). `--attest-commercial` stamps `license_attested_at` from the Clock. `studio voice list`.
- `studio tts audition --voice-id <id> --model-keys tts.main[,tts.candidate.flash,tts.candidate.v3] --lines-file <utf-8 file> [--no-diacritics-variant] --cap-usd 0.50` renders each (model × line × diacritics-variant) through the Gateway with `episode_id=None` (month/global budgets apply) plus an **additional hard cap** passed as a scoped call budget. Outputs go to `data/previews/audition/<timestamp>/` with a `README.txt` listing cost per file. The `--lines-file` is UTF-8 only, because Windows consoles mangle Arabic argv. Print the estimated total and require `--yes` to proceed.
- Fail closed for `final` profile runs: S5a (below) refuses any voice lacking `license_attested_at`.

### H3. `audio/library.py`: scan and validation

- `studio library scan` walks `library/{music,sfx,ambience}`, reads `index.yaml` sidecars (per-directory `defaults:` plus per-file entries; Pydantic-validated: `file`, `title`, `moods|tags` (controlled vocab), `intensity 1–5` (music), `bpm`, `loopable`, `license`, `license_url`, `attribution_required`, `attribution_text`, `source`), probes duration with ffprobe, hashes the file, and **measures integrated loudness** (`ebur128`) into `loudness_lufs`.
- Upsert is keyed by `sha256` (a renamed file updates `rel_path`). A missing file sets `active=0` but never deletes the row, because `audio_cues` reference it.
- A track becomes `active=1` only if it has a license, valid tags/intensity, a readable file, and a duration > 0. Otherwise `active=0` with `scan_error` set. **Selection only sees active tracks.**
- `studio library list [--kind] [--inactive]` and `studio library check` (prints coverage per mood × intensity and warns on empty cells).

### H4. `audio/timeline.py`: single source of truth for sync

```python
@dataclass(frozen=True)
class ShotSpan:  shot_id; idx; scene_id; start_frame; effective_frames
@dataclass(frozen=True)
class LinePlacement: line_id; shot_id; start_sample; length_samples; conformed_sha256
@dataclass(frozen=True)
class SceneSpan: scene_id; idx; start_frame; end_frame
@dataclass(frozen=True)
class Timeline: fps; sample_rate; shots; scenes; lines; total_frames; total_samples; timeline_hash
def build_timeline(repos, deliverable_id, settings) -> Timeline   # pure, deterministic
```

- Units: **frames** for video, **samples** for audio, with `samples_per_frame = sample_rate // fps` (an integer, validated).
- `clip_frames = round(clip_duration_ms × fps / 1000)`. `effective_frames = ceil(clip_frames / speed_factor) + hold_frames`. Shots are contiguous in `idx` order with hard cuts (no overlap) in Phase 3.
- Line placement inside shot `i`: `start = shot_start_samples + lead_in + Σ(previous line samples) + gaps`, using `lead_in_ms`, `line_gap_ms` and `tail_ms` from `timing` (rounded to samples). **Invariant:** the last line ends at or before `shot_end − tail`. A violation raises `TimelineOverflowError` listing shot and overflow in ms, and never silently truncates.
- `timeline_hash` is a sha256 over the canonical JSON of all spans and line shas. **Phase 5's assembler must use this same function for video concatenation. Document this in the module docstring.**
- Invariant for tests: shot spans are identical across all deliverables of the same track, regardless of language.

### H5. `audio/conform.py`: per-line local processing (free, deterministic)

For one raw TTS asset:
1. Decode to mono, `aresample` to `sample_rate`, 16-bit.
2. Trim leading and trailing silence (`silenceremove` forward, then `areverse` → `silenceremove` → `areverse`), keeping `speech_trim.keep_ms` of padding each side, with `threshold_db` from config.
3. Measure integrated loudness (`ebur128`). If the result is unusable (below −60 LUFS or too short), fall back to an RMS-based estimate (`astats`) with a documented constant offset in config-derived code.
4. Apply gain = `speech_line_lufs − measured + voice.mix_gain_db`, clamped to ±`max_line_gain_db`, followed by `alimiter` (ceiling −1 dBFS).
5. Store the WAV in the CAS (`kind=audio`, `retention=intermediate`) and return `(AssetRef, samples)`. Samples are read from the WAV header, not from ffprobe's rounded duration.

### H6. Stages S5a and S5

**`s5a_tts_preflight`** (scope episode, free, no gate). Depends on the post-Gate-1 stage (§C1). Fails with `PreflightError` *before any spend* if any of the following hold:
- a voice is missing for any (speaker, language) in use (list every missing pair at once)
- `final` profile and any used voice lacks `license_attested_at`
- the TTS model resolves to a placeholder
- `resolve_profile` fails
- the worst-case TTS reservation (Σ `len(text_to_send)` × price) exceeds the remaining episode/month/global budget (raise `BudgetExceeded` semantics, a halt, not a crash)

**`s5_tts`** (scope line, depends on S5a). For each line:
1. `text_to_send = language.normalize_for_tts(language, text_final, settings)` (honors `tts.keep_diacritics`).
2. Context: if `tts.use_context` **and** the model's `supports_context`: `previous_text` = tail of the previous line's text and `next_text` = head of the next line's text (same deliverable, ordered by shot idx then line idx, each truncated to `context_max_chars`), else `None`. Context is part of the cache key via `TTSRequest`.
3. Nonce = `narration_lines.tts_nonce`, **persisted before each paid call** (single statement). The Gateway call is `gateway.tts(voice.model_key, req, CallContext(episode_id, stage_run_id, nonce=line.tts_nonce))`.
4. Conform (H5), then write `audio_sha256`, `audio_duration_ms` (raw), `conformed_sha256`, `conformed_samples`, `voice_snapshot_json` (voice id, model id, settings, billed chars), status `voiced`.
5. **TTS QC:** compute `expected_s = word_count / timing.words_per_second[language]`. If `conformed_s / expected_s` is outside `[min_duration_ratio, max_duration_ratio]`, or the line's RMS is below `silent_line_rms_db`, treat as a technical failure: set `tts_nonce = f"{base}.q{n}"` (persisted), regenerate (paid, deliberately, at most `tts.qc.auto_retries` times), then fail the stage with a `TtsQualityError` naming the line and measurements. Cached good lines are not repaid on resume.
6. Retention: raw TTS audio is stored with `retention=keep_n_days` (`keep_narration_audio_days`) so re-mixing never repays.

### H7. Stage S6: timing reconciler

Scope **track** (free, local; aggregating so the operator sees *all* overflows at once). Depends on S5. For each shot in the track:

```
need_frames = max over the track's deliverables of:
              ceil((lead_in + Σ line_samples + Σ gaps + tail) / samples_per_frame)
              and shot.min_duration_ms converted to frames
for each supported duration d (ascending) with clip_frames(d):
    no_stretch_ok(d)  = need_frames <= clip_frames(d)
    stretch_ok(d)     = need_frames <= ceil(clip_frames(d)/(1/max_stretch)) + hold_frames_max
choose:
    d_plain   = smallest d with no_stretch_ok
    d_squeeze = smallest d with stretch_ok            (only if timing.prefer_stretch_to_save_cost)
    d = min(d_plain, d_squeeze) -> if d_squeeze < d_plain use d_squeeze else d_plain
    none fits -> overflow
given d: speed = 1.0 if need <= clip else max(1/max_stretch, clip/need)
         effective = ceil(clip/speed); hold = max(0, need - effective)   # hold <= max_hold_ms
```

Write `clip_duration_ms` (the *requested generation duration*, a supported value), `speed_factor`, `hold_frames`, `timing_status='locked'`. **Never edit narration text.** If any shot overflows: mark those `timing_status='overflow'` and fail the stage with `TimingOverflowError` listing every overflowing shot (id, idx, needed ms, max capacity ms, offending lines). Provide `studio narration show <episode>` and `studio narration edit <line_id> (--text TEXT | --file PATH)` (UTF-8, reuse Phase 2 equivalents if present). The edit writes `text_final`, sets `tts_nonce` unchanged (new text means a new cache key anyway) and appends an `audit_events` row `post_gate_edit` with the old and new text hashes, then the operator runs `studio resume`.

Output JSON also includes `projected_video_seconds` and `projected_video_cost_musd` (Σ `clip_duration` × registry per-second price for the profile's video model, worst case with the configured retry factor if one exists in Phase 2 settings). Compare with the Gate-1 estimate stored by S4. If drift exceeds `timing.estimate_drift_warn_pct`, append an audit event `estimate_drift` with both numbers. Phase 5/6 must show these at Gate 2. **Regenerating a clip must never change the locked duration** (a Phase 4 invariant; document in the module).

### H8. Stage S7: audio plan (deterministic, seeded)

Scope deliverable, depends on S6. Writes `audio_cues` rows (all with `plan_hash`). Pure function of (timeline, scenes, active library, recent-use history snapshot, config), with `input_hash` covering all of them, so recomputation happens only when something changed.

**Music:**
1. Segment scenes: consecutive scenes merge into one segment when `music_mood` is equal and `|Δintensity| < segment_intensity_break`.
2. For each segment, score every active music track: `3 × mood_match − |intensity_diff| − recent_penalty − loop_penalty − same_as_previous_segment_penalty`. `mood_match` is 1.0 for an exact tag, the `mood_affinity` value for a neighbor, else 0. `recent_penalty` applies to tracks used in the last `recent_penalty_episodes` episodes of the series (from `audio_cues` joined to episodes). `loop_penalty` applies when the track is shorter than the segment but loopable.
3. Tie-break by ascending `sha256(f"{episode_id}:{segment_idx}:{track_id}")`, with **no RNG**. The seed is per episode, not per deliverable, so every language of an episode gets the same music.
4. Coverage: if the chosen track is shorter than the segment and not loopable, chain the next-best track(s) until covered. Adjacent cues overlap by `crossfade_ms` (the earlier cue's end extends by half, the later cue's start moves earlier by half, equal-power fades via fade fields). Loopable tracks set `params.loop=true`.
5. Per-cue gain: `gain_db = music_lufs − track.loudness_lufs` (clamped ±12) plus intensity offsets as breakpoints (`params.envelope`, linear ramps of `intensity_ramp_ms` at scene boundaries). First cue: `fade_in_ms`. Final cue: fade out over `min(final_fade_ms, total/4)` ending exactly at `total_samples`.
6. If no track matches any mood for a segment, pick the closest by intensity and record a `qc_results` warn `music_no_mood_match`. If the library has zero active music tracks, raise `LibraryEmptyError` (fail closed).

**Ambience:** per segment of equal scene `ambience_tags`, pick the best tag-overlap *loopable* ambience track, level `ambience_lufs`, same crossfade rule. No tags means no ambience cue.

**SFX:** (a) if `audio.sfx.transition_enabled`, one `transition`-tagged SFX at each scene boundary (not the first), offset by `transition_offset_ms`, chosen deterministically as above; (b) per-shot SFX only if the shot has `sfx_tags` (per §C3). If it doesn't, do not invent a source, and report that in the Discovery Report. Level `sfx_lufs`.

**Licensing:** every selected library item must have a license record (guaranteed by `active`). The plan stage appends `qc_results` info rows listing items with `attribution_required`.

### H9. `audio/envelope.py`, `audio/ducking.py`, `audio/mixer.py`

**Envelope helper:** `build_gain_expr(points: list[tuple[float_seconds, gain_db]]) -> str`. Piecewise-linear in dB, constant outside the range, with strictly increasing times (nudge duplicates by one sample). Output is an FFmpeg expression using `lerp`, `if`, `lt`, `gt` in a **balanced binary decision tree** (depth log₂ n, never a linear chain). It is applied via `asetnsamples=n=480:p=0,volume='pow(10,(EXPR)/20)':eval=frame` (10 ms frames, which avoids audible stepping on the ramps). The expression is quoted inside the filtergraph *file*. A unit test round-trips a 2,000-point envelope through FFmpeg and checks sampled gains.

**Duck envelope (mode `envelope`, default):** from the timeline's line placements: merge speech spans whose gap is below `merge_gap_ms`, then for each merged span emit breakpoints: 0 dB at `start − pre_ms`, `−depth_db` at `start − pre_ms + attack_ms`, hold, `−depth_db` at `end`, back to 0 dB at `end + release_ms`. Spans overlapping are merged first. The envelope multiplies the music bus; ambience gets `ambience_fraction × depth_db`.

**Sidechain mode (`sidechain`):** `[music_bus][narr_key]sidechaincompress=threshold=…:ratio=…:attack=…:release=…:makeup=…` using the narration stem as the key (`asplit` for key vs. mix). Both modes must satisfy the same QC.

**Mixer (single FFmpeg run, filtergraph in a file):**
1. Inputs: the narration stem WAV, plus music/ambience/SFX cue files.
2. Each cue: `atrim` from `source_offset_ms`, loop via `-stream_loop -1` on the input when `params.loop`, `aformat` to stereo float at `sample_rate`, cue gain/envelope, `afade` in/out (`curve=qsin`), then `adelay` to its timeline start (millisecond precision is acceptable for beds and SFX; speech is sample-exact elsewhere).
3. Sum cues per bus with `amix=normalize=0:duration=longest`. Apply ducking to the music (and ambience) buses. Narration goes mono→stereo with `pan=stereo|c0=c0|c1=c0`. Sum all buses with `amix=normalize=0`.
4. Force exact length: `apad=whole_len=<total_samples>,atrim=end_sample=<total_samples>`.
5. Output a temp **premaster** WAV (`pcm_s24le`).

**Narration stem (`audio/stem.py`):** assemble with stdlib `wave` as a mono 16-bit WAV of exactly `total_samples`: write silence and each conformed line's frames at its `start_sample`. No FFmpeg is involved. The result is sample-exact by construction.

### H10. `audio/mastering.py`

Two-pass `loudnorm` on the premaster: pass 1 with `print_format=json` (parse the JSON from stderr robustly: take the last `{…}` block), pass 2 with the `measured_*` values, `linear=true`, targets from `audio.master`. Then **re-conform the length**: `aresample=<sample_rate>,apad=whole_len=N,atrim=end_sample=N` (loudnorm resamples internally and can change length by a few samples). Output a mastered **FLAC** (24-bit). Verify via `ebur128`: integrated within `target_lufs ± lufs_tolerance` and true peak ≤ `tp_ceiling_db`; also `astats` peak ≤ `clip_peak_db`. Failure raises `MasteringError` (the stage fails). *Note for Phase 5: AAC encoding can overshoot true peak by ~0.5 dB, so the muxed file must be re-measured.*

### H11. `qc/audio.py`: persisted to `qc_results`

| Check | Method | Fail |
|---|---|---|
| unreadable / zero-byte | size + ffprobe | any |
| exact length | master samples vs `timeline.total_samples` | any difference |
| narration presence | per-line window RMS in the final mix | any line < `silent_line_rms_db` |
| loudness | `ebur128` | outside `target ± tolerance` |
| true peak / clipping | `ebur128`, `astats` | above ceilings |
| **duck depth** | 100 ms RMS series (`asetnsamples` + `astats=metadata=1:reset=1` + `ametadata=print:file=-`) of the music bus pre-duck vs post-duck, averaged over speech windows excluding `exclude_onset_ms` | < `min_duck_db` |
| speech-to-music ratio | same RMS series, speech windows | < `min_speech_to_music_db` |
| plan coverage | cues vs timeline | uncovered music span (unless no-mood-match warn), cue beyond timeline |
| license completeness | cues → library | missing license |

Parsing helpers live in `audio/analysis.py` (`rms_series`, `loudness`, `peaks`) and are unit-tested on synthetic signals.

### H12. Stage S7b: audio mix

Scope deliverable, depends on S7 and S5. `input_hash` covers the timeline hash, the cue plan hash, the ducking/master config, and the sha256 of every input asset. If an `audio_masters` row already exists for `(deliverable_id, input_hash)` with a live asset, reuse it. Otherwise: build the stem, build the graph file, render the premaster, master, QC, then insert `audio_masters` (and mark older rows `superseded`). The master FLAC and the narration stem are stored with `retention=intermediate`. Failures never leave a half-written file at a CAS path. Phase 5's animatic and assembler read `audio_masters.master_sha256` and `timeline_hash`.

### H13. CLI and doctor additions

- New: `studio library scan|list|check`; `studio voice set|list`; `studio tts audition`; `studio narration show|edit`; `studio audio preview <deliverable_id> [--stem master|narration]` (copies to `data/previews/` with a readable name); `studio audio qc <deliverable_id>`; `studio audio calibrate [--episode <id>]`.
- `calibrate` reads `narration_lines` with `audio_duration_ms` and word counts and **prints** measured words-per-second per language and a suggested `timing.words_per_second`. It never writes config. Word counting uses the Phase 2 diacritic-aware counter.
- `doctor` adds: ffmpeg ≥ 5.0 and required filters; FFmpeg version and filtergraph-file option; library status (active/inactive counts, empty mood cells); `ELEVENLABS_API_KEY` presence; voices without attestation; placeholder or unverified TTS model; fps divides sample rate; `tts.keep_diacritics` informational.

### H14. DAG integration

Register S5a → S5 → S6 → S7 → S7b in the existing DAG using the dependency discovered in §C1. **No new gates.** Update the demo/mock pipeline so a sandbox run exercises the full audio path with the mock TTS provider and a generated test library. Update `studio run` so `--languages ar,en` still doubles only TTS and mix work (video-side stages are unaffected).

## I. Test Specification

Everything runs offline by default. The mock TTS provider produces sine tones and `httpx.MockTransport` simulates ElevenLabs. Library fixtures are generated at test time with FFmpeg `lavfi` (tagged sine/noise tracks plus `index.yaml`). FFmpeg-dependent tests carry `@pytest.mark.ffmpeg`. **All of the following are mandatory** (plus every Phase 1/2 test, unchanged and green).

**Hardening**
| ID | Assertion |
|---|---|
| T-CAS-03 | Retention upgrade on conflict, both orders (D1). |
| T-REC-02 | Parametrized over the TTS and LLM mock single-call providers × crash points {`after_reserve`, `before_submit`, `during_single_call`, `after_store_before_complete`}: the crash, a new Gateway on the same DB, and the repeat call give exactly one settled cost and one cached asset. `during_single_call` yields exactly one `potential_orphan` event, with a documented second provider invocation and no dangling reservation. |
| T-PRICE-02 | S4's TTS estimate equals S5's worst-case reservation sum, counting diacritics (D4). |
| T-CI-01 | The FFmpeg capability test passes (§D3). |

**ElevenLabs adapter** (`httpx.MockTransport`)
| ID | Assertion |
|---|---|
| T-EL-01 | Correct URL, query `output_format`, `xi-api-key` header, and body fields. `previous_text`/`next_text` appear only when `supports_context`. `language_code` only when `supports_language_code`. |
| T-EL-02 | The error mapping table, row by row, including the `detail.status` cases and the empty-200 body. |
| T-EL-03 | The API key never appears in `provider_calls.request_json`, the audit table, logs (`caplog`), or exception messages. |
| T-EL-04 | `billed_chars` uses the header when valid, else `len(text)`. The settled cost is an integer matching `ceil(chars × price/1000)` in micro-USD. |
| T-EL-05 | `ProviderQuotaExceeded` halts the run (`halted_budget`, audit reason `provider_quota`) and is not retried. |
| T-EL-06 (opt-in) | `STUDIO_LIVE_TTS=1` + key: synthesize one short Arabic sentence under a $0.50 cap, assert non-empty audio, ledger settle equals chars × price, and print the `request-id`. Skipped otherwise, with an explicit message. |

**Library and timeline**
| ID | Assertion |
|---|---|
| T-LIB-01 | Scan registers valid tracks active, marks missing-license/invalid-tag/unreadable ones inactive with `scan_error`, is idempotent, handles a renamed file by sha, and a deleted file sets `active=0` without deleting the row. |
| T-LIB-02 | Loudness is measured within ±0.5 LU of a known generated signal. |
| T-TL-01 | `build_timeline` is deterministic. Shots are contiguous. `effective_frames` follows the formula. The timeline hash changes when any shot or line changes. |
| T-TL-02 | Shot spans are identical across all deliverables of a track in different languages. |
| T-TL-03 | Overflowing narration raises `TimelineOverflowError` with exact numbers, and nothing is truncated. `48000 % fps != 0` raises `ConfigError`. |

**Conform / S5**
| ID | Assertion |
|---|---|
| T-CONF-01 | Leading/trailing silence is trimmed to `keep_ms` ± 10 ms. Gain is clamped. Peak ≤ −1 dBFS. The sample count in the DB equals the WAV header. |
| T-S5-01 | Preflight lists every missing voice at once and spends nothing (`submit_count == 0`). A `final` run with an unattested voice fails. An over-budget worst-case halts before the first call. |
| T-S5-02 | Resuming after a crash mid-stage repays zero completed lines. `tts_nonce` persists across the crash. |
| T-S5-03 | A TTS QC failure (mock returns silence, then bad duration) triggers exactly `auto_retries` paid regenerations with new nonces, then fails with `TtsQualityError`. Earlier lines are not repaid on resume. |
| T-S5-04 | Context text is truncated to `context_max_chars`, appears in the cache key, and a changed neighbor changes only the affected cache keys. `keep_diacritics=false` strips diacritics and produces different keys. |
| T-S5-05 | `--languages ar,en`: TTS calls double, voices resolve per language, with no literal language codes in `src/studio/audio/**` (T-LANG-01 still green). |

**Timing (S6), table-driven (24 fps, durations [5, 10], `max_stretch` 1.15, `max_hold_ms` 500)**
| ID | Case | Expected |
|---|---|---|
| T-S6-01 | need 4.2 s | d=5, speed 1.0, hold 0 |
| T-S6-02 | need 5.6 s, prefer_stretch=true | d=5, speed ≈ 0.893, hold 0 |
| T-S6-03 | need 5.6 s, prefer_stretch=false | d=10, speed 1.0 |
| T-S6-04 | need 6.2 s | d=5, speed = 1/1.15, hold fills the remainder (≤ 500 ms) |
| T-S6-05 | need 6.4 s | d=10, speed 1.0 |
| T-S6-06 | need 12.1 s | overflow, with every overflowing shot reported in one error, narration text unchanged |
| T-S6-07 | two deliverables in a track | the longer language drives the duration |
| T-S6-08 | drift | `estimate_drift` audit event appears when projected cost exceeds Gate-1 by > 25%, and output JSON has projected seconds and cost. |
| T-S6-09 | `studio narration edit --file` | updates `text_final`, writes the `post_gate_edit` audit row with hashes, and `resume` re-voices only that line (earlier lines unpaid again). A UTF-8 Arabic file round-trips on Windows. |
| T-S6-10 | `j_cut_ms != 0` | `ConfigError`. |

**Plan, envelope, ducking, mix, mastering**
| ID | Assertion |
|---|---|
| T-SEL-01 | Selection is deterministic across runs and across languages of the same episode. No `random`, and tie-breaks follow the hash order. |
| T-SEL-02 | The recent-use penalty steers away from tracks used in the last N episodes. Mood neighbors give the configured affinity. No mood match produces a `music_no_mood_match` warn. An empty library raises `LibraryEmptyError`. |
| T-SEL-03 | Coverage: loopable tracks loop, and non-loopable short tracks chain with `crossfade_ms` overlap. Segments merge per the break rules. The final fade ends exactly at `total_samples`. |
| T-SEL-04 | A transition SFX is placed at each non-first scene boundary. With no `sfx_tags` on shots, no per-shot SFX is invented. |
| T-ENV-01 (ffmpeg) | A 2,000-point envelope renders correctly. Sampled gains match within 0.3 dB. Expression depth is O(log n). |
| T-DUCK-01 (ffmpeg) | Envelope mode: music is attenuated ≥ `min_duck_db` in speech windows and within 1 dB of unducked outside the pre/release margins. Short gaps (< `merge_gap_ms`) stay ducked. |
| T-DUCK-02 (ffmpeg) | Sidechain mode also meets `min_duck_db` per the QC method. |
| T-STEM-01 | The narration stem has exactly `total_samples` samples. Each line's samples sit at `start_sample` (byte-compare), and everything else is zero. |
| T-MIX-01 (ffmpeg) | A full sandbox mix yields exactly `total_samples`, a stereo master FLAC, and an `audio_masters` row. |
| T-MIX-02 (ffmpeg) | Mastered integrated loudness is −14 ± 1 LUFS and true peak ≤ −1.0 dBTP. No clipping. |
| T-MIX-03 | Resuming S7b with unchanged inputs re-renders nothing (the same `audio_masters` row). Changing one narration line changes `input_hash` and supersedes the old row. |
| T-MIX-04 | A graph longer than 40,000 characters (synthetic: 300 SFX cues + 150 duck spans) renders on both OSes via the filtergraph file. |
| T-MIX-05 (slow) | A synthetic 20-minute timeline (300 lines, 40 scenes) mixes and passes QC. `@pytest.mark.slow`, excluded from default CI and run on demand. |
| T-QCA-01 | Each QC check fails on a purpose-built bad input (silent line, clipped mix, un-ducked mix, overshooting loudness, short master) and persists its `qc_results`. |

**Pipeline integration**
| ID | Assertion |
|---|---|
| T-ENG-12 | With the sandbox profile and `['ar']`: the full DAG including audio runs end to end. With `['ar','en']`: TTS and mix counts double, image/video call counts are unchanged, and the video timelines are identical. |
| T-ENG-13 | A crash at a random point inside S7b (`SimulatedCrash`), then restart: completes, and zero TTS lines are repaid. |
| T-CLI-03 (ffmpeg) | `library scan` → `voice set` → sandbox `run` → `audio preview` produces a playable file, and `doctor` reports the new checks. |

**Coverage:** ≥ 90% line coverage for `studio.core`, `studio.db`, `studio.pipeline`, **and `studio.audio` and `studio.providers.tts`**. The Phase 1/2 thresholds are unchanged.

## J. Working Method and Output

Implement in this order. After each step run `ruff`, `mypy --strict`, and that step's tests.

1. Discovery Report (§C), then D1, D2 and the migration
2. `providers/tts/elevenlabs.py`, registry/settings/vocab updates, factory wiring
3. `audio/analysis.py`, `library.py`, `timeline.py`, `conform.py`, `stem.py`
4. Stages S5a/S5, then S6 with `narration` CLI
5. `selector` + S7
6. `envelope`, `ducking`, `mixer`, `mastering`, `qc/audio.py`, then S7b
7. CLI/doctor, D3 (CI), D4, README section "Audio engine: library format, levels, ducking modes, how to audition voices"

**Output rules**
- Complete file contents, one block per file, labeled with its path. No "…unchanged" placeholders.
- If you cannot execute code or reach the network, say so and mark affected tests **"unverified"** instead of claiming they pass. Do not fabricate ElevenLabs API details. Anything unverified against live docs goes in "Known limitations".
- Do not add features outside §B. When ambiguous, choose the option that spends less or fails closed, and record it under "Decisions".

**Final report (required):** (1) Discovery Report; (2) test results by ID; (3) coverage numbers; (4) deviations and why; (5) decisions on ambiguities; (6) known limitations, including every ElevenLabs field or error mapping that is unverified; (7) exact commands for the audition → benchmark flow on Windows and Linux.

## K. Definition of Done

- All §I tests pass on Ubuntu and Windows in CI, coverage thresholds met, and `ruff` and `mypy --strict` are clean.
- A sandbox run, Arabic-only, completes S1–S7b. The master FLAC has exactly `total_samples`, −14 ± 1 LUFS, and ≥ 8 dB ducking.
- The same run with `--languages ar,en` doubles only TTS and mix work.
- `after_submit_before_persist` (job providers) and `during_single_call` (single-call providers) are the only crash points that can double-invoke a provider, and each logs exactly one `potential_orphan`.
- `studio doctor` prints actionable results for FFmpeg capabilities, the library, voices, and the TTS registry entries.
- No new dependencies. No `pydub`/`numpy`. No SDK imports in `src/` (except Phase 2's existing ones, in their adapters).

# ═══════════ END PROMPT ═══════════

---

## After the coding LLM returns

1. Run `pytest`, `ruff`, `mypy --strict`, and `studio doctor` yourself, and don't accept a "pass" without the output.
2. Run `studio tts audition` on your Fusha test lines with the candidate models, **with and without diacritics, and listen**. That choice (model, `keep_diacritics`) matters more for Arabic quality than anything else in this phase. Update `tts.main`, then set `pricing_verified_on` once you've checked the price against your dashboard.
3. Run one live T-EL-06 (cap $0.50) and compare the billed characters against the ElevenLabs dashboard. This validates my diacritic-cost assumption (finding #5).
4. Send me the Phase 3 report, including the **Discovery Report**, your answer on **finding #6 (16 vs. 14 crash scenarios)** and **#7 (gate rounds)**, and your decision on **#8 (draft creative routing)**. Then I'll write the **Phase 4 prompt**: fal adapters, reference-guided keyframes, image-to-video clips, the prompt sanitizer, and the QC retry policy.




