# AI Studio — Phase 1 (Foundation)

A local, single-operator pipeline that will orchestrate paid generative-AI APIs to
produce cinematic animated videos. **Phase 1 ships the safety infrastructure only:
no real provider is ever called by this codebase today.** The demo pipeline runs
entirely on mock providers, and it is what the test suite and the demo commands
below exercise.

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
  cli.py            Typer CLI: init, doctor, run, worker, resume, db, gates, budget
  config/           Loader with ${STUDIO_HOME} interpolation and validation
  core/             Gateway, budget ledger, CAS, canonical JSON/cache keys,
                    pricing, retry, rate limiter, ids, clock, fault injection
  db/               SQLite connection (WAL, FK on, busy_timeout=5000), migrations, repos
  demo/             Demo seed + demo stages (LLM → TTS → keyframes → clips → finalize)
  domain/           Money (integer micro-USD), enums, errors, refs (SubjectRef lineage)
  pipeline/         DAG, engine (invalidation/gates/budget halt), worker, recovery
  providers/        Base provider contracts + mocks (LLM, TTS, image, video)
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
.venv/bin/studio gates list                 # shows the G1_SCRIPT gate
.venv/bin/studio gates approve <G1_ID>
.venv/bin/studio worker --until-idle        # ...until the G2_KEYFRAMES gate
.venv/bin/studio gates approve <G2_ID>
.venv/bin/studio worker --until-idle        # ...until the G3_FINAL_CUT gate
.venv/bin/studio gates approve <G3_ID>
.venv/bin/studio worker --until-idle        # run reaches status=completed
```

The final manifest asset is stored under `data/cas/<sha[:2]>/<sha[2:4]>/<sha>.json`
(forward-slash relative path in the `assets` table).

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
`studio.pipeline` (currently 93% total; CI enforces `--cov-fail-under=90`).

## How it works (30-second tour)

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
- **Gates.** G1_SCRIPT, G2_KEYFRAMES, G3_FINAL_CUT. A pending gate pauses the
  whole run (`awaiting_approval`); `gates approve` enqueues the next `advance_run`.
  `gates reject --shot <id>` creates a new attempt for **only** that shot and
  re-runs the affected stages. `final` never auto-approves.

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

GitHub Actions (`.github/workflows/ci.yml`) runs ruff, mypy, and the test suite
with the 90% coverage gate on **Ubuntu and Windows**, Python 3.11 and 3.12.
`ffmpeg` is installed on the runners (or via choco on Windows); ffmpeg-marked
tests auto-skip if it is unavailable.

## License / status

Phase 1 foundation. No real provider calls are implemented; the registry ships
placeholders. See `PHASE1_REPORT.md` for test results, coverage, deviations,
decisions and known limitations.
