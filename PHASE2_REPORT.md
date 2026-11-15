# Phase 2 Report — Text Pipeline (S1–S4) with Real LLM Adapters

Branch `arena/c029d3df-ai-studio` @ Phase 1 base `47d7b8c`.
Date: 2026-11-15. Operator: AI Director pipeline (this build).

## 1. Test results by ID

Legend: ✅ passed · ⏭️ opt-in, not run in CI (as specified).
All offline; zero real provider spend; `STUDIO_LIVE_LLM` unset during the run.

| ID | File | Result |
|---|---|---|
| T-ADP-01 | tests/test_llm_adapters.py::test_anthropic_request_built_correctly | ✅ |
| T-ADP-02 | ::test_anthropic_usage_maps_to_hand_computed_musd (1200 in @ $3 + 350 out @ $15 = **8,850 µ$**) | ✅ |
| T-ADP-03 | ::test_anthropic_error_mapping[400,401,403,404,413,429,500,502,529] + ::test_anthropic_timeout_and_connection_are_transient | ✅ |
| T-ADP-04 | ::test_anthropic_empty_content_is_transient | ✅ |
| T-ADP-05 | ::test_openai_request_built_correctly, ::test_openai_expect_json_absent_sets_no_response_format (alias `gpt-4o-mini` verbatim; `response_format={"type":"json_object"}` iff `expect_json`) | ✅ |
| T-ADP-06 | ::test_openai_usage_maps_to_hand_computed_musd (1200 in @ $0.15 + 350 out @ $0.60 = **390 µ$**) | ✅ |
| T-ADP-07 | ::test_openai_error_mapping[…same matrix] + ::test_openai_timeout_and_connection_are_transient + ::test_openai_empty_content_is_transient + ::test_invalid_json_from_model_is_permanent | ✅ |
| T-ADP-08 | ::test_missing_anthropic_sdk_raises_config_error, ::test_missing_openai_sdk_raises_config_error (both carry the `ai-studio[providers]` install hint), ::test_unknown_provider_name_raises_config_error | ✅ |
| T-ADP-09 | ::test_request_json_never_contains_api_key[anthropic,openai] (gateway-routed call; `request_json` scrubbed) | ✅ |
| T-TASH-01…08 | tests/test_tashkeel.py (8 tests: F3 strip-equality, tatweel/0670/06D6–06ED stripping, first-mismatch index, NFC, empty inputs) | ✅ |
| T-PACK-01…06 | tests/test_bible_pack.py (determinism, truncation + floor, 0.5 tok/char accounting, fixed section order, PRIOR_WORK sort/omit, pack sha feeds S2 input_hash) | ✅ |
| T-S1-01…03 | tests/test_stages_text.py::test_s1_creates_bible_v1 / ::test_s1_identical_rerun_is_idempotent_and_free / ::test_s1_changed_concept_creates_v2_and_keeps_v1 | ✅ |
| T-S2-01 | ::test_s2_narration_lines_persisted_with_tashkeel | ✅ |
| T-S2-02 | ::test_s2_letters_altered_terminal_failure_and_audit (one retry → 2nd failure → stage failed + exactly one `tashkeel_mismatch` audit) | ✅ |
| T-S2-03 | ::test_s2_retry_succeeds_with_two_verify_calls (exactly 2 verify calls; `cache_key`s differ by attempt nonce) — see Decisions D3 | ✅ |
| T-S3-01 | ::test_s3_unknown_character_writes_nothing (continuity violation ⇒ PermanentProviderError; **zero** shots written; run failed) | ✅ |
| T-S3-02 | ::test_s3_shots_order_and_durations (+ pack sha feeds input hashes in all 4 stages) | ✅ |
| T-S4-01 | ::test_s4_metadata_written_per_deliverable (ar + en × 1 track; `safety=clear`) | ✅ |
| T-S4-02 | ::test_s4_flagged_content_parks_at_g3 (flagged ⇒ still parks at G3_FINAL_CUT) | ✅ |
| T-E2E-TEXT-01 | ::test_e2e_text_pipeline_parks_at_g1_then_completes (mocks; zero paid calls; zero paid ledger settles; G1 → G3 → completed) | ✅ |
| T-LIVE-01 | tests/test_live_smoke.py | ⏭️ opt-in only; **not collected** unless `STUDIO_LIVE_LLM=1` (conftest `collect_ignore`); `tests/test_live_env.py` asserts the flag is absent in the suite | ⏭️ |
| T-REGR-01 | §"Commands" below — 225/225 tests green (145 Phase 1 unchanged + 80 Phase 2); coverage gate 91.88%; ruff clean; mypy strict clean (73 files) | ✅ |
| T-REGR-02 | tests/test_regression_phase2.py (draft full-profile still refuses via shipped registry; sandbox resolves; subset resolves draft; T-CFG-04 untouched; BEGIN_PROMPT.md untouched) | ✅ |

