# Phase 2 Implementation Prompt: Text Pipeline — S1–S4 with Real LLM Adapters

**Usage:** Hand this file to a senior coding LLM together with the repository at
branch `arena/143b1398-ai-studio` (Phase 1 tip: `9533f8c`). Phase 1 contracts (§H gateway,
§H7 engine, integer micro-USD, kill switch, CAS, gates, crash-safe lifecycle) remain
**binding** and are referenced throughout.

## Notes before you hand this over

**Phase 1 audit outcome (2026-11-09):** accepted. Independently re-verified — 145 tests
pass with 93% coverage on `studio.core`/`studio.db`/`studio.pipeline`, ruff clean, mypy
strict clean (62 files), no provider SDK imports anywhere in `src/`, T-REC-01 crash matrix
meets §I, `studio run --demo --profile sandbox` completes G1→G2→G3 with a manifest asset,
and the `ar,en` run doubles TTS with unchanged image/video counts. **No architectural
gaps block Phase 2.** See "Non-blocking observations" below.

**Phase 2 preflight decisions (do not re-litigate):**

- **`llm.creative`** = Anthropic, model_id `claude-sonnet-4-5-20250929` (dated snapshot;
  the `claude-sonnet-4-5` alias also resolves). Verified 2026-11-09: 200K context,
  64K max output, **$3.00 / $15.00 per 1M input/output tokens**. Claude 3.7 Sonnet is
  no longer listed in Anthropic's current models table and must not be configured.
  (If the operator prefers newer headroom at equal-or-lower cost, `claude-sonnet-4-6`
  $3/$15 1M ctx, or `claude-sonnet-5` $2/$10 1M ctx are drop-in replacements — an
  operator decision recorded here, not taken unilaterally.)
  Sources: https://platform.claude.com/docs/en/models/overview (fetched 2026-11-09);
  corroborated by pricing trackers checked Sept–Oct 2026.
- **`llm.light`** = OpenAI, model_id `gpt-4o-mini` (alias → latest snapshot). Verified
  2026-11-09: 128K context, 16,384 max output, **$0.15 / $0.60 per 1M input/output**,
  cached input $0.075. Sources: https://platform.openai.com/docs/pricing (as checked by
  multiple trackers through 2026-07..2026-10). Pin `gpt-4o-mini-2024-07-18` only if the
  operator wants a frozen snapshot; one unverified tracker mentioned a January 2027
  retirement of that snapshot — not corroborated by OpenAI's pages; treat as rumor and
  re-check the pricing page before relying on it. Using the alias is the configured,
  recommended choice.
- **ElevenLabs** = Creator Plan confirmed by the operator 2026-11-09 (commercial license,
  no attribution required). `tts.main` keeps `commercial_use_verified: true`. The Arabic
  TTS model_id is still `<CONFIRM_CURRENT_ID>` — supplied by the operator before Phase 4.
- **TTS / image / video entries remain intentionally unverified and zero-priced.**
  `draft` still refuses to resolve (`RegistryError`) until those three are verified;
  this is a feature, not a bug (T-CFG-04). Phase 2 therefore exercises real LLM spend
  only through the gateway from S1–S4 and the opt-in live smoke test — never in CI.
- `configs/model_registry.yaml` is **already updated** to the values above (pricing,
  `pricing_verified_on: "2026-11-09"`, `needs_confirmation: false`, LLM entries only).
  Phase 2 code must not change those values.

---

# ═══════════ BEGIN PROMPT ═══════════

## A. Role and Mission

You are a senior Python engineer implementing **Phase 2** of the AI Content Studio: the
real LLM text pipeline. Phase 1 proved the safety infrastructure. Phase 2 attaches real
intelligence to it — the **AI Director** that writes the series bible, the Arabic
tashkeel-clean narration, the shot plan, and the per-deliverable metadata — every byte
through the existing crash-safe, budget-enforced `ProviderGateway`.

Priorities unchanged: (1) never pay twice, (2) never exceed a cap, (3) any crash is
recoverable, (4) typed, documented code.

## B. Scope

