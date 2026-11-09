# Phase 1 Implementation Prompt: Foundation Layer

**Usage:** Give the coding LLM everything under the line "BEGIN PROMPT" together with Blueprint v1.0 §5–7, §17 and §18 as reference attachments. This prompt overrides the blueprint wherever they conflict.

## Notes before you hand this over

**Blueprint corrections found while writing Phase 1.** They are baked into the prompt as "Amendments" (§C):

| # | Problem in Blueprint v1.0 | Fix |
|---|---|---|
| 1 | Provider interfaces were single-call (`image_to_video`), so the Gateway could not persist `remote_request_id` before waiting. | Job-based providers expose `submit/poll/fetch`. |
| 2 | `UNIQUE(scope, scope_ref)` on `budgets` allows duplicate "global" rows, because SQLite treats NULLs as distinct. | Use the sentinel `'global'`. |
| 3 | Money stored as `REAL` drifts. | Store integer micro-USD. |
| 4 | The submit-to-persist crash window was undetectable. | Add `provider_calls.submit_started_at` and a `potential_orphan` audit event. |
| 5 | Nothing prevented duplicate job enqueues. | Add `jobs.dedupe_key` with a partial unique index. |
| 6 | No place to record language and aspect intent. | Add `episodes.languages_json` and `episodes.aspects_json`. |

**Your decisions that need a guard:**
- **Arabic-only toggle:** implemented as `languages: [ar]` in config plus a per-episode override. No stage may hardcode a language, and a lint test enforces this. Visuals are paid once per track regardless of language count, and a test proves it.
- **Model IDs:** I can't verify any of them.
  - `claude-3-5-sonnet-latest` may be deprecated, so confirm it before Phase 2.
  - To my knowledge `fal-ai/flux/dev` is text-to-image, not reference-guided, so it won't deliver character consistency. It is a Phase 4 decision, and the registry flags it.
  - `fal-ai/kling` and `fal-ai/minimax` are incomplete endpoint paths.
- **Zero prices:** the registry ships with prices of `0.0` for real models. A zero price makes the budget guard meaningless, so the prompt blocks Draft/Final runs on any non-mock model whose price is zero and unverified. You must enter real prices before the first paid call.
- **ElevenLabs Creator plan:** I recorded `commercial_use_verified: true` as an operator attestation, because I can't verify plan terms. Re-check it against your current plan.
- **Flux dev:** it is marked `commercial_use_verified: false`, so Final export is blocked until you verify its license and flip the flag.
- **No real provider in Phase 1:** the first real spend happens in Phase 2 (LLM) and Phase 4 (media).

---

# ═══════════ BEGIN PROMPT ═══════════

## A. Role and Mission

You are a senior Python engineer implementing **Phase 1 (Foundation)** of the "AI Content Studio", a local, single-operator pipeline that orchestrates paid generative-AI APIs to produce cinematic animated videos.

Phase 1 delivers the infrastructure that makes later phases safe: configuration, SQLite persistence, a content-addressed asset store, a budget-enforcing `ProviderGateway`, mock providers, and a resumable pipeline engine with human-approval gates. It is proven by a demo pipeline and a strict test suite. **No real AI provider is called in this phase.**

**Priorities, in order:** (1) never pay twice for the same result, (2) never exceed a budget cap, (3) any crash is recoverable, (4) clean, typed, documented code.

## B. Scope

**In scope**
- Project scaffolding, config loaders, and model registry with validation
- Domain types, errors, and money helpers
- SQLite connection, migrations, and repositories
- CAS (content-addressed storage), canonical cache keys, retry, rate limiter, and pricing estimator
- Budget and ledger services with a kill switch
- `ProviderGateway` with a crash-safe lifecycle
- Mock providers (LLM, TTS, image, video) with fault and crash injection
- Pipeline engine (DAG, invalidation, gates), worker, and recovery
- Demo pipeline and the CLI (`init`, `doctor`, `db`, `run`, `worker`, `resume`, `gates`)
- CI workflow and full test suite

**Out of scope (do not create these files or stubs)**
Real provider adapters (Anthropic, OpenAI, ElevenLabs, fal), real pipeline stages S1–S15, audio engine, assembler and NVENC logic, QC modules beyond zero-byte checks, Streamlit UI, and publishing.

## C. Amendments to Blueprint v1.0

These override the attached blueprint.

1. **Job-based provider protocol.** Image and video providers implement `submit → poll → fetch`. LLM and TTS providers are single-call. The Gateway adapts both. See §H.
2. **Money** is stored as `INTEGER` micro-USD (1 USD = 1,000,000). Rename these columns:
   - `cache_entries.cost_usd` → `cost_musd`
   - `provider_calls.est_cost_usd` → `est_cost_musd`
   - `provider_calls.actual_cost_usd` → `actual_cost_musd`
   - `budgets.cap_usd` → `cap_musd`
   - `cost_ledger.amount_usd` → `amount_musd`

   No floats appear in any money arithmetic. YAML and CLI accept USD decimals and convert at the boundary.
3. **`budgets`:** `scope_ref TEXT NOT NULL`, with the sentinel `'global'` for the global scope. Episode scope_ref is the episode id. Month scope_ref is `'YYYY-MM'` (UTC).
4. **`provider_calls`:** add `submit_started_at TEXT`.
5. **`jobs`:** add `dedupe_key TEXT` and
   `CREATE UNIQUE INDEX ux_jobs_dedupe ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued','running');`
6. **`episodes`:** add `languages_json TEXT NOT NULL` and `aspects_json TEXT NOT NULL`, validated against config.
7. Create tables in dependency order (referenced tables first). Add `CHECK(json_valid(col))` on every `*_json` column.
8. `budgets.yaml` is dropped. Budget defaults live in `settings.yaml`.
9. Keep `approval_gates` unconstrained by subject uniqueness. Multiple rows per (run, gate, subject) are *rounds*, and the latest row is the active one.

## D. Engineering Rules (non-negotiable)

1. Python ≥ 3.11, full type hints, `mypy --strict` clean, `ruff` clean, Pydantic v2 models for all config and domain payloads.
2. Pipeline stages never import provider SDKs, `httpx`, or `subprocess`. External effects go only through `ProviderGateway` (paid calls) or `video/ffmpeg.py` (local media).
3. No hardcoded model IDs, prices, paths, or language codes outside `config/`, `domain/enums.py`, `language/`, and `tests/`. A lint test enforces the language rule (T-LANG-01).
4. **Windows/Linux parity.**
   - Use `pathlib` everywhere and explicit `encoding="utf-8"`.
   - Store relative paths in the DB with forward slashes (`PurePath.as_posix()`).
   - Write atomically via a temp file in the same directory, then `os.replace`.
   - No symlinks.
   - Keep ASCII slugs and shallow paths (CAS sharding is two levels).
   - Never assume POSIX signals or `fork`.