Phase 2 test counts: 37 adapter + 8 tashkeel + 7 packer + 11 stage/e2e + 4 regression + 12 production/prompt/live-smoke guards + 1 live-env guard = **80 new tests**; **225 total** (Phase 1: 145).

Additional Phase 2 unit tests (not in the ID matrix): `tests/test_production.py` — seed validation, cost-projection arithmetic, `load_prompt` provenance, full `resolve_profile("draft")` refusal, language enum wiring.

## 2. Coverage

CI gate (same as Phase 1: `studio.core` + `studio.db` + `studio.pipeline`):

```
python -m pytest tests/ -q --cov=studio.core --cov=studio.db --cov=studio.pipeline \
    --cov-report=term --cov-fail-under=90
→ TOTAL 2772 statements, 225 missed → 91.88%  (gate ≥ 90% ✓)
```

Full package (`--cov=studio`, for information):

| Module | Cover | | Module | Cover |
|---|---|---|---|---|
| language/tashkeel.py | 100% | | cli.py | 54% |
| domain/series_bible.py | 100% | | production.py | 98% |
| db/repos/bible.py | 97% | | pipeline/bible_pack.py | 90% |
| providers/anthropic_llm.py | 90% | | pipeline/stages_text.py | 87% |
| providers/openai_llm.py | 84% | | pipeline/engine.py | 87% |
| providers/factory.py | 82% | | live_smoke.py | 64% |
| config/registry.py | 82% | | **FULL TOTAL** | **88%** |

Untested lines are concentrated in CLI presentation, live-network paths (never CI), and defensive early returns.

## 3. Deviations and why

- **D1. `narration_lines` rows are materialized by S3, not S2.**
  The table has `NOT NULL shot_id`; shots exist only after S3 (per track). S2 validates and carries the lines in its stage output; S3 persists them keyed by `(line.no == shot_no, language)`. No schema fork taken.
