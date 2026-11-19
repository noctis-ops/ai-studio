# Phase 3 Pre-Flight Audit & Verification (Step 0)

Date: 2026-11-17 · Branch `arena/bddd030b-ai-studio` · base `952c74c` · Linux, Python 3.11.2.

## 1. Structure verification

All Phase 1–3 components are present and wired:

| Component | Evidence |
|---|---|
| Foundation | `src/studio/core/` (budget, cache_key, canonical, cas, clock, faults, gateway, ids, pricing, ratelimit, retry), `src/studio/db/` (connection, migrate, 14 repos), `src/studio/domain/` (enums, errors, models, money, refs, series_bible), `src/studio/config/` (channels, languages, loader, prompts, registry, settings, vocab). |
| Text pipeline S1–S4 | `src/studio/pipeline/stages_text.py`, `pipeline/bible_pack.py`, `configs/prompts/director_prompt.txt`, `production.py` DAG. |
| ElevenLabs TTS | `src/studio/providers/tts/elevenlabs.py` (raw `httpx.AsyncClient` + injectable `MockTransport`), `src/studio/language/{arabic,tashkeel}.py`. |
| Local audio engine S5a–S7b | `src/studio/pipeline/stages_audio.py` (S5a/S5/S6/S7/S7b), `src/studio/audio/` (14 modules), `src/studio/qc/audio.py`, `src/studio/video/{ffmpeg,probe}.py`. |
| Migrations 001–004 | `src/studio/db/migrations/001_init.sql`, `002_stage_marks.sql`, `003_series_bible.sql`, `004_audio.sql`; `studio init` applies all four. |
| Reports / prompts | `PHASE1_REPORT.md`, `PHASE2_*`, `PHASE3_REPORT.md`, `PHASE3_IMPLEMENTATION_PROMPT.md`, `PHASE4_IMPLEMENTATION_PROMPT.md` all present. |

`PHASE3_REPORT.md` arithmetic is self-consistent: 412 collected = 410 passed + 1 skipped + 1 deselected; 4995 − 351 = 4644 = 92.97%.

## 2. Environment provisioning (no repository dependency added)

The clone arrived with a bare Python and no FFmpeg. To reproduce the Phase 3 contract I installed, **inside the gitignored `.venv` only**:

- `pip install -e '.[dev]'` (the repository's declared dev dependencies).
- `pip install -e '.[dev,providers]'`'s two SDK pieces needed by `mypy --strict`: `anthropic`, `openai` (these are already declared in `[project.optional-dependencies].providers` and CI installs them). Without them mypy reports 2 `import-not-found` errors; with them mypy is clean.
- `ffmpeg` **9.0.2** (from the `imageio-ffmpeg` wheel — the exact version used by the Phase 3 validation) and a **real `ffprobe` 6.0** (from the `ffmpeg-binaries` wheel), symlinked into `.venv/bin`. `scripts/check_ffmpeg.py` reports `OK: ffmpeg 9.0.2; 20 required filters present`. This replaces Phase 3's D11/R6 `ffprobe` stand-in with a real JSON-emitting ffprobe.
- `COLUMNS=200` for pytest/CI runs: with `rich` 15 / `typer` 0.29 the default 80-column console wraps `does\nnot exist` in `tests/test_mocks_cli.py::test_cli_03_library_scan_voice_set_run_preview_doctor`. Setting the terminal width (as a real terminal does) keeps the assertion intact; **no test was weakened**.
- `STUDIO_LIVE_FAL`, `STUDIO_LIVE_TTS`, `STUDIO_LIVE_LLM` are unset; no API keys are set. Zero spend.

## 3. Hidden Phase 3 defect found and fixed

**`src/studio/video/probe.py` forced `ffprobe -f <file extension>`.** For still images this is invalid: `png` is a codec, not a demuxer, so real ffprobe exits 1 with `Unknown input format: png`. Phase 3's stand-in ffprobe ignored `-f`, so every image/keyframe CAS write appeared green. With a real ffprobe, 30 Phase 1–3 tests failed (engine, gateway, crash matrix, ElevenLabs, audio mix) because `ContentStore.put_file()` calls `probe()` for `kind in {audio,video,image}`.

Fix (this commit):
- `probe()` no longer passes `-f`; ffprobe auto-detects the container (verified for PNG, WAV, MP3, FLAC, MP4 and the minimal MP3 fixture).
- Still-image classification now uses `nb_frames == 1` **or** a still-image extension **or** an image-pipe demuxer, because ffprobe 6/9 report `nb_frames: null` for `png_pipe`. Previously only `nb_frames == "1"` was checked.
- Regression tests added: `tests/test_probe_media.py` (2 tests, `@pytest.mark.ffmpeg`) generate a PNG, WAV and MP4 with lavfi and assert auto-detection, dimensions, channels, duration and fps.

No other Phase 1–3 source file was changed. `--cov` still reports 4995 statements / 351 missed / **92.97%**.

## 4. Second environment finding (documented, no code change)

With `ffmpeg` **6.0** (the `ffmpeg-binaries` static build) the S7b premaster mix **hangs**: `-stream_loop -1` inputs combined with `atrim` in `-filter_complex_script` produce byte-correct WAV outputs but the process never exits, so `run_ffmpeg(timeout_s=600)` stalls the suite. `ffmpeg` 9.0.2 (Phase 3's validated version, using `-/filter_complex`) terminates correctly. This is a toolchain-version issue in Phase 3's `audio/mixer.py` design, **not** modified here because §B/§O of the Phase 4 prompt put audio-engine changes out of scope; the validated 9.0.2 binary is provisioned instead. CI must keep using an ffmpeg build that terminates looped inputs (Phase 4 H3 capability check records the required filter set). Reported as an unverified environment caveat in the Phase 4 report.

## 5. Verification output (this environment)

```
$ ruff check src tests
All checks passed!
$ mypy --strict src
Success: no issues found in 94 source files
$ pytest tests/ -q -m "not slow" --cov=studio.core --cov=studio.db --cov=studio.pipeline \
    --cov=studio.audio --cov=studio.providers.tts --cov-fail-under=90
TOTAL 4995 statements, 351 missed -> 92.97%
412 passed, 1 skipped (T-EL-06 live), 1 deselected (T-MIX-05 slow) in 220.90s
$ python scripts/check_ffmpeg.py
OK: ffmpeg 9.0.2; 20 required filters present
```

Baseline was 410 passed + 2 new probe regression tests = 412 passed. Zero regressions. Phase 3 is verified clean and ready for Phase 4.
