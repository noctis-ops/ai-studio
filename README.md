# AI Studio — Phase 5 (render, export, bible, retention)

A local, single-operator pipeline that will orchestrate paid generative-AI APIs to
produce cinematic animated videos. **Phase 5 completes the tail of the DAG: the
S9 animatic (gate G2), the S11 assembly, the S12 render QC (gate G3), the S13
free export (MP4 + sidecar + thumbnail candidates), the S14 series-bible
proposals and the S15 publish marker / retention purge.** The paid media path
(Phase 4: raw httpx fal.ai adapter, s4p/s10p preflights, S8a/S8/S10, visual QC,
deterministic conform, gates G0/G2) is unchanged and still mock-only in tests.
The demo pipeline runs on mock providers, and that is what the test suite
exercises. Nothing here spends real money until the fal endpoints, prices and
licences in `configs/model_registry.yaml` are confirmed by an operator
(`studio doctor` lists every unconfirmed entry).

Priorities, in order:

1. Never pay twice for the same result (content-addressed cache + idempotent jobs).
2. Never exceed a budget cap (atomic reservations, monthly + per-episode caps, kill switch).
3. Any crash is recoverable (submit/poll lifecycle with crash injection points and a recovery sweep).
4. Clean, typed, documented code (ruff + mypy strict).

## Repository layout

```
configs/            Default configuration (settings.yaml, model_registry.yaml,
                    channel_profiles.yaml, vocab.yaml)
src/studio/
  cli.py            Typer CLI: init, doctor, run, worker, resume, db, gates, budget,
                    voice, audio, shot, media, export, render, bible, publish, purge
  config/           Loader with ${STUDIO_HOME} interpolation and validation
  core/             Gateway, budget ledger, CAS, canonical JSON/cache keys,
                    pricing, retry, rate limiter, ids, clock, fault injection
  db/               SQLite connection (WAL, FK on, busy_timeout=5000), migrations, repos
  demo/             Demo seed + demo stages (LLM → TTS → keyframes → clips → S9–S13)
  domain/           Money (integer micro-USD), enums, errors, refs (SubjectRef lineage)
  pipeline/         DAG, engine (invalidation/gates/budget halt), worker, recovery,
                    stages_audio (S5a preflight, S5 TTS, S6 timing, S7 plan, S7b mix)
  assemble/         S11: segment plan, concat copy-join or re-encode, audio mux
  video/            ffmpeg/ffprobe wrappers, geometry, encoders, animatic + QC probes
  export/           S13: sidecar, thumbnail candidates, slug/limits, preset matrix
  bible/            S14: schema/evidence validation, proposal propose/accept/reject
  retention/        S15: publish marker, protection index, purge plan/run, GC
  audio/            Audio engine: analysis, library, timeline, conform, stem, selector,
                    envelope, ducking, mixer, mastering (stdlib + ffmpeg only)
  qc/               Persisted audio QC (qc_results)
  providers/        Base provider contracts + mocks (LLM, TTS, image, video),
                    providers/tts/elevenlabs.py (raw httpx, injectable transport)
tests/              §I test suite (offline, mock providers, FakeClock)
configs/            Defaults you are expected to edit for real budgets/providers
```

## Setup

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
sudo apt-get install -y ffmpeg   # macOS: brew install ffmpeg
.venv/bin/studio --help
```

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -e .[dev]
choco install ffmpeg             # or download from ffmpeg.org and add to PATH
studio --help
```

Python ≥ 3.11 is required. `ffmpeg`/`ffprobe` are required for the video demo path
and for the `@pytest.mark.ffmpeg` tests (those tests auto-skip with a clear message
if the binaries are missing).

## Configuration switches

Everything lives under `$STUDIO_HOME` (defaults to the repository root when unset).
Copy and edit the `configs/` files, or leave them as-is for the free sandbox demo.