- **D2. S2–S4 consult the bible version pinned by the run's S1 output** (falling back to `latest()` only if S1 output is missing), not unconditionally `latest()`. Pack bytes and cache keys survive unrelated bible edits in other series; a real S1 re-run on a changed concept correctly invalidates downstream stages.
- **D3. T-S2-03 "exactly 2 provider calls"** was interpreted as exactly 2 `tashkeel_verify` calls (attempt nonces `"0"` / `"1"` ⇒ distinct gateway cache keys, so the retry genuinely re-calls). The full S2 stage makes 3 LLM calls total (S2a creative + 2 verify). The test asserts both facts explicitly.
- **D4. S4 `metadata_safety` parks the run at the existing `G3_FINAL_CUT` gate** rather than introducing a new gate kind.BEGIN_PROMPT §H's gate semantics are unchanged; metadata/safety are decision input for the final-cut approval, with `flagged` treated fail-closed (unrecognized safety ⇒ `flagged` + synthetic issue).
- **D5. Registry resolution for production (text-only) runs** uses the new `resolve_llm_subset()` (llm roles only). The full `resolve_profile()` is byte-for-byte unchanged: `draft` and `final` still raise `RegistryError` on media placeholders (T-CFG-04 semantics intact). The engine opts in via `Engine(..., llm_subset_only=True)`, set only on the `--production` path.
- **D6. Windows CI ffmpeg step**: `choco install ffmpeg --no-progress -y` replaced by `ffmpeg -version && ffprobe -version` on `windows-latest` (ffmpeg is preinstalled on the GitHub image; choco was redundant and a flake source). Ubuntu keeps `apt-get install ffmpeg`. Install step now uses `-e .[dev,providers]`.
- **D7. `studio run --production` defaults the profile to `draft`** and until Phase 4 (TTS/image/video verification) executes **S1–S4 only**, printing a media-stages-are-skipped notice and stopping at G1/G3. No model/price registry value was changed by Phase 2 (guarded by test).
- **D8. `studio run --production` requires `--allow-paid-llm`**; without it the command exits before any seed or provider call. Worst-case projected LLM cost (chars × 0.5 tok/char × registry rates, per stage, incl. one tashkeel retry) is printed **before** the run row is created.
- **D9. Series Bible carries provenance** (`created_by`, `created_at`) through the model, enabling `studio db bible` without raw SQL in the CLI. Phase 1 call sites are untouched (defaults keep `from_content`/`from_row` compatible).
- **D10. Migration 003 header-comment fix**: the `;` inside the m003 header comment broke the naive `;`-split in `migrate._statements()` ("near *" OperationalError). Comment reworded; `studio init` verified end-to-end (`migrations applied: [1, 2, 3]`). No semantics changed.
- **D11. Mock LLM scans only the `# SERIES_BIBLE` section** when picking a continuity-safe character for `shot_plan` (previously matched the Director prompt's own bullet lines → wrong character → permanent continuity failure on the happy path). The mock remains deterministic.
- **D12. Live smoke not wired into pytest in CI**: `tests/test_live_smoke.py` is excluded from collection unless `STUDIO_LIVE_LLM=1` (conftest); `tests/test_live_env.py` asserts the flag is absent. The operator runs the smoke explicitly (`scripts/live_llm_smoke.py`); `studio doctor --llm` is the in-CLI equivalent. Phase 1 "no real spend" rule is preserved.
- **D13. Phase 1 AST test (`no hardcoded language codes`) is satisfied** in `providers/mock/llm.py` by emitting `Language.AR.value`/`Language.EN.value` instead of literals — the Phase 1 test was not modified.

## 4. Decisions on ambiguities

- **A1. "draft until media verify" vs `draft` refusing** — T-CFG-04 is binding, so the production subset resolves LLM roles only and the full profile stays refused. `final` additionally requires `commercial_use_verified` on keyframe+video (still `false`); refused by design.
- **A2. Where crash recovery lives** — pre-existing `recovery` runs at worker start; a crashed `advance_run` job is requeued after `stale_after_s` (120 s) with identical `dedupe_key` semantics. No change to §H6/H7.
- **A3. Gates for the text DAG on the CLI** — `studio gates list/approve/reject` picks the text DAG when the run’s stages (or a pending gate’s stage) are text stages, else the demo DAG; `--production` forces text selection on `worker`/`resume`. `gates approve` prints the matching `--production` hint for text gates.
- **A4. S4 dependency** — implemented as `metadata_safety depends_on shot_plan`; the `s4_after_g1: true` setting is loaded, validated (`bool`), and recorded but DAG ordering is static (the gate is what actually sequences S4 after human approval).
- **A5. `studio db bible`** — runs migrations first (idempotent), reads via the repo, prints version + 12-char sha256 prefix + title + `created_by` + `created_at`.
- **A6. Cost projection** is deliberately *worst-case* (pack at the token budget cap, full verify retry) even though real runs cost far less; actuals are reconciled by the gateway from provider-reported usage and appear in `provider_calls.actual_cost_musd`.
- **A7. DoD "G1 → G2 → G3 demo unchanged"** — `studio run --demo --profile sandbox` verified on a fresh home: G1_SCRIPT → G2_KEYFRAMES → G3_FINAL_CUT approved, run `completed`, gates table semantics unchanged.

## 5. Known limitations

- Media stages (TTS/image/video) remain **unverified placeholders**; `--production` stops after S4 by design. ElevenLabs Arabic TTS `model_id` is still `<CONFIRM_CURRENT_ID>` (Phase 4, operator input).
- `cli.py` line coverage is 54% (presentation + argument plumbing); core logic it calls is tested. `live_smoke.py` 64% — network paths exercised only by the opt-in smoke.
- `engine.py`/`worker.py` remain 87% (Phase 1 appendix observation stands).
- `request_changes` on a *text* gate re-executes the stage through the shared GateService; there is no text-specific "shot rejection" (shots do not exist at G1). Use the note; semantics are "stage re-executes with the same input hash change policy as Phase 1".
- NVENC h264 probe remains WARN-only on machines without NVIDIA encoders (as in Phase 1).
- Windows ffmpeg presence is asserted, not installed; if GitHub’s `windows-latest` image ever drops it, the version-check step fails loudly (intended).
- `asyncio_default_fixture_loop_scope` deprecation warning from pytest-asyncio is cosmetic.
- Actual paid smoke (T-LIVE-01) has not been executed in this environment — no API keys are present here; it is operator-invoked by design.

## 6. Commands — sandbox end-to-end and opt-in live smoke (Linux and Windows)

Prereqs: `ffmpeg` + `ffprobe` on PATH; `git clone` + `pip install -e .[dev,providers]`.

Linux / macOS:
```bash
pip install -e .[dev,providers]
export STUDIO_HOME="$PWD"          # or any scratch dir
python -m pytest tests/ -q --cov=studio.core --cov=studio.db --cov=studio.pipeline --cov-fail-under=90
ruff check src tests && mypy
studio init
studio run --demo --profile sandbox
studio worker --until-idle
studio gates list
studio gates approve <gate-id>     # repeat worker/approve until run completes
studio doctor                      # LLM rows verified (see §7)
studio doctor --llm                # opt-in live 1-token ping per adapter
STUDIO_LIVE_LLM=1 python scripts/live_llm_smoke.py   # opt-in, paid, ~0 cost
```

Windows (PowerShell):
```powershell
pip install -e .[dev,providers]
$env:STUDIO_HOME = "$PWD"
python -m pytest tests\ -q --cov=studio.core --cov=studio.db --cov=studio.pipeline --cov-fail-under=90
ruff check src tests; mypy
studio init
studio run --demo --profile sandbox
studio worker --until-idle
studio gates approve <gate-id>
studio doctor
$env:STUDIO_LIVE_LLM = "1"; python scripts\live_llm_smoke.py   # opt-in, paid, ~0 cost
```

CI: `.github/workflows/ci.yml` — matrix ubuntu-latest/windows-latest × Python 3.11/3.12; installs `.[dev,providers]`; Windows ffmpeg step is a version check; `STUDIO_LIVE_LLM` is never set in CI.

Observed in this sandbox (2026-11-15): full suite 225/225 ✅ (fast ffmpeg 9.0.2 static build); demo run G1→G2→G3 `completed`; text production path exercised offline end-to-end (`run --production` with the LLM subset zero-priced in a scratch registry; gates G1/G3 approved; `safety_status=clear`; all `provider_calls.actual_cost_musd == 0`; `studio db bible` printed v1).

## 7. `studio doctor` (pasted, abridged to the two LLM rows; full table shown)

```
│ python >= 3.11                   │ PASS   │ 3.11.2                           │
│ sqlite >= 3.35                   │ PASS   │ 3.40.1                           │
│ ffmpeg on PATH                   │ PASS   │ /tmp/bin/ffmpeg                  │
│ ffprobe on PATH                  │ PASS   │ /tmp/bin/ffprobe                 │
│ Anthropic LLM                    │ PASS   │ verified 2026-11-09 ·            │
│ (claude-sonnet-4-5-20250929,     │        │ in=$3.0/MTok out=$15.0/MTok ·    │
│  verified 2026-11-09)            │        │ commercial use verified          │
│ OpenAI LLM (gpt-4o-mini,         │ PASS   │ verified 2026-11-09 ·            │
│  verified 2026-11-09)            │        │ in=$0.15/MTok out=$0.6/MTok ·    │
│                                  │        │ commercial use verified          │
│ NVENC h264 probe (informational) │ WARN   │ (no NVIDIA encoder on this host)│
│ write access to …/data           │ PASS   │ OK                               │
```

Registry entries (`configs/model_registry.yaml`, unchanged by Phase 2): `llm.creative` = `claude-sonnet-4-5-20250929`, $3.00/$15.00 per 1MTok, verified 2026-11-09; `llm.light` = `gpt-4o-mini`, $0.15/$0.60 per 1MTok, verified 2026-11-09. TTS/image/video remain placeholders (Phase 4).

## 8. Files (Phase 2)

New: `configs/prompts/director_prompt.txt`, `src/studio/config/prompts.py`, `src/studio/domain/series_bible.py`, `src/studio/db/migrations/003_series_bible.sql`, `src/studio/db/repos/bible.py`, `src/studio/language/tashkeel.py`, `src/studio/pipeline/bible_pack.py`, `src/studio/pipeline/stages_text.py`, `src/studio/providers/anthropic_llm.py`, `src/studio/providers/openai_llm.py`, `src/studio/production.py`, `src/studio/live_smoke.py`, `scripts/live_llm_smoke.py`, `tests/test_{llm_adapters,tashkeel,bible_pack,stages_text,production,regression_phase2,live_env,live_smoke}.py`, `PHASE2_REPORT.md`.

Modified: `.github/workflows/ci.yml`, `configs/settings.yaml` (`text_pipeline:` block), `pyproject.toml` (confusables allow-list), `src/studio/cli.py` (`run --production`, `doctor --llm` + LLM rows, `db bible`, text-DAG worker/gate routing), `src/studio/config/registry.py` (`resolve_llm_subset`), `src/studio/config/settings.py` (`TextPipelineSettings`), `src/studio/core/gateway.py`, `src/studio/db/repos/__init__.py`, `src/studio/db/repos/content.py` (`get_series`, `set_deliverable_metadata`), `src/studio/domain/models.py` (`metadata_json`, `safety_status`), `src/studio/pipeline/engine.py` (`llm_subset_only`), `src/studio/providers/factory.py` (lazy SDK registration), `src/studio/providers/base.py` (`expect_json`/`raw_ref`), `src/studio/providers/mock/llm.py` (text-pipeline schema branches), `tests/conftest.py` (T-LIVE collect_ignore). Phase 1 contracts untouched; `BEGIN_PROMPT.md` untouched.