**In scope**
- Real LLM adapters: `src/studio/providers/anthropic_llm.py`, `src/studio/providers/openai_llm.py`
- The AI Director persona prompt: `configs/prompts/director_prompt.txt`
- Series Bible: migration `003_series_bible.sql`, repository, deterministic versioning
- Series Bible context packer: `src/studio/pipeline/bible_pack.py`
- Arabic Tashkeel strip-equality validator: `src/studio/language/tashkeel.py`
- Stages S1–S4 wired into the DAG as a separate spec set (`text_specs()`), replacing
  nothing — the Phase 1 demo remains untouched and mock-only
- `studio run --production --allow-paid-llm`: an operator-gated path for real LLM runs
  (explicit flag, `draft`+ validation, episode cap enforced, kill switch honored)
- Migration-added columns on `deliverables` for S4 output
- Full §I-style test matrix for everything above; Phase 1 suite stays green

**Out of scope (do not create)**
- Real TTS/image/video adapters and calls (Phase 4+); media stages
- Real Anthropic/OpenAI spend in CI (live calls are opt-in via `STUDIO_LIVE_LLM=1`)
- Audio engine, assembler, QC beyond zero-byte, UI, publishing, S5–S15
- Any change to Phase 1 invariants: integer micro-USD, `BEGIN IMMEDIATE`, gates
  semantics, dedupe-key uniqueness, CAS, crash-point handling

## C. Decisions locked for Phase 2

1. **`llm.creative` = Anthropic `claude-sonnet-4-5-20250929`**, **`llm.light` = OpenAI
   `gpt-4o-mini`** (see preflight notes for evidence). No other models without an
   operator decision.
2. **All real-LLM spend flows through the gateway** — cache key includes
   `prompt_version_sha` = sha256 of the packed context, so an unchanged prompt never
   re-pays. Adapters perform HTTP only inside `providers/`; the engine never sees a URL.
3. **Actual cost from API usage.** `actual_cost_musd` computes from the API's own
   `usage` (input/output tokens), never from the pre-estimate. Estimates use
   `pricing.llm_tokens_per_char_worst_case` (0.5 tok/char for Arabic) — keep it.
4. **Tashkeel is validated by strip-equality**, not by string comparison: the model may
   add harakat; it may not change the underlying letters/spaces/punctuation order.
5. **Series Bible is versioned and content-addressed** (sha256 of canonical JSON);
   the packer emits the same bytes for the same inputs on every run.
6. **G1_SCRIPT still pauses the run** after S1–S3. S4 runs after G1 approves
   (metadata describes the approved cut; no wasted spend on a rejected script).
7. **`sandbox` profile stays mock-only forever** — mocks remain the only free path.

## D. Engineering Rules (Phase 1 rules remain binding, plus)

- No provider SDK import outside `src/studio/providers/*_llm.py`; SDKs load lazily and
  raise `ConfigError("install ai-studio[providers]")` when missing.
- Never log request/response bodies containing API keys; secrets never persist to
  `request_json` (T-GW-08 remains green).
- Adapter temperature defaults: creative 0.7, light 0.2 (deterministic-ish tasks).
- `max_tokens` explicit on every call (Anthropic requires it; default 4096 creative,
  1024 light). Empty content blocks → `TransientProviderError` (retryable).
- HTTP timeouts 60s; `429`/`5xx`/`529`/timeout → `TransientProviderError`;
  `400`/`401`/`403`/`404`/`413` → `PermanentProviderError`; malformed JSON from model →
  `PermanentProviderError` (fail closed, surfaced in stage_run error).
- `pip install -e .[providers]` installs `anthropic>=0.39` and `openai>=1.40`.

## E. Files to create / modify

Create: `configs/prompts/director_prompt.txt`, `src/studio/language/__init__.py`,
`src/studio/language/tashkeel.py`, `src/studio/providers/anthropic_llm.py`,
`src/studio/providers/openai_llm.py`, `src/studio/pipeline/bible_pack.py`,
`src/studio/pipeline/stages_text.py` (S1–S4 specs + handlers),
`src/studio/db/migrations/003_series_bible.sql`, `src/studio/db/repos/bible.py`,
`src/studio/domain/series_bible.py` (SeriesBible model), `scripts/live_llm_smoke.py`,
`tests/test_llm_adapters.py`, `tests/test_tashkeel.py`, `tests/test_bible_pack.py`,
`tests/test_stages_text.py`, `PHASE2_REPORT.md`.