| Switch | File | Default | Notes |
|---|---|---|---|
| `languages` | `settings.yaml` | `[ar]` | `ar`, `en`, or both. Arabic-only is the launch config. |
| `aspects` | `settings.yaml` | `[9:16]` | `9:16`, `16:9`, or both. |
| `default_profile` | `settings.yaml` | `draft` | `sandbox` \| `draft` \| `final`. |
| `budgets.default_episode_cap_usd` | `settings.yaml` | `10.0` | Per-episode cap. |
| `budgets.monthly_cap_usd` | `settings.yaml` | `150.0` | Per-UTC-month cap. |
| `budgets.warn_pct` | `settings.yaml` | `80` | One `budget_warning` audit event per scope when crossed. |
| `gates.allow_auto_approve_in` | `settings.yaml` | `[sandbox, draft]` | The `final` profile **never** auto-approves. |
| Model registry + prices | `model_registry.yaml` | placeholders, zero prices | `draft`/`final` refuse to run until real prices and model IDs are entered. |

`${STUDIO_HOME}` inside YAML values is interpolated from the environment; an unset
variable raises `ConfigError`, and unknown keys are rejected.

> The shipped registry contains **unverified model IDs and zero prices** on purpose.
> `studio doctor` prints warnings for these. `draft` and `final` runs are blocked
> by design until you enter real prices (with `pricing_verified_on`) and real model
> IDs. `sandbox` runs entirely on mocks and costs 0.

## Running the demo (Linux / macOS)

```bash
export STUDIO_HOME="$PWD"
.venv/bin/studio init                       # create layout + migrate DB (idempotent)
.venv/bin/studio doctor                     # environment checks
.venv/bin/studio run --demo --profile sandbox \
    --languages ar                           # prints "run id: <id>"
.venv/bin/studio worker --until-idle        # advances until a gate pauses the run
.venv/bin/studio gates list                 # shows every pending round, oldest first
# Gate order is fixed: G1 script -> G1 metadata text -> G0 sheets -> G2 animatic -> G3 render.
.venv/bin/studio gates approve <G1_SCRIPT_ID>
.venv/bin/studio worker --until-idle        # ...until the metadata text approval
.venv/bin/studio gates approve <G1_METADATA_ID>
.venv/bin/studio worker --until-idle        # ...until the G0_CHARACTERS gate
.venv/bin/studio gates approve <G0_ID>
.venv/bin/studio worker --until-idle        # ...until the G2_KEYFRAMES gate (on the animatic)
.venv/bin/studio gates show <G2_ID>         # cost panel: spent, projected, remaining cap
.venv/bin/studio gates approve <G2_ID>
.venv/bin/studio worker --until-idle        # ...until the G3_FINAL_CUT gate (on the render)
.venv/bin/studio gates approve <G3_ID> --checklist-file checklist.json
.venv/bin/studio worker --until-idle        # run reaches status=completed
.venv/bin/studio export <RUN_ID>            # writes exports/<channel_key>/<date>_<slug>/
```

`auto_approve` is a **sandbox/draft** convenience only (`gates.allow_auto_approve_in`);
the `final` profile never auto-approves and the G3 checklist must be filled by a
human. The exported MP4 is copied from the approved render asset, and its sha256
is re-verified after the copy. Inspect the final render with
`studio media preview <RUN_ID> --stage render`.

## Running the demo (Windows / PowerShell)

```powershell
$env:STUDIO_HOME = (Get-Location).Path
studio init
studio doctor
studio run --demo --profile sandbox --languages ar
studio worker --until-idle
studio gates list
studio gates approve <G1_ID>
studio worker --until-idle
studio gates approve <G2_ID>
studio worker --until-idle
studio gates approve <G3_ID>
studio worker --until-idle
```

Arabic + English double-language run (TTS calls double; image and video call
counts are identical to the Arabic-only run — see T-ENG-08):

```bash
.venv/bin/studio run --demo --profile sandbox --languages ar,en
```

## Testing