5. All timestamps come from an injectable `Clock` (UTC-aware, serialized as `YYYY-MM-DDTHH:MM:SS.mmmZ`). All IDs come from an injectable ULID generator. No bare `datetime.now()`, `uuid`, or `random` in `src/` outside those helpers.
6. Docstrings explain *why*, not *what*. No magic numbers, so every tunable lives in `settings.yaml`.
7. Secrets: read only from environment variables (via `.env`). Never log or persist keys. `request_json` in `provider_calls` must be passed through `redact()`.
8. SQLite: stdlib `sqlite3` only (sync). Async code calls repositories via `asyncio.to_thread`. Each transaction must begin and end inside one function on one thread. Require SQLite ≥ 3.35 (checked in `doctor` and at `db migrate`).
9. Repositories are Protocol-backed interfaces with SQLite implementations, and queries stay portable SQL, so a Postgres migration is possible later. The only SQLite-specific query allowed is the job-claim `UPDATE … RETURNING`.
10. Async exception hygiene:
    - Never write `except BaseException`.
    - No `finally` block may modify DB state.
    - `SimulatedCrash` (a `BaseException` used in tests) must propagate and leave the job `running`, exactly like a killed process.
11. No TODOs, `pass` bodies, or `NotImplementedError` in in-scope modules, except in `providers/factory.py` for real providers (see §H).

## E. Project Initialization

**Linux/macOS**
```bash
mkdir ai-studio && cd ai-studio && git init
python3.11 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -e ".[dev]"
studio init && studio doctor && pytest -q
```
**Windows (PowerShell)**
```powershell
mkdir ai-studio; cd ai-studio; git init
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
python -m pip install -U pip; pip install -e ".[dev]"
studio init; studio doctor; pytest -q
```

**`pyproject.toml`**
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ai-studio"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.6", "pyyaml>=6.0", "python-dotenv>=1.0",
  "typer>=0.12", "httpx>=0.27", "python-ulid>=2.2", "rich>=13.7",
]
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "pytest-cov>=5", "ruff>=0.5",
       "mypy>=1.10", "types-PyYAML"]
providers = ["anthropic", "openai", "elevenlabs", "fal-client"]   # Phase 2+, do not import in Phase 1
ui = ["streamlit>=1.35"]                                          # Phase 6

[project.scripts]
studio = "studio.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["src/studio"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["ffmpeg: requires ffmpeg/ffprobe on PATH (auto-skipped with a clear message if absent)"]

[tool.mypy]
strict = true
plugins = ["pydantic.mypy"]
python_version = "3.11"