Modify: `src/studio/db/__init__.py` (wire migration 003), `src/studio/db/repos/__init__.py`,
`src/studio/providers/factory.py` (lazy adapter registration by provider name),
`src/studio/cli.py` (`studio run --production --allow-paid-llm`, `doctor` prints
LLM verified status + dates, `studio db bible`), `src/studio/demo/stages.py` is NOT
touched, `pyproject.toml` (`[project.optional-dependencies] providers = [...]` already
exists — bump pins), `.github/workflows/ci.yml` (install `.[dev,providers]`), `configs/settings.yaml`.

## F. Module specifications

### F1. `configs/prompts/director_prompt.txt`

Ship this file with a complete AI Director persona. Required content blocks (English
shell, Arabic domain rules):

1. **Identity**: "You are the AI Director of an Arabic-first cinematic short-form
   studio. You write Fusha Arabic with full, correct tashkeel for spoken narration."
2. **Tashkeel contract**: narration must be fully voweled; diacritics never alter the
   underlying letters; output must pass strip-equality validation (see F3) — if unsure
   of a diacritic, emit the most common pausal form, never a different root.
3. **Vision**: cinematic tone, 9:16-first composition, motivated lighting, continuity
   of characters/props across shots, visual storytelling over exposition.
4. **Output discipline**: respond ONLY with the JSON object requested by the current
   task; no prose, no markdown fences; all string values in the requested language.
5. **Bible hygiene**: stay consistent with the provided Series Bible; when the concept
   contradicts it, prefer the Bible and report the conflict in `continuity_notes`.
6. **Refusals**: refuse illegal/hateful content; output
   `{"error": "refused", "reason": "<short>"}`; the stage treats this as a terminal
   `PermanentProviderError`.

Loading: `studio.config.prompts.load_prompt(name)` → returns (text, sha256). The sha256
is fed into every S-stage `input_hash` and into the gateway's `prompt_version_sha`.

### F2. Series Bible — migration 003_series_bible.sql

```sql
CREATE TABLE IF NOT EXISTS series_bible (
  id            TEXT PRIMARY KEY,
  series_id     TEXT NOT NULL REFERENCES series(id),
  version       INTEGER NOT NULL,
  content_json  TEXT NOT NULL CHECK (json_valid(content_json)),
  content_sha256 TEXT NOT NULL,
  created_by    TEXT NOT NULL DEFAULT 's1',
  created_at    TEXT NOT NULL,
  UNIQUE (series_id, version)
);
ALTER TABLE deliverables ADD COLUMN metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json));
ALTER TABLE deliverables ADD COLUMN safety_status TEXT NOT NULL DEFAULT 'pending'
  CHECK (safety_status IN ('pending','clear','flagged'));
```

`domain/series_bible.py`: frozen `SeriesBible` model — `series_id`, `version`,
`title`, `premise`, `world`, `characters: list[CharacterBible(name, description,
visual_tokens, arc)]`, `style_tokens: list[str]`, `tone: list[str]`,
`continuity_notes: str`, `content_json` (canonical), `sha256`. Repository methods:
`create_version` (auto-increments, sets sha256 from canonical JSON), `latest(series_id)`,
`get(series_id, version)`, `list_versions(series_id)`.

### F3. Arabic Tashkeel strip-equality validator (`language/tashkeel.py`)