```bash
python -m pytest tests/ -q                 # full suite (offline; mocks only)
python -m pytest tests/ -q -m ffmpeg       # only tests that need ffmpeg/ffprobe
python -m pytest tests/ -q --cov=studio.core --cov=studio.db --cov=studio.pipeline --cov-report=term-missing
ruff check src tests
mypy
```

Coverage gate: **≥ 90% line coverage** on `studio.core`, `studio.db`,
`studio.pipeline`, `studio.audio` and `studio.providers.tts` (currently 92.97%
total; CI enforces `--cov-fail-under=90` and runs `pytest -m "not slow"`).

## Audio engine

Phase 3 adds a fully local, deterministic audio path alongside the pre-existing
pipeline stages. Everything under `src/studio/audio/` is language-agnostic
(Arabic normalization happens in `language/arabic.py` before anything reaches the
engine), all audio math is in integer samples at `audio.sample_rate` (48 kHz;
`render.fps` must divide it exactly or the config fails to load), and every
persisted output goes through the CAS with `-bitexact`.

### Library format

```
library/{music,sfx,ambience}/<track>.{wav,mp3,flac,ogg}
library/{music,sfx,ambience}/index.yaml
```

Each `index.yaml` has a per-directory `defaults:` block plus a `tracks:` list.
Per track: `file`, `title`, `moods` (music; controlled vocab from `vocab.yaml`)
or `tags` (sfx/ambience), `intensity` 1–5 (music), `bpm`, `loopable`, `license`,
`license_url`, `attribution_required`, `attribution_text`, `source`.

```bash
studio library scan     # probes loudness (ebur128) + duration, registers sha256
studio library list     # --kind music|sfx|ambience, --inactive for skipped tracks
studio library check    # coverage grid per mood x intensity
```

Only **active** tracks are selected: a track needs a license, valid tags, a
readable file and a duration > 0, otherwise it is stored inactive with
`scan_error` set. A renamed file updates its row (keyed by sha256); a deleted
file flips `active=0` but is never removed (`audio_cues` reference it).

### S5/S6: voices, narration, timing

- `studio voice set <character_slug> --series <slug> --language <code>
  --voice-id <id> [--stability ...] [--speed ...] [--mix-gain-db ...]
  [--attest-commercial]` — `--attest-commercial` is required before `final`
  runs will pass S5a preflight. `studio voice list`.
- `studio tts audition --voice-id <id> [--model-keys tts.main,...]
  --lines-file <utf-8 file> [--no-diacritics-variant] [--cap-usd 0.50]`
  renders (model × line × diacritics) variants through the gateway into
  `data/previews/audition/<ts>/` with a `README.txt` of per-file cost. Prints
  the worst-case total first and requires `--yes` to proceed. This is the
  A/B knob for Arabic tashkeel on/off (`tts.keep_diacritics` in
  `configs/settings.yaml`).
- Narration overflow is **never** fixed by editing your text automatically.
  S6 fails closed with a per-shot overflow report; fix the line yourself:

```bash
studio narration show <episode_id>              # lines + status
studio narration edit <line_id> --file edit.txt # UTF-8; writes text_final,
                                                # audits post_gate_edit w/ hashes
studio resume <run_id>                          # re-voices only that line
```

- `studio audio calibrate [--episode <id>]` prints measured words/second per
  language and a suggested `timing.words_per_second`; it never writes config.

### Ducking, mixing, mastering

- Music/ambience/SFX selection is deterministic (no RNG: per-episode hash
  tie-break) and identical across the languages of an episode.
- The narration stem is assembled with stdlib `wave` (sample-exact); the mix
  is one ffmpeg filtergraph written to a file (`-/filter_complex` on
  ffmpeg ≥ 7.0, `-filter_complex_script` otherwise).
- Ducking: `envelope` (default, piecewise-linear gain curve on the music bus;
  ambience gets `ambience_fraction × depth_db`) or `sidechain`
  (`sidechaincompress` keyed by the narration stem). Both meet the same QC.