[tool.ruff]
line-length = 100
target-version = "py311"
[tool.ruff.lint]
select = ["E","F","I","B","UP","ASYNC","S","RUF"]
ignore = ["S101"]
```

**`.env.example`**
```
STUDIO_HOME=.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
ELEVENLABS_API_KEY=
FAL_KEY=
```
**`.gitignore`:** `.venv/ data/ .env __pycache__/ .mypy_cache/ .ruff_cache/ .pytest_cache/ library/**/*.wav library/**/*.mp3 library/**/*.flac`

**`studio init`** is idempotent and does the following:
- creates `data/{cas,projects,exports}` and `library/{music,sfx,ambience}`
- creates `.env` from `.env.example` only if missing
- never overwrites existing `configs/*.yaml`
- runs migrations
- prints a summary

**`.github/workflows/ci.yml`:** matrix `ubuntu-latest` and `windows-latest`, Python 3.11 and 3.12. Install ffmpeg (`apt-get install -y ffmpeg` on Linux, `choco install ffmpeg -y` on Windows). Then run `ruff check`, `mypy src`, and `pytest --cov=studio --cov-report=term-missing`. The coverage gate is ≥ 90% for `studio.core`, `studio.db` and `studio.pipeline`.

## F. Files to Create

```
ai-studio/
├─ pyproject.toml  .gitignore  .env.example  README.md
├─ .github/workflows/ci.yml
├─ configs/  settings.yaml  model_registry.yaml  vocab.yaml  channel_profiles.yaml
├─ src/studio/
│  ├─ __init__.py                 # __version__
│  ├─ cli.py  logging_setup.py
│  ├─ config/    settings.py registry.py vocab.py channels.py languages.py loader.py
│  ├─ domain/    enums.py errors.py money.py refs.py models.py
│  ├─ db/        connection.py migrate.py
│  │   ├─ migrations/001_init.sql
│  │   └─ repos/ base.py assets.py providers.py budgets.py jobs.py pipeline.py gates.py content.py audit.py
│  ├─ core/      ids.py clock.py canonical.py cache_key.py cas.py retry.py ratelimit.py
│  │              pricing.py budget.py faults.py gateway.py
│  ├─ providers/ base.py factory.py
│  │   └─ mock/  llm.py tts.py image.py video.py
│  ├─ video/     ffmpeg.py probe.py             # minimal: runner + ffprobe only
│  └─ pipeline/  stage.py subjects.py dag.py context.py engine.py worker.py recovery.py gates.py
│      └─ demo/  seed.py stages.py
└─ tests/        conftest.py  unit/…  integration/…  fixtures/
```

## G. Default Configuration Files (create verbatim)

**`configs/settings.yaml`**
```yaml
paths: {home: "${STUDIO_HOME}", db: data/studio.db, cas: data/cas, exports: data/exports, projects: data/projects}

# ---- Language / aspect switches (CRITICAL) ----
# Defaults for new episodes; can be overridden per episode. Stages must iterate these, never hardcode.
languages: ["ar"]            # allowed values: ar, en. Launch config is Arabic-only.
aspects: ["9:16"]            # allowed values: 9:16, 16:9. First benchmark is a single-aspect Short.
default_profile: draft       # sandbox | draft | final

worker: {max_concurrency: 1, per_provider_concurrency: 1, heartbeat_s: 20, stale_after_s: 120,
         idle_poll_s: 0.5, job_retry_backoff_s: 30}
retry: {transient_max: 5, backoff_base_s: 2, backoff_cap_s: 60}
gates: {allow_auto_approve_in: [sandbox, draft]}
budgets:
  default_episode_cap_usd: 10.0      # benchmark Short cap
  monthly_cap_usd: 150.0
  global_cap_usd: null
  warn_pct: 80
pricing: {llm_tokens_per_char_worst_case: 0.5}   # conservative for Arabic when estimating input tokens
mock: {tts_chars_per_second: 15, video_width_16x9: 480, video_height_16x9: 270, fps: 24,
       poll_ticks: 2}
retention: {keep_raw_clips_days: 30, keep_narration_audio_days: 30}
```

**`configs/channel_profiles.yaml`**
```yaml
channels:
  yt_ar_wide:     {language: ar, aspect: "16:9", platform: youtube}
  yt_en_wide:     {language: en, aspect: "16:9", platform: youtube}
  short_ar_tall:  {language: ar, aspect: "9:16", platform: shorts_tiktok}
  short_en_tall:  {language: en, aspect: "9:16", platform: shorts_tiktok}
```

**`configs/model_registry.yaml`**
```yaml
registry_version: 1
models:
  llm.creative:                       # direction, shot planning, bible updates
    provider: anthropic
    capability: llm
    model_id: "claude-3-5-sonnet-latest"      # UNVERIFIED alias, confirm before Phase 2
    pricing: {unit: per_1m_tokens, in_usd: 0.0, out_usd: 0.0}
    pricing_verified_on: null
    commercial_use_verified: true
    needs_confirmation: true
  llm.light:                          # tashkeel, metadata, safety filtering
    provider: openai
    capability: llm
    model_id: "gpt-4o-mini"
    pricing: {unit: per_1m_tokens, in_usd: 0.0, out_usd: 0.0}
    pricing_verified_on: null
    commercial_use_verified: true
    needs_confirmation: true
  tts.main:
    provider: elevenlabs
    capability: tts
    model_id: "<CONFIRM_CURRENT_ID>"          # must support Arabic (Fusha) + English
    pricing: {unit: per_1k_chars, usd: 0.0}
    pricing_verified_on: null
    commercial_use_verified: true
    commercial_use_note: "Operator attestation: ElevenLabs Creator plan, commercial license, no attribution. Re-verify."
    needs_confirmation: true
  image.keyframe:
    provider: fal
    capability: image_reference_generate
    endpoint_id: "fal-ai/flux/dev"            # NOTE: believed text-to-image only; need a reference-capable endpoint for consistency (Phase 4)
    pricing: {unit: per_image, usd: 0.0}
    pricing_verified_on: null
    commercial_use_verified: false
    param_map: {prompt: prompt, refs: image_urls, aspect: aspect_ratio, seed: seed}
    needs_confirmation: true
  video.i2v.draft:
    provider: fal
    capability: image_to_video
    endpoint_id: "fal-ai/minimax"             # incomplete path, placeholder
    supported_aspects: ["16:9", "9:16"]
    supported_durations_s: [5, 10]
    max_resolution: "720p"
    pricing: {unit: per_second, usd: 0.0}
    pricing_verified_on: null
    commercial_use_verified: false
    param_map: {}
    needs_confirmation: true
  video.i2v.final:
    provider: fal
    capability: image_to_video
    endpoint_id: "fal-ai/kling"               # incomplete path, placeholder
    supported_aspects: ["16:9", "9:16"]
    supported_durations_s: [5, 10]
    max_resolution: "1080p"
    pricing: {unit: per_second, usd: 0.0}
    pricing_verified_on: null
    commercial_use_verified: false
    param_map: {}
    needs_confirmation: true
  mock.llm:   {provider: mock, capability: llm,   model_id: mock-llm,   simulated: true,
               pricing: {unit: per_1m_tokens, in_usd: 0.0, out_usd: 0.0}, commercial_use_verified: true}
  mock.tts:   {provider: mock, capability: tts,   model_id: mock-tts,   simulated: true,
               pricing: {unit: per_1k_chars, usd: 0.0}, commercial_use_verified: true}
  mock.image: {provider: mock, capability: image_reference_generate, endpoint_id: mock-image, simulated: true,
               pricing: {unit: per_image, usd: 0.0}, commercial_use_verified: true}
  mock.video: {provider: mock, capability: image_to_video, endpoint_id: mock-video, simulated: true,
               supported_aspects: ["16:9","9:16"], supported_durations_s: [5, 10], max_resolution: "720p",
               pricing: {unit: per_second, usd: 0.0}, commercial_use_verified: true}
profiles:
  sandbox: {llm_creative: mock.llm, llm_light: mock.llm, tts: mock.tts, keyframe: mock.image, video: mock.video}
  draft:   {llm_creative: llm.light, llm_light: llm.light, tts: tts.main, keyframe: image.keyframe, video: video.i2v.draft}
  final:   {llm_creative: llm.creative, llm_light: llm.light, tts: tts.main, keyframe: image.keyframe, video: video.i2v.final}
```

**`configs/vocab.yaml`:** minimal controlled vocabularies (`moods`, `shot_types`, `camera_moves`, `intensity: [1..5]`) with 6–8 sensible entries each. They are loaded and validated in Phase 1 and used from Phase 2.

## H. Module Specifications

### H1. `config/`
- `settings.py`: Pydantic `Settings` (`extra="forbid"`). `${ENV}` interpolation happens at load time, and an unset variable raises `ConfigError`. `languages` must be a non-empty, de-duplicated subset of `{ar,en}` and `aspects` a non-empty subset of `{9:16,16:9}`.
- `languages.py`: `resolve_languages(settings, episode_override: list[str] | None) -> list[Language]` is the **only** way stages obtain languages. Order is preserved.
- `channels.py`: `channel_for(language, aspect) -> str`. Raise `ConfigError` if no channel matches.
- `registry.py`: Pydantic discriminated models for pricing (`per_1m_tokens | per_1k_chars | per_image | per_second`). The loader enforces:
  - profile roles reference existing keys with the correct capability
  - `placeholder` is derived (any string containing `<CONFIRM`)
  - `resolve_profile(name) -> ResolvedProfile` raises `RegistryError` for any non-`simulated` model with an unresolved placeholder
  - for `draft` and `final`, `resolve_profile` also raises `RegistryError` for any non-simulated model whose price is zero, unless it carries `pricing_acknowledged_free: true`. Reason: zero prices silently disable budget protection.
  - `registry_snapshot(resolved) -> dict` is deterministic (sorted, includes `registry_version`) and is stored in `pipeline_runs.config_snapshot_json`.
  - `assert_exportable(resolved)` raises `CommercialUseNotVerified` if any model used has `commercial_use_verified: false` and the profile is `final`. It accepts an `override: bool` that writes an `audit_events` row.
- `loader.py`: `load_all(home: Path) -> AppConfig` (settings, registry, vocab, channels). Fail fast with the file name and field path in errors.

### H2. `domain/`
- `enums.py`: `Language(ar,en)`, `Aspect`, `Profile`, `Scope(series,episode,track,deliverable,shot,line)`, `GateKind(G0_CHARACTERS,G1_SCRIPT,G2_KEYFRAMES,G3_FINAL_CUT)`, `RunStatus`, `StageStatus`, `CallStatus`, `ErrorClass`. Values match the CHECK constraints in the SQL.
- `errors.py`:
```python
class StudioError(Exception): ...
class ConfigError(StudioError): ...
class RegistryError(ConfigError): ...
class CommercialUseNotVerified(StudioError): ...
class ProviderError(StudioError):
    error_class: ErrorClass
class TransientProviderError(ProviderError): ...      # 429/5xx/timeout
class PolicyRefusalError(ProviderError): ...          # content-policy refusal
class PermanentProviderError(ProviderError): ...
class ZeroByteAssetError(PermanentProviderError): ...
class BudgetExceeded(StudioError):                    # carries scope, scope_ref, cap_musd, held_musd, requested_musd
class KillSwitchTripped(BudgetExceeded): ...
class GatePolicyError(StudioError): ...
class ProviderNotImplementedError(StudioError): ...
class SimulatedCrash(BaseException): ...              # test-only; defined in core/faults.py
```
- `money.py`: `usd_to_musd(Decimal|str|float) -> int` (ROUND_HALF_UP), `musd_to_usd(int) -> Decimal`, and a `fmt_usd(int)` helper.
- `refs.py`:
  - `AssetRef(sha256, rel_path, kind, mime, size_bytes, duration_ms|None, width|None, height|None)`
  - `SubjectRef(scope, id, lineage: tuple[tuple[Scope,str],...])`, hashable and ordered. `lineage` lists ancestors root-first.
- `models.py`: Pydantic row models for the repositories (only what Phase 1 needs).

### H3. `db/`
- `connection.py`: `Database(path)`.
  - Thread-local connections.
  - `isolation_level=None` (explicit transaction control), `row_factory=sqlite3.Row`.
  - Pragmas on **every** new connection: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`.
  - `tx(immediate: bool=False)` is a context manager that issues `BEGIN`/`BEGIN IMMEDIATE`, then commits or rolls back.
  - `async def arun(fn, *a, **kw)` wraps `asyncio.to_thread`.
- `migrate.py`: applies numbered `.sql` files in order inside a transaction, records `schema_migrations`, and is idempotent. Check `sqlite3.sqlite_version_info >= (3,35,0)`.
- `migrations/001_init.sql`: Blueprint §17 plus Amendments C1–C9.
- Repositories (each a Protocol plus a SQLite class), implementing **only** what the engine, gateway, demo and tests need:
  - `assets`: `upsert`, `get`, `mark_purged`, `exists_live`
  - `providers`: `cache_get/put`, `find_inflight(cache_key)`, `calls` CRUD, `ledger` append
  - `budgets`: `ensure`, `get`, `held_musd(scope, ref)`, `trip`, `reset`
  - `jobs`: `enqueue(dedupe_key=…)`, `claim(worker_id)`, `heartbeat`, `complete`, `fail`, `requeue_stale`
  - `pipeline`: runs and stage_runs CRUD
  - `gates`: create, decide, list pending, rounds
  - `content`: series, characters (narrator), episodes, tracks, deliverables, shots, shot_attempts
  - `audit`: `append(actor, entity, event, payload)`
- The job claim query is the one from Blueprint §5, extended with `attempts = attempts + 1`.

### H4. `core/`
- `canonical.py`: `canonical_json(obj) -> bytes`. Rules:
  - sorted keys, compact separators, `ensure_ascii=False`, UTF-8
  - **every string NFC-normalized**
  - Arabic diacritics are never stripped (different tashkeel must produce a different key)
  - reject NaN/Inf
  - sets and tuples are normalized to sorted lists or lists
  - `Path` is rejected (callers must pass `sha256` of content)
- `cache_key.py`: `make_cache_key(capability, model_key, model_ref, params, input_asset_sha256s, prompt_version_sha, seed, nonce) -> str` (sha256 hex over canonical JSON of a dict with these fields). `input_asset_sha256s` is sorted. Absent optionals are encoded as `null`, never omitted.
- `cas.py`: class `ContentStore(root, db, clock)`:
  - `put_file(src: Path, *, kind, ext, retention="intermediate", move=True) -> AssetRef`: streams the file to compute sha256, rejects size 0 with `ZeroByteAssetError`, writes to `<root>/ab/cd/<sha>.<ext>` via a temp file in the destination directory then `os.replace`, and probes media metadata via `probe.py` for audio/video/image kinds. If the destination already exists, discard the temp file and keep the existing one. Inserts the `assets` row with `ON CONFLICT DO NOTHING`.
  - `put_bytes(...)`, `resolve(sha) -> Path` (raises if purged or missing), `exists_live(sha) -> bool`, `mark_purged(sha)`, `verify(sha)`.
  - File first, then the DB row. Orphan files are tolerated, and orphan rows are not.
- `retry.py`: `async def with_retry(op, *, classify, max_attempts, base_s, cap_s, sleep, rng)`. Full-jitter exponential backoff. Only `TransientProviderError` is retried. `sleep` and `rng` are injected so tests run instantly.
- `ratelimit.py`: `ProviderLimiter(max_concurrency, min_interval_s, clock, sleep)`, an async context manager. One instance per provider name.
- `pricing.py`: `estimate_cost_musd(entry, req) -> int` (worst case):
  - **llm:** `ceil(len(prompt_chars) × tokens_per_char_worst)` input tokens plus `max_tokens` output tokens
  - **tts:** chars
  - **image:** `n_images`
  - **video:** `duration_s` × per_second
  - `actual_cost_musd(entry, result) -> int` uses provider-reported usage when present, else the estimate.
- `faults.py`: `FaultInjector` with a no-op default.
  - `crash_at(point: str)` raises `SimulatedCrash` if armed.
  - `arm(point, *, once=True)`.
  - Defined crash points: `after_reserve`, `before_submit`, `after_submit_before_persist`, `after_persist_id`, `during_poll`, `after_fetch_before_store`, `after_store_before_complete`.
  - Production code calls `faults.crash_at("…")` at those exact points, and the overhead is a dict lookup.
- `budget.py`: `LedgerService(db, clock, settings)`. Each method is **one** transaction:
  - `begin_call(call_row, scopes) -> None`:
    1. `BEGIN IMMEDIATE`.
    2. Ensure budget rows exist (defaults from settings).
    3. If any applicable scope has `kill_switch_tripped`, raise `KillSwitchTripped`.
    4. For each scope compute `held = Σreserve − Σrelease + Σsettle` from `cost_ledger`. If `held + est > cap`, set `kill_switch_tripped=1` and `tripped_at` on that scope, append an audit event, commit that trip, then raise `BudgetExceeded`. The trip must persist even though the call is refused.
    5. Otherwise insert the `provider_calls` row (`reserved`) and a `reserve` ledger row.
    6. If the post-reserve held crosses `warn_pct` for the first time, append a `budget_warning` audit event once per scope.
  - `complete_call(call_id, actual_musd, cache_rows) -> None`: update the call to `completed`, add `release(est)` and `settle(actual)` ledger rows, and insert `cache_entries`, all in one transaction.
  - `fail_call(call_id, error_class, message, *, settle_musd=0)`: mark the call `failed`, add `release(est)`, and add `settle` only if `settle_musd > 0`.
  - `reset_kill_switch(scope, ref, new_cap_musd=None)` writes an audit event.
  - Scopes for a call: episode (`episode_id`, if any), month (`YYYY-MM` from the clock), and global.
  - Calls without an `episode_id` (such as series setup) are checked against the month and global scopes only.

### H5. `providers/`
`base.py` (Protocols and request/response models):
```python
class MediaPayload(BaseModel):       # what a provider hands back; Gateway downloads/copies it
    url: str | None = None
    local_path: Path | None = None
    ext: str
    mime: str

class JobHandle(BaseModel):  remote_request_id: str; meta: dict[str, Any] = {}
class JobState(BaseModel):   state: Literal["queued","running","succeeded","failed"]; error_class: ErrorClass | None = None; message: str | None = None

class LLMProvider(Protocol):    async def complete(self, req: LLMRequest) -> LLMResult: ...
class TTSProvider(Protocol):    async def synthesize(self, req: TTSRequest) -> TTSResult: ...   # MediaPayload + billed_chars
class JobProvider(Protocol):
    async def submit(self, req: MediaRequest) -> JobHandle: ...
    async def poll(self, handle: JobHandle) -> JobState: ...
    async def fetch(self, handle: JobHandle) -> list[MediaPayload]: ...
```
`LLMRequest(system, prompt, schema_name|None, max_tokens, temperature, prompt_version_sha)`, `LLMResult(text, parsed_json|None, tokens_in, tokens_out)`, `TTSRequest(text, voice_id, language, settings, previous_text, next_text)`, and `MediaRequest(capability, params, input_assets: list[AssetRef], aspect, duration_s, seed)`.

`factory.py`: `build_provider(entry) -> object`. `provider == "mock"` returns a mock. Any other provider raises `ProviderNotImplementedError("<provider> adapter is implemented in Phase 2 (LLM/TTS) or Phase 4 (fal)")`.

**Mock providers** (`providers/mock/`):
- Use FFmpeg `lavfi` through `video/ffmpeg.py`, with `-bitexact -fflags +bitexact -map_metadata -1`.
- **TTS:** a sine tone whose duration is `chars / mock.tts_chars_per_second` (min 0.5 s), written to WAV.
- **Image:** a single-frame PNG at the aspect's mock resolution, with the seed encoded in the color.
- **Video:** an MP4 (`testsrc2` or `color`) at the mock resolution (16:9 is 480×270, 9:16 is 270×480), `fps` from settings, and exactly the requested `duration_s`.
- **LLM:** deterministic JSON or text derived from `sha256(prompt)`. When `schema_name == "demo_shots"` it returns a valid shot list (N shots) so the demo pipeline works.
- Mock job providers: `submit` returns `mock-<ulid>` and records it in a **persistent JSON ledger file** (in the test's tmp dir, so it survives a simulated process restart) with `submit_count`. `poll` returns `running` for `mock.poll_ticks` calls, then `succeeded`. `fetch` renders the file.
- **Fault plan** (`MockFaultPlan`, injected via constructor): `fail_transient_n`, `fail_policy`, `fail_permanent`, `zero_byte`, `latency_s`, and `fail_on_nth_call`. Every mock exposes `calls` and `submit_count` for assertions.
- Mocks bill according to the registry pricing in effect. Defaults are `0.0`, and budget tests override the pricing with non-zero simulated values.

### H6. `core/gateway.py`: the heart of the system
```python
@dataclass(frozen=True)
class CallContext:
    episode_id: str | None
    stage_run_id: str | None
    nonce: str = "0"                 # regeneration changes it; a crash-resume must reuse it
    purpose: str = ""

@dataclass(frozen=True)
class GatewayResult:
    assets: list[AssetRef]; inline_json: dict | None
    cost_musd: int; cache_hit: bool; provider_call_id: str | None

class ProviderGateway:
    async def llm  (self, model_key: str, req: LLMRequest,   ctx: CallContext) -> GatewayResult
    async def tts  (self, model_key: str, req: TTSRequest,   ctx: CallContext) -> GatewayResult
    async def image(self, model_key: str, req: MediaRequest, ctx: CallContext) -> GatewayResult
    async def video(self, model_key: str, req: MediaRequest, ctx: CallContext) -> GatewayResult
```
**Lifecycle (implement exactly):**

1. Compute `cache_key`. **Cache hit** means a `cache_entries` row exists **and** every referenced asset is live (the file exists and `purged_at IS NULL`). A hit returns cost `0`, performs no provider call, and writes no ledger rows. A hit is allowed even when a kill switch is tripped.
2. **Adopt in-flight work.** Look up `provider_calls` with this `cache_key` in status `reserved` or `submitted` (`find_inflight`).
   - `submitted` with `remote_request_id`: skip to step 6 (resume polling). **Never resubmit and never reserve again.**
   - `reserved` with `submit_started_at IS NULL`: reuse the reservation and go to step 4.
   - `reserved` with `submit_started_at` set but no remote id: reuse the reservation, append an `audit_events` row `potential_orphan` (payload includes the call id and provider), and go to step 4.
3. Otherwise `LedgerService.begin_call` (atomic budget check + reserve), then `faults.crash_at("after_reserve")`. `BudgetExceeded` and `KillSwitchTripped` propagate and the provider is never invoked.
4. Acquire the provider limiter. For job providers, persist `submit_started_at` (a single statement) and `crash_at("before_submit")`. Then `submit` (retried on `TransientProviderError` with backoff, within the same call row, bumping `attempt`). `crash_at("after_submit_before_persist")`. Persist `remote_request_id` and set status `submitted` in **one statement**. `crash_at("after_persist_id")`. (Single-call providers, LLM and TTS, skip persistence of a remote id and run `complete()` or `synthesize()` directly under retry.)
5. Not used (reserved).
6. Poll until `succeeded` or `failed` (each iteration `crash_at("during_poll")` on the first iteration only). Polling uses the injected sleep and a configurable interval. Transient poll errors are retried. A `failed` state maps to the typed error by `error_class`.
7. `fetch` → `crash_at("after_fetch_before_store")` → download **immediately** (HTTP via `httpx` streaming to a temp file; `local_path` is copied) → `ContentStore.put_file` (zero bytes raise `ZeroByteAssetError`) → `crash_at("after_store_before_complete")` → `LedgerService.complete_call` (atomic).
8. On a typed error:
   - `TransientProviderError` after exhaustion, `PolicyRefusalError` and `PermanentProviderError` call `fail_call` (which releases the reservation).
   - **Exception:** `ZeroByteAssetError` after a paid job keeps the estimated cost settled (`settle_musd=est`), because the provider likely billed. The call is marked `failed` and nothing is cached.
   - Then re-raise.
9. `request_json` is `redact(normalized request)`. Never store binaries. Store `input_assets` as sha256 lists.

Per-provider `ProviderLimiter` instances are created lazily from `registry.limits`.

### H7. `pipeline/`: engine, worker, gates, recovery

**`stage.py`**
```python
class StageHandler(Protocol):
    def subjects(self, ctx: RunContext) -> list[SubjectRef]: ...       # enumerate from DB state
    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str: ...
    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput: ...   # MUST be idempotent

@dataclass(frozen=True)
class StageSpec:
    name: str; scope: Scope; depends_on: tuple[str, ...]
    handler: StageHandler; gate_after: GateKind | None = None
```
`RunContext` holds `db`, `repos`, `gateway`, `clock`, `ids`, `config`, `resolved_profile`, and `run_id`, `episode_id`, `languages`, `aspects`. Handlers obtain languages **only** from `ctx.languages`.

**`dag.py`:** `Dag(stages)` validates unique names, known dependencies, and no cycles (raises `ConfigError`), and exposes topological order.

**Engine rules (`engine.py`)**
1. **Stage-level barrier:** a stage becomes eligible only when every `depends_on` stage is *complete*, meaning all of its enumerated subjects are `succeeded` or `skipped`, and enumeration happened. On eligibility the engine enumerates `handler.subjects(ctx)` and creates missing `stage_runs` (`pending`).
2. **`advance_run` job:** computes eligible stages and enqueues one `run_stage` job per pending stage_run (`dedupe_key=f"{run_id}:{stage}:{subject_id}"`). It handles gates and run completion, and is idempotent.
3. **`run_stage` job:**
   - mark the stage_run `running`
   - store `input_hash`
   - call `handler.run`
   - on success set `succeeded` with `output_json`, then enqueue `advance_run` (dedupe-key it)
4. **Invalidation:** before executing, and on every `advance_run`, recompute `input_hash` for `succeeded` stage_runs. A mismatch marks that run `stale`, and all *related* downstream stage_runs become `stale` transitively. "Related" means one subject is an ancestor-or-equal of the other in `lineage`. Stale runs re-execute (they become `pending`). Unrelated subjects stay untouched. A re-execution with identical gateway inputs costs zero because of the CAS cache.
5. **Gates:** when a stage with `gate_after` is complete, create an `approval_gates` row (`pending`, `snapshot_hash` from the stage_runs' output hashes), set the run `awaiting_approval`, and enqueue **nothing**.
   - `GateService.approve(gate_id, note)` → mark approved and enqueue `advance_run`.
   - `GateService.request_changes(gate_id, note, rejections: list[ShotRejection(subject, edited_prompt|None)])`: for each rejection, create a new `shot_attempts` row (`attempt_no+1`, a fresh `nonce`, `prompt_used` = edited prompt if given), mark that subject's stage_run and its downstream `stale`, mark the gate `changes_requested`, enqueue `advance_run`. When the stage completes again, a **new gate round** is created.
   - Auto-approve: allowed only when the profile is in `gates.allow_auto_approve_in` **and** the run was created with `auto_approve=True`. Otherwise raise `GatePolicyError`. In `final` it is always refused.
6. **Failure handling inside the worker job runner:**

   | Exception | Result |
   |---|---|
   | `BudgetExceeded` / `KillSwitchTripped` | run → `halted_budget`, stage_run back to `pending`, job `succeeded`, no retry |
   | `TransientProviderError` (after gateway retries) | job failed with `run_after = now + job_retry_backoff_s`, requeued until `max_attempts`, then stage_run `failed` and run `failed` |
   | `PolicyRefusalError` / `PermanentProviderError` / other `StudioError` | stage_run `failed`, run `failed` |
   | `SimulatedCrash` | not caught, so state is left exactly as a killed process would leave it |

7. **Resume:** `studio resume <run_id>` clears `halted_budget` and `failed` (after the operator fixed the cause), sets failed stage_runs to `pending`, and enqueues `advance_run`.

**`worker.py`:**
- `Worker(db, engine, config, worker_id)`.
- Loop: recover on start, claim a job, run it as an asyncio task with a heartbeat task (every `heartbeat_s`), execute, and mark the job `succeeded` or `failed`.
- Concurrency is `worker.max_concurrency` (default 1, enforced by a semaphore).
- `run(until_idle: bool=False, max_jobs: int|None=None)`.
- The idle poll interval comes from settings.

**`recovery.py`:**
- `recover_stale_jobs(db, now, stale_after_s)` resets `running` jobs with `heartbeat_at < now − stale_after_s` back to `queued` (attempts already incremented at claim). Jobs whose attempts reach `max_attempts` become `failed`.
- `recover_stage_runs` resets `running` stage_runs that have no `running` job back to `pending`.
- Provider calls are **not** touched here, because the Gateway adopts them lazily (H6 step 2).
- Append an audit event for every recovery action.

### H8. Demo pipeline (`pipeline/demo/`), used by tests and `studio run --demo`

`seed.py`: creates a demo series, a narrator character (`role=narrator`) with mock voices per enabled language, one standalone episode (using `resolve_languages` and config aspects, and storing `languages_json` and `aspects_json`), one track per aspect, and one deliverable per (track × language) via `channel_for`.

**Stages** (all handlers idempotent, all external calls through the Gateway):

| Stage | Scope | Depends on | Does | Gate after |
|---|---|---|---|---|
| `demo_script` | episode | none | `llm` (mock, `schema_name="demo_shots"`) → creates N shots for **each track** | G1_SCRIPT |
| `demo_voice` | deliverable | `demo_script` | `tts` per shot for that deliverable's language, writing `narration_lines` | none |
| `demo_keyframes` | shot | `demo_script` | `image` per shot, using the current `shot_attempts` row's nonce | G2_KEYFRAMES |
| `demo_clips` | shot | `demo_keyframes`, `demo_voice` | `video` (duration quantized up to the nearest supported duration ≥ `max(narration ms across the track's deliverables)`) | none |
| `demo_finalize` | deliverable | `demo_clips` | writes a manifest JSON asset | G3_FINAL_CUT |

Key invariant proven by tests: **keyframe and clip calls scale with tracks, not with languages**, and **TTS calls scale with deliverables**.

### H9. `video/`
- `ffmpeg.py`: `async run_ffmpeg(args, *, timeout_s)` using `asyncio.create_subprocess_exec`, UTF-8, capturing stderr, and raising `PermanentProviderError`-free local error `MediaToolError` on non-zero exit. `find_binary("ffmpeg"|"ffprobe")`.
- `probe.py`: `probe(path) -> ProbeResult(kind, duration_ms, width, height, fps, has_audio)` via `ffprobe -print_format json`.

### H10. `cli.py` (Typer)
`studio init`; `studio doctor`; `studio db migrate`; `studio run --demo [--profile sandbox|draft|final] [--languages ar,en] [--aspects 9:16] [--auto-approve]`; `studio worker [--until-idle]`; `studio resume <run_id>`; `studio gates list|approve <id>|reject <id> --shot <subject_id> [--prompt "…"]`; `studio budget status|reset <scope> <ref> [--cap USD]`.

`studio doctor` checks and prints PASS/WARN/FAIL for:
- Python ≥ 3.11
- SQLite ≥ 3.35
- ffmpeg and ffprobe on PATH
- an informational NVENC probe (a 1-frame `h264_nvenc` test encode, WARN if unavailable, **never** FAIL)
- write access to `data/`
- API-key env vars (present or absent; never print values)
- registry warnings: placeholders, `pricing_verified_on` null or older than 30 days, zero prices, and `commercial_use_verified: false`

Exit code is non-zero only on FAIL.

## I. Test Specification

All tests run offline using the mock providers, with a temporary `STUDIO_HOME`, `FakeClock`, deterministic IDs, and an injected no-op `sleep`. Tests that need ffmpeg carry `@pytest.mark.ffmpeg`. **Every test below is mandatory.**

**Config and registry**
| ID | Assertion |
|---|---|
| T-CFG-01 | `${STUDIO_HOME}` interpolation works, an unset variable raises `ConfigError`, an unknown key is rejected. |
| T-CFG-02 | `languages`: `['ar']` and `['ar','en']` are valid; `[]`, `['fr']` and duplicates are rejected. Aspects likewise. |
| T-CFG-03 | A profile referencing a missing key or the wrong capability raises `RegistryError`. |
| T-CFG-04 | `resolve_profile("draft")` on the shipped registry raises (placeholders and zero prices). `sandbox` resolves. A modified registry with real prices and no placeholders resolves for `draft`. |
| T-CFG-05 | `assert_exportable` raises for `final` with an unverified model, passes with the override flag, and the override writes an audit row. |
| T-CFG-06 | `registry_snapshot` is deterministic and is persisted to `pipeline_runs.config_snapshot_json`. |
| T-LANG-01 | AST scan of `src/studio/**` finds no string constant equal to `"ar"` or `"en"` outside the allow-list (`config/`, `domain/enums.py`, `language/`). |
| T-LANG-02 | `channel_for` maps all four configured pairs and raises for unknown pairs. |

**Database**
| ID | Assertion |
|---|---|
| T-DB-01 | Migration applies to an empty DB and is idempotent. Every new connection reports WAL, FK on, and `busy_timeout=5000`. |
| T-DB-02 | FK violations raise. CHECK constraints reject: `assets.size_bytes=0`, invalid enums, and invalid JSON in `*_json`. |
| T-DB-03 | With 8 threads claiming from 50 queued jobs, each job is claimed exactly once. |
| T-DB-04 | The `dedupe_key` partial unique index blocks a second queued or running job and allows re-enqueue after completion. |
| T-DB-05 | A writer thread and a reader thread run concurrently in WAL without "database is locked" errors. |
| T-DB-06 | `budgets` rejects duplicate `('global','global')`. |

**Core**
| ID | Assertion |
|---|---|
| T-CK-01 | Cache key is stable under dict key reordering. It changes with `nonce`, `model_ref`, `seed`, `prompt_version_sha`, and input asset hashes. It is invariant to input-asset order. |
| T-CK-02 | NFC-composed and decomposed Latin strings give the same key. Arabic text with different diacritics gives **different** keys. NaN raises. |
| T-CAS-01 | `put_file` produces `ab/cd/<sha>.<ext>`, re-put is idempotent, the DB stores forward-slash relative paths, and a zero-byte file raises `ZeroByteAssetError`. |
| T-CAS-02 | A simulated crash between temp write and `os.replace` leaves no file at the final path. A purged asset makes `resolve` raise. |
| T-RETRY-01 | Backoff delays follow full-jitter bounds with a seeded rng. Max attempts are respected. Non-transient errors are never retried. |
| T-RL-01 | Limiter enforces `max_concurrency` and `min_interval_s` under a fake clock. |
| T-PRICE-01 | Estimators match hand-computed values for all four pricing units, including Arabic worst-case token estimation. |
| T-MONEY-01 | `usd_to_musd` rounds half-up and round-trips. No float arithmetic appears in the ledger paths (assert on integer types). |

**Budget and ledger**
| ID | Assertion |
|---|---|
| T-BUD-01 | reserve +E, complete → net held = actual. Fail → net 0. Held formula holds after random interleavings. |
| T-BUD-02 | A call that would exceed the cap is refused **before** the provider is invoked (`submit_count == 0`), the kill switch is persisted, and it survives closing and reopening the DB. |
| T-BUD-03 | While tripped, paid calls raise `KillSwitchTripped`, but cache hits still succeed. `reset_kill_switch` re-enables spending and writes an audit event. |
| T-BUD-04 | Episode cap trips while the monthly cap is fine, and vice versa. Month scope uses the clock's UTC month. |
| T-BUD-05 | `warn_pct` crossing emits exactly one `budget_warning` audit event per scope. |
| T-BUD-06 | Two threads reserving concurrently never oversubscribe a cap (`BEGIN IMMEDIATE`). |

**Gateway**
| ID | Assertion |
|---|---|
| T-GW-01 | Second identical call is a cache hit: cost 0, provider untouched, no new ledger rows. |
| T-GW-02 | Same inputs with a new `nonce` pay again, and the old asset is retained. |
| T-GW-03 | N transient failures then success: exactly one reservation, one settle, and one provider call row. Exhaustion leaves net-zero ledger and `failed` status. |
| T-GW-04 | Policy refusal raises `PolicyRefusalError`, releases the reservation, and does not retry. |
| T-GW-05 | A purged or missing asset turns a cache hit into a miss, and the call is paid again. |
| T-GW-06 | A paid job returning zero bytes raises `ZeroByteAssetError`, keeps the estimate settled, marks the call `failed`, and caches nothing. |
| T-GW-07 | LLM results are cached via `inline_json` and a hit performs no provider call. |
| T-GW-08 | `request_json` never contains a secret value (inject a fake key into the env and grep the DB). |

**Crash recovery matrix (T-REC-01, parametrized).** For each crash point in the table, for each of the image and video mock job providers: arm the point, run the call, catch `SimulatedCrash`, build a **new** Gateway, Ledger and Worker on the same DB and the same mock ledger file, repeat the identical call, and assert:

| Crash point | Mock `submit_count` after resume | Settled cost | Extra assertions |
|---|---|---|---|
| `after_reserve` | 1 | 1× | the reservation was reused (one `reserve` row) |
| `before_submit` | 1 | 1× | none |
| `after_submit_before_persist` | 2 (one orphan) | 1× | exactly one `potential_orphan` audit event |
| `after_persist_id` | 1 | 1× | no resubmit, resumed polling |
| `during_poll` | 1 | 1× | none |
| `after_fetch_before_store` | 1 | 1× | none |
| `after_store_before_complete` | 1 | 1× | CAS file exists once |

After every scenario: the final ledger has no dangling reservation, `held` equals one call's cost, and exactly one asset is cached.

**Engine, worker and gates**
| ID | Assertion |
|---|---|
| T-ENG-01 | DAG rejects cycles, unknown dependencies and duplicate names. Topological order is correct. |
| T-ENG-02 | With `max_concurrency=1`, no two `run_stage` jobs overlap (instrumented). |
| T-ENG-03 | Kill the worker mid-stage (`SimulatedCrash`), advance the clock past `stale_after_s`, and restart. The job requeues, the run completes, and total mock submits equal the number of distinct requests (zero re-paid). |
| T-ENG-04 | Invalidation: changing one shot's input marks only that shot's stage_runs (and related downstream) `stale`. Other tracks and deliverables are untouched. The re-run of unchanged gateway inputs costs 0. |
| T-ENG-05 | Gate lifecycle: reaching G1 sets the run `awaiting_approval` and enqueues no jobs. Approving enqueues `advance_run`. `request_changes` on shot k creates `shot_attempts` N+1 with a new nonce for **only** shot k, re-runs only that shot, and creates gate round 2. |
| T-ENG-06 | `final` profile cannot auto-approve (`GatePolicyError`). `sandbox` with `auto_approve=True` runs end-to-end without a human. |
| T-ENG-07 | Budget halt: the run goes to `halted_budget`, with no further jobs. After resetting the cap and kill switch, `resume` completes the run with **zero** repaid calls. |
| T-ENG-08 | **Language toggle:** with `['ar']` there is exactly 1 deliverable per track, zero `en` rows, and zero English TTS calls. With `['ar','en']` there are 2 deliverables per track, TTS calls double, and **image and video call counts are identical to the Arabic-only run**. Both runs use the same code path. |
| T-ENG-09 | Aspects: `['9:16']` gives 1 track. `['9:16','16:9']` gives 2 tracks with independent shots. Mock video output aspect equals the track aspect (ffprobe). |
| T-ENG-10 | Permanent errors fail the stage_run and run. Exhausted transient errors requeue with the configured backoff, then fail at `max_attempts`. |
| T-ENG-11 | Recovery resets stale `running` jobs and orphaned `running` stage_runs, and writes audit events. |

**Mock providers and CLI**
| ID | Assertion |
|---|---|
| T-MOCK-01 (ffmpeg) | Video duration equals the requested duration ±1 frame, the aspect matches, and TTS duration equals `chars/rate` (min 0.5 s). Outputs are non-empty and ffprobe-valid. |
| T-CLI-01 | `studio init` is idempotent. `doctor` exits non-zero when ffmpeg is hidden from PATH. |
| T-CLI-02 (ffmpeg) | End-to-end: `init` → `run --demo --profile sandbox` → `worker --until-idle` → run `awaiting_approval` at G1 → `gates approve` ×3 with `worker --until-idle` between each → run `completed`. The manifest asset exists. |

**Coverage:** ≥ 90% line coverage for `studio.core`, `studio.db` and `studio.pipeline`.

## J. Working Method and Deliverable Format

Implement in this order. After each step, run `ruff`, `mypy`, and the tests for that step, and fix failures before proceeding.

1. Scaffolding, `config/`, `domain/`, `core/ids.py`, `clock.py`, `canonical.py`, `cache_key.py`
2. `db/` (schema, migrations, repos)
3. `core/cas.py`, `retry.py`, `ratelimit.py`, `pricing.py`, `faults.py`, `video/`
4. `core/budget.py`
5. `providers/base.py`, `providers/mock/`, `providers/factory.py`
6. `core/gateway.py` and the crash matrix
7. `pipeline/` (dag, engine, gates, worker, recovery) and the demo
8. `cli.py`, CI workflow, README (setup, config switches, how to run the demo, how to add a provider)

**Output rules**
- Output **complete file contents**, one file per block, labeled with its path. Never use "…rest unchanged" placeholders.
- If you cannot execute code, say so explicitly, and list every test as "unverified" rather than claiming it passes.
- Do not add features outside §B. Where this prompt is ambiguous, choose the most conservative option (the one that spends less, or fails closed), and record it under "Decisions" in your report.

**Final report (required):** (1) the test results table by ID, (2) coverage numbers, (3) deviations from this prompt and why, (4) decisions you made on ambiguities, (5) known limitations, (6) the exact commands to run the demo on Windows and on Linux.

## K. Definition of Done

- Every test in §I passes on Ubuntu and Windows in CI, and coverage thresholds are met.
- `studio run --demo --profile sandbox --languages ar` completes end-to-end through the three gates with a manifest output. The same command with `--languages ar,en` completes and shows doubled TTS but unchanged image and video call counts.
- The crash matrix shows zero repaid calls, except for the documented `after_submit_before_persist` orphan, which is logged.
- `studio doctor` runs and prints actionable warnings for the placeholder model IDs, zero prices, and the unverified commercial-use flags.
- No import of any provider SDK exists anywhere in `src/`.

# ═══════════ END PROMPT ═══════════

---

## What to do with the result

1. Run the prompt in a coding LLM session, with Blueprint §5–7, §17 and §18 attached.
2. Run `pytest` and `studio doctor` yourself. Don't accept "all tests pass" without seeing the output.
3. Send me the **Phase 1 Report**, plus any failing tests or doctor warnings. I'll review it for gaps, especially the crash-recovery matrix and the language-toggle test, before generating the **Phase 2 prompt** (text pipeline: S1–S4, Arabic tashkeel validator, Series Bible context packer, and the real Anthropic/OpenAI adapters).
4. Before Phase 2 you need to supply:
   - a working LLM model ID for each role
   - real per-token prices entered in `model_registry.yaml` (otherwise `draft` runs are blocked by design)