```python
TASHKEEL_RANGES = (
    ("\u0610", "\u061a"),  # sign markup
    ("\u064b", "\u065f"),  # fathatan..wavy hamza above (covers tanwin, harakat, shadda, sukun, quranic marks)
    ("\u0670", "\u0670"),  # superscript alef
    ("\u06d6", "\u06ed"),  # small high quranic annotation signs
    ("\u0640", "\u0640"),  # tatweel / kasheeda
)
TASHKEEL_SET = frozenset(chr(c) for lo, hi in TASHKEEL_RANGES for c in range(ord(lo), ord(hi) + 1))

def normalize_nfc(text: str) -> str: ...          # unicodedata.normalize("NFC", text)
def strip_tashkeel(text: str) -> str: ...         # NFC, remove TASHKEEL_SET, collapse no whitespace
@dataclass(frozen=True)
class TashkeelCheck:
    ok: bool
    first_mismatch: int | None   # index into the stripped comparison, None when ok
    stripped_source: str
    stripped_output: str

def check_tashkeel(source: str, output: str) -> TashkeelCheck: ...
```

Rules: **strip-equality only** — do NOT normalize letter variants (alef/hamza forms are
the model's responsibility and are graded, not rewritten), do NOT trim or collapse
whitespace, NFC both sides first. `check_tashkeel` reports the first index where the
stripped sequences diverge (or where one is a prefix of the other). S2 behaviour on
`ok=False`: one deterministic retry with the same input plus
`"Your previous answer changed the underlying letters; emit the identical consonantal
text with only tashkeel added."` appended to the system pack; second failure →
`StudioError`, audit event `tashkeel_mismatch`, stage_run failed. Never auto-fix.

### F4. Context packer (`pipeline/bible_pack.py`)

```python
@dataclass(frozen=True)
class ContextPack:
    text: str
    pack_sha256: str          # sha256 of .text, utf-8
    n_chars: int
    truncated_sections: tuple[str, ...]

def pack_context(*, bible: SeriesBible | None, episode: EpisodeSummary,
                 prior_deliverables: list[DeliverableSummary],
                 stage_input: str, token_budget: int = 6000,
                 max_section_chars: int | None = None) -> ContextPack
```

- Section order (stable, always): `SERIES_BIBLE` → `EPISODE` → `PRIOR_WORK` →
  `STAGE_INPUT`. Budget accounting in **worst-case Arabic tokens**: `tokens = ceil(chars * 0.5)`.
- Bible truncated to fit 70% of budget if oversize; `SERIES_BIBLE` never dropped
  entirely (hard floor: premise + characters + style_tokens, then truncate the rest
  with `…[truncated N chars]`).
- Deterministic: same inputs ⇒ same `text` byte-for-byte ⇒ stable `pack_sha256`.
- `PRIOR_WORK` sorted by created_at; summarized to title+one line each.
- Packer output feeds `LLMRequest.system`; the S-stage `input_hash` includes
  `pack_sha256` so any context drift invalidates the stage (Phase 1 rule 4 does the rest).

### F5. Adapters

Common contract (existing `LLMRequest`/`LLMResult`). Mappers per provider:

**anthropic_llm.py** — `client.messages.create(model=model_id, max_tokens=..., temperature=..., system=pack, messages=[...])`;
concatenate `text` blocks from `response.content`; `usage.input_tokens/output_tokens` →
actual; `response.model` and `response.id` into `result.raw_ref`. Errors: anthropic.SDK
`AuthenticationError/PermissionDeniedError` → Permanent; `NotFoundError` → Permanent;
`BadRequestError` → Permanent; `RateLimitError`, `InternalServerError`, `APIStatusError>=500`,
`APITimeoutError/APIConnectionError` → Transient.

**openai_llm.py** — `client.chat.completions.create(model=model_id, messages=[{system}, {user}], temperature=..., max_tokens=..., response_format={"type": "json_object"} when expect_json else omitted)`;
`choices[0].message.content`; `usage.prompt_tokens/completion_tokens` → actual;
`response.id` into `raw_ref`; errors by `status_code`: 401/403/404/400/413 → Permanent;
429/>=500/timeout/connection → Transient.

Both: `verify=True` TLS; explicit `timeout`; **no streaming**; **no tool use** in Phase 2;
retry handled ONLY by the gateway/worker backoff — adapters never retry internally.
Empty/None content → `TransientProviderError("empty completion")`. JSON-expected
validation (`LLMRequest.expect_json`) parses `json.loads(content)`; on failure raise
`PermanentProviderError` with a truncated excerpt (first 200 chars, no secrets).

`factory.py`: provider name → class binding map; unknown provider → `ConfigError`;
SDK missing → `ConfigError("LLM adapter 'anthropic' requires ai-studio[providers]")`.

### F6. Stages S1–S4 (`pipeline/stages_text.py`)

Specs registered under `text_specs()` (S1→S2→S3→S4), subject = episode subject for S1/S2,
track subject for S3, deliverable subject for S4. All calls through `gateway.llm(...)`.

- **S1 `bible_upsert`** (llm.creative, capability `llm`, purpose `bible_upsert`):
  Input: concept text + series defaults. Output JSON schema:
  `{title, premise, world, characters[], style_tokens[], tone[], continuity_notes}`.
  Writes `series_bible` row (version bump when content sha differs; identical content ⇒
  no new version — idempotent). Stores the bible JSON as CAS asset `kind='bible'`.
  Audit `series_bible_updated` with version + sha.
- **S2 `narration_script`** (creative then light):
  S2a: `narration_script` purpose — creative writes the shot-by-shot narration JSON
  `{lines: [{no, text_tashkeel, language}]}` in full tashkeel per F1.
  S2b: `tashkeel_verify` purpose — light model re-emits each line; validated by F3;
  mismatch → one retry → `tashkeel_mismatch` audit + terminal StudioError.
  Persists `narration_lines` rows (existing table; keep `page_index` semantics — check
  schema and reuse, do not fork). Strip-equality is a hard gate, not a score.
- **S3 `shot_plan`** (llm.creative): JSON `{shots: [{no, duration_s, visual_prompt,
  characters[], camera}]}`. Writes `shots` rows per track (create track/deliverable rows
  as the demo does). Continuity: every referenced character must exist in the bible,
  else PermanentProviderError with the offending name.
- **S4 `metadata_safety`** (llm.light, purpose `metadata_safety`): per deliverable
  (ar + en when bilingual): JSON `{title, description, tags[], safety: 'clear'|'flagged',
  issues[]}`. Writes `deliverables.metadata_json` + `safety_status`
  (`flagged` ⇒ run ends `awaiting_approval` at G3 anyway; metadata is decision input).

DAG wiring: `advance_run` semantics unchanged; S4 depends on all G1-approved deliverables'
`demo_finalize`-equivalent stage_runs for production runs; the demo pipeline is not edited.

### F7. CLI additions

- `studio run --production --allow-paid-llm --concept "…" [--series-id ID] [--languages ar,en] [--aspects 9:16]`
  — requires: `--allow-paid-llm` present, profile != sandbox reaching real models is
  expected to be `draft` **after TTS/image/video verify**; until then, production runs
  use the LLM-only subset (S1–S4) and stop before media stages with a clear message.
  Prints projected worst-case cost (chars × 0.5 × rate) before creating the run.
- `studio doctor` gains two rows: `Anthropic LLM (claude-sonnet-4-5-20250929, verified 2026-11-09)` and
  `OpenAI LLM (gpt-4o-mini, verified 2026-11-09)`; plus a live `--llm` flag that performs
  one 1-token provider ping per adapter (cost ≈ 0) — opt-in, never in CI.
- `studio db bible [--series ID]`: prints bible versions with sha prefixes.

### F8. settings.yaml additions

```yaml
text_pipeline: {context_token_budget: 6000, tashkeel_retry: 1, s4_after_g1: true}
```
Loader validation: keys known, `context_token_budget` int in [500, 100000],
`tashkeel_retry` 0..2, `s4_after_g1` bool — unknown keys rejected (T-CFG-01 style).

## G. Test specification (Phase 2 — all mandatory)

**Adapters (offline, stubbed HTTP via httpx.MockTransport / SDK monkeypatch)**
| ID | Assertion |
|---|---|
| T-ADP-01 | Anthropic request built correctly: model, max_tokens, system=pack, timeout; no streaming/tool use |
| T-ADP-02 | Anthropic usage → `actual_cost_musd` matches hand-computed integer musd (in=1200,out=350 @ $3/$15) |
| T-ADP-03 | Anthropic errors map: 401→Permanent, 400→Permanent, 429→Transient, 500/529→Transient, timeout→Transient |
| T-ADP-04 | Anthropic empty content → TransientProviderError |
| T-ADP-05 | OpenAI request built correctly; `expect_json` sets `response_format={"type":"json_object"}` |
| T-ADP-06 | OpenAI usage → actual musd matches; `gpt-4o-mini` alias used verbatim |
| T-ADP-07 | OpenAI errors map identically to T-ADP-03 |
| T-ADP-08 | Missing SDK → ConfigError with install hint; unknown provider name → ConfigError |
| T-ADP-09 | request_json never contains the API key (T-GW-08 extension for both adapters) |

**Tashkeel validator**
| ID | Assertion |
|---|---|
| T-TASH-01 | Equal-after-strip passes: same letters, added harakat pass |
| T-TASH-02 | Tatweel stripped before comparison |
| T-TASH-03 | Superscript alef (0670) stripped |
| T-TASH-04 | Quranic mark ranges (06D6–06ED) stripped |
| T-TASH-05 | Letter change (ك→ق) fails with correct first_mismatch index |
| T-TASH-06 | Dropped/inserted diacritic-only → ok; dropped letter → fail |
| T-TASH-07 | NFC: composed vs decomposed input compare equal |
| T-TASH-08 | Empty strings and whitespace-only inputs handled (ok iff equal) |

**Packer**
| ID | Assertion |
|---|---|
| T-PACK-01 | Same inputs → identical `pack_sha256` |
| T-PACK-02 | Oversize bible → truncated with `…[truncated N chars]`; premise+characters survive |
| T-PACK-03 | Budget accounting uses 0.5 tok/char (Arabic), hard cap respected |
| T-PACK-04 | Sections appear in fixed order; unknown sections impossible |
| T-PACK-05 | `PRIOR_WORK` sorted by created_at; empty list omits section cleanly |
| T-PACK-06 | pack_sha256 feeds S-stage input_hash (changing pack invalidates stage) |

**Stages (offline, mock LLM scripted per purpose)**
| ID | Assertion |
|---|---|
| T-S1-01 | Fresh series: bible v1 created; sha256 stable on re-pack |
| T-S1-02 | Identical concept re-run → no new version (idempotent); cache hit, zero spend |
| T-S1-03 | Changed concept → bible v2; v1 retained; audit carries versions |
| T-S2-01 | narration_lines persisted with tashkeel text; language per episode settings |
| T-S2-02 | Mock emits letters-altered text → one retry → second failure → stage_run failed + `tashkeel_mismatch` audit |
| T-S2-03 | Retry succeeds → stage proceeds; exactly 2 provider calls (cache keys differ by attempt nonce) |
| T-S3-01 | Shots written per track; continuity-violating character name → PermanentProviderError, no shots written |
| T-S3-02 | Shot order and durations preserved; S3 input_hash includes pack sha |
| T-S4-01 | Metadata written per deliverable (1 or 2 per track by language); safety_status set |
| T-S4-02 | Flagged content → safety_status='flagged'; run still parks at G3 |
| T-E2E-TEXT-01 | Sandbox end-to-end via mocks: seed → S1..S4 → run reaches G1, zero spend, zero paid calls in ledger |

**Live (opt-in)**
| ID | Assertion |
|---|---|
| T-LIVE-01 | With STUDIO_LIVE_LLM=1: one bible_upsert call against real Anthropic + one tashkeel_verify against real OpenAI; prints usage musd; skipped and NOT collected otherwise; never in CI |

**Regression**
| ID | Assertion |
|---|---|
| T-REGR-01 | Phase 1 suite: 145/145 green, coverage ≥ 90%, ruff clean, mypy strict clean |
| T-REGR-02 | `draft` still raises on shipped registry (TTS/image/video unverified); `sandbox` resolves; T-CFG-04 unchanged |

**Coverage:** ≥ 90% aggregate (same gate); report per-file.

## H. CI and dependencies

`pip install -e .[dev,providers]` in CI; matrix unchanged (ubuntu+windows × 3.11/3.12);
`STUDIO_LIVE_LLM` is never set in CI (assert absence in a test). mypy strict on new files;
SDK type stubs: `anthropic` and `openai` ship py.typed — no stubs packages needed.

## I. Working method and deliverable format

Same rules as Phase 1. In addition: do not weaken T-CFG-04's assertion to make the suite
green; if implementation diverges, document under "Decisions" in the report. If a model
ID or price in `configs/model_registry.yaml` changes, the operator must re-approve
(update `pricing_verified_on`) in the same PR.

## J. Final report (required)

`PHASE2_REPORT.md`: (1) test results by ID; (2) coverage numbers; (3) deviations and why;
(4) decisions on ambiguities; (5) known limitations; (6) exact commands to run the
sandbox end-to-end and the opt-in live smoke on Linux and Windows; (7) pasted `studio
doctor` output showing the two LLM rows verified.

## K. Definition of Done

- All Phase 2 tests pass; Phase 1 tests still pass; coverage aggregate ≥ 90%
- `studio doctor` shows both LLM entries verified with dates; NVENC still WARN-only
- `studio run --demo --profile sandbox`: completed, gates approve, unchanged bytes semantics
- Live smoke (operator-invoked) completes and prints real token usage + musd cost
- No SDK imports outside `providers/`; no spend without `--allow-paid-llm` or the gateway
- `PHASE2_REPORT.md` complete; `BEGIN_PROMPT.md` untouched

# ═══════════ END PROMPT ═══════════

## Appendix 1 — Phase 1 non-blocking observations (recorded, not fixed in Phase 2)

1. `db/repos/audit.py` 59% line coverage — thin append-only helpers; acceptable.
2. `engine.py`/`worker.py` 87% coverage — residual misses are defensive early returns;
   aggregate gate is enforced in CI.
3. Crash matrix covers mock job-based providers only; real-provider crash behavior is
   validated implicitly by the T-LIVE-01 smoke, not by fault injection (Phase 4 concern).
4. `after_submit_before_persist` resumes by re-submitting one provider call (logged via
   `potential_orphan`) — inherent to providers without idempotency keys; documented.
5. Input invalidation runs inside `advance_run`; completed runs go stale only when the
   run is reopened (gate decision / request_changes). S1–S4 inherit this semantics —
   changing it is a Phase 5 (orchestration) decision.
6. Registry: TTS/image/video remain placeholders; `draft` and `final` are blocked by
   design until the operator verifies them. `final` additionally requires
   `commercial_use_verified=true` on keyframe+video (current: `false`).
7. Suite datetime: `asyncio_default_fixture_loop_scope` deprecation warning is cosmetic.

## Appendix 2 — Verified model facts (2026-11-09)

| Field | Anthropic llm.creative | OpenAI llm.light |
|---|---|---|
| API ID | `claude-sonnet-4-5-20250929` | `gpt-4o-mini` |
| Context | 200K (1M via beta header, not used) | 128K |
| Max output | 64K (we use ≤ 4096) | 16,384 |
| Input price | $3.00 / 1M | $0.15 / 1M |
| Output price | $15.00 / 1M | $0.60 / 1M |
| Cached input | $0.30 / 1M | $0.075 / 1M (not wired in Phase 2) |
| Release | 2025-09-29 | 2024-07-18 (snapshot) |
| Commercial use | Approved (API terms) | Approved (API terms) |

Sources: https://platform.claude.com/docs/en/models/overview (official, fetched
2026-11-09 — note the same page lists newer `claude-sonnet-4-6` $3/$15 1M ctx,
`claude-sonnet-5` $2/$10 1M ctx, `claude-sonnet-5-5` $2/$10 1M ctx, `claude-opus-5`
$5/$25, `claude-haiku-4-5` $1/$5); https://platform.openai.com/docs/pricing (via trackers
checked 2026-07..2026-10; official page body not fetchable verbatim in this environment —
**operator should open it once before Phase 2 spend**). Claude 3.7 Sonnet: absent from the
current models table — do not use.