- Mastering: two-pass `loudnorm` to −14 LUFS / −1.5 dBTP, then re-conformed to
  exact length, verified by `ebur128` + `astats` (else `MasteringError`).
- `studio audio preview <deliverable_id> [--stem master|narration]` copies the
  result to `data/previews/`; `studio audio qc <deliverable_id>` re-prints the
  persisted `qc_results` rows (duck depth, speech-to-music ratio, loudness,
  true peak, plan coverage, license completeness, per-line narration RMS).

### What is *not* verified live

The ElevenLabs adapter is built to the documented endpoint shape but has never
been run against the live API from this repo. Model IDs and per-1k-char
pricing in `configs/model_registry.yaml` are placeholders marked unverified;
see `PHASE3_REPORT.md` ("Known limitations"). Set `ELEVENLABS_API_KEY` to
exercise `studio tts audition` / `T-EL-06` for real.



- **Jobs & CAS.** Every stage run is enqueued as a job with a `dedupe_key`
  (`run:stage:subject`). Identical inputs (same model, prompt, seed, refs, nonce)
  hit a canonical cache key and cost **0** on a cache hit — the asset is served
  from the content-addressed store.
- **Budgets.** Before any provider call, `begin_call` reserves an estimated cost
  atomically (`BEGIN IMMEDIATE`). Exceeding a cap refuses the call **before** the
  provider is invoked, persists the kill switch, and halts the run as
  `halted_budget`. `studio resume <run_id>` re-enables the run after you raise the
  cap / reset the switch — re-runs reuse cache entries and pay nothing.
- **Crash recovery.** Providers are job-based (`submit`/`poll`/`fetch`). Crash
  points are armed in tests (`core/faults.py`); recovery resets stale `running`
  jobs and orphaned `running` stage_runs, and a never-resubmit-on-resume policy
  keeps the ledger consistent. The crash matrix (T-REC-01) proves zero re-paid
  calls for every crash point, with one documented exception:
  `after_submit_before_persist` leaves a provider-side orphan that is logged via a
  `potential_orphan` audit event.
- **Gates.** G1_SCRIPT (script + metadata text), G0_CHARACTERS (sheets),
  G2_KEYFRAMES (recorded on the S9 animatic) and G3_FINAL_CUT (recorded on the
  S12 render). A pending gate pauses the whole run (`awaiting_approval`);
  `gates approve` enqueues the next `advance_run`. `gates reject --shots 3,7`
  creates a new attempt for **only** those shots and re-runs the affected
  stages. `final` never auto-approves. No paid stage may sit below a
  `G3_FINAL_CUT` ancestor (T-GATE-02), which is why the metadata approval is a
  `G1_SCRIPT` round.

## Media pipeline (Phase 4)

Paid media is strictly sequenced behind free, fail-closed preflights and human
gates:

```
s4p_media_preflight (free) → s5a/s5 (TTS) → s6_timing → s7/s7b (audio)
  → s8a_character_sheets (SERIES; gate G0_CHARACTERS — only if something was generated)
  → s8_keyframes (SHOT; gate G2_KEYFRAMES) → s10p_clip_preflight (free, after G2)
  → s10_clips (SHOT; raw paid clip is stored first, then conform is derived locally)
```

* **Preflights fail closed.** s4p lists every problem at once (unconfirmed
  models, missing `FAL_KEY`, unsupported aspect/duration, ref capability,
  never-verified price in final profile, over-budget) before any paid call.
  s10p fails closed if a locked `clip_duration_ms` is not in the video model's
  `supported_durations_s` (message: re-run S6).
* **Crash resume repays nothing.** Attempt nonces are persisted before the
  first submit; the cache key resumes the in-flight call instead of paying twice.
* **Visual QC is ffmpeg/ffprobe only** (decode, min bytes, aspect, blackdetect,
  freezedetect, first-frame luma/SSIM, audio presence). The SSIM check records
  *luma* SSIM against the keyframe at clip size. No visual-LLM validation anywhere.
* **Conform is derived, free and deterministic** — fps-normalize, trim/pad to
  exactly `round(clip_duration_ms * timing.fps / 1000)` frames, strip audio,
  SAR 1, libx264/yuv420p; already-compliant raws are referenced without re-encode.
* **Gates.** G0 and G2 snapshot asset shas + prompts; approval flips rows and
  lets the pipeline advance; rejection regenerates only the affected subjects.
* **Fail-fast (no circuit-breaker knob).** Any shot that exhausts QC retries
  fails the stage and the run; a single non-retryable QC failure raises
  `MediaCircuitOpen` and writes a `media_circuit_open` audit row. The Phase 4
  `media.circuit_breaker_consecutive_failures` key was parsed but read by no
  code, so Phase 5 removed it (H5.1). A settings file that still carries it
  logs a `DeprecationWarning`, writes a `config_deprecated_key_ignored` audit
  row and shows one `INFO` row in `studio doctor` ("ignored: media stage is
  fail-fast").
* **Sanitizer** rewrites policy-refused scene text (level 1: light rewrite,
  level 2: deterministic lexicon + stronger rewrite), never resubmits an
  identical prompt, and parks the shot for a human after `sanitizer.max_levels`.

Everything is gated by `model_registry.yaml`: fal entries ship as
`<CONFIRM_FAL_ENDPOINT_ID>` placeholders with `needs_confirmation: true` and
unverified pricing, so the final profile refuses to run until an operator
confirms them (see `studio doctor`). The sandbox/draft profiles auto-approve
and run on mock providers.

```bash
studio doctor                       # FAL_KEY, registry confirmation, media QC, ffmpeg filters
python scripts/check_ffmpeg.py      # CI capability gate (audio + visual filter sets)
studio run --demo --profile sandbox # mock media, no money
studio media preview <run-id>       # local HTML preview of sheets/keyframes/clips
studio media qc <attempt-id|sha|path>   # re-run visual QC on a stored asset
studio media audition image --model-keys image.ref.draft --cap-usd 0.50 --yes   # opt-in, real fal calls
studio media smoke --cap-usd 3.00 --yes      # opt-in live smoke (§Q), never in CI
```

Opt-in live tests: `STUDIO_LIVE_FAL=1 python -m pytest tests/test_phase4_fal.py -k live -q`
(one image under $0.25, one shortest clip under $1.50). Nothing live runs in CI.

## Render, export, bible and retention (Phase 5)

```
s8_keyframes → s9_animatic (SHOT; free render; gate G2_KEYFRAMES)
  → s10p_clip_preflight → s10_clips (SHOT)
  → s11_assemble (LANGUAGE; free concat + audio mux) → s12_render_qc (LANGUAGE; free; gate G3_FINAL_CUT)
  → s13_export (LANGUAGE; free)   s14_bible_proposals (EPISODE; paid, parked at G1)
```

* **The animatic is a real render.** S9 concatenates the approved keyframes at
  the locked S6 timing (`clip_frames`, `hold_frames`) into a silent H.264 MP4 so
  G2 reviews motion, not stills. S9/S11/S12/S13 never call a provider: they read
  CAS and run ffmpeg only.
* **S11 assembles, S12 checks.** The MP4 is exactly Σ `clip_frames` frames with
  the mixed audio; S12 re-probes it (frame count, duration, A/V sync, black and
  freeze runs, loudness) and records the checklist that G3 reviews.
  `studio render rebuild <run-id>` re-renders from CAS for free.
* **S13 writes a directory, never a database blob.** `export/<series>/<episode>/<lang>/`
  holds the MP4, `metadata.json` (the sidecar: hashes, timing, licences, flags)
  and `thumbnail_candidates/frame_*.png`. Re-renders create `_r2` and keep the
  old directory. A final-profile run needs G1, G0, G2 and G3 approved against
  the current snapshots; draft/sandbox runs write under `export.draft_root` and
  record the blockers as `export_flags`. Nothing is published by exporting.
* **S14 proposes, the operator decides.** One paid LLM call turns the finished
  episode into evidence-cited bible facts; the proposal is stored `pending` and
  is **never** auto-applied. `accept` refuses a stale base and bumps the bible
  version with a row; `reject` leaves the bible untouched. S14 is parked at G1
  so it cannot spend before the script is approved, and it is refused until G3
  clears.
* **S15 makes publishing a fact.** `publish mark` records the URL and timestamp
  (it needs a prior export) and closes the episode to paid stages
  (`EpisodeClosed`); free stages such as a re-export still run. `purge plan` is
  free and dry by default; `purge run` deletes only published-episode assets in
  the configured windows (raw clips, narration audio, rejected attempts,
  intermediates) and never touches anything protected: bible, permanent
  retention rows, gate-approved keyframes and anything a second, unpublished
  episode still references. A purged asset can never cause a silent re-pay: a
  later cache hit raises `AssetPurged` unless the operator passes
  `--allow-repay-purged` (audited). `purge gc` removes CAS orphans, scratch and
  stale `.tmp-*` export directories.

```bash
studio run --demo --profile sandbox --languages ar   # mock providers, real ffmpeg, ends in S13
studio gates list --run <run-id>                     # G1 → G0 → G2 → G3, in that order
studio export <run-id>                               # free: re-writes the export directory
studio render rebuild <run-id>                       # free: re-render from CAS
studio bible propose <episode-id>                    # prints the worst-case cost first; needs --yes
studio bible accept <proposal-id> --note "why"       # bumps the bible version
studio publish mark <deliverable-id> --url <url> --yes
studio purge plan --episode <episode-id>             # deletes nothing
studio purge run --episode <episode-id>              # dry run unless --yes
studio purge gc                                     # dry run unless --yes
studio doctor                                        # + export root, presets/channels, retention windows
```

`studio doctor` additionally checks that the export root is writable (and the
disk has room), that every channel key has a matching preset, and that the
retention windows are ordered (raw clips ≥ narration audio ≥ intermediates).
Every one of the above commands prints what it will do and refuses to write or
spend without `--yes`.

## Adding a provider (Phase 2+)

1. Add the model to `configs/model_registry.yaml` with real pricing
   (`pricing_verified_on` set) and the correct `capability`.
2. Implement the provider contract from `src/studio/providers/base.py`
   (LLM: single call; TTS/image/video: job-based `submit`/`poll`/`fetch`).
   No provider SDK import may appear outside `src/studio/providers/`.
3. Register it in `providers/factory.py`.
4. Reference it from a profile in `model_registry.yaml`.
5. Add tests: pricing estimator + a mock-backed gateway test.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff, mypy, an ffmpeg
capability check (`scripts/check_ffmpeg.py`: ffmpeg ≥ 5.0 and the full filter
list incl. `loudnorm ebur128 astats sidechaincompress`), and the test suite
with the 91% coverage gate plus `scripts/check_coverage_floors.py` (per-module
floors from `coverage_floors.json`) on **Ubuntu and Windows**, Python 3.11 and 3.12,
via `pytest -m "not slow"`. ffmpeg comes from
`FedericoCarboni/setup-ffmpeg` (pinned) on both OSes; ffmpeg-marked tests
auto-skip if the binaries are unavailable.

## License / status

Phase 4 complete on this branch: text pipeline (Phase 2) + ElevenLabs TTS
adapter + local audio engine (S5a–S7b, Phase 3) + fal.ai media pipeline
(S8a/S8/S10, visual QC, G0/G2) with placeholders for the real fal endpoints.
The demo path runs on mock providers; real-provider calls are opt-in via
environment flags and a confirmed registry. ElevenLabs
model IDs and pricing are placeholders marked unverified. See
`PHASE3_REPORT.md` for test results, coverage, deviations, decisions and
known limitations (and `PHASE1_REPORT.md`, `PHASE2_REPORT.md` for earlier
phases).
