"""Production (LLM-only subset) helpers for `studio run --production`.

Until TTS/image/video are verified in `configs/model_registry.yaml`
(Phase 4), a production run executes only S1-S4 against real (paid) LLM
endpoints and stops before media. This module seeds the episode and
projects the worst-case LLM cost *before* anything is written.

No provider calls happen here — the projection is arithmetic on registry
pricing. The operator's `--allow-paid-llm` flag is the consent record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from studio.config.channels import channel_for
from studio.config.languages import resolve_languages
from studio.config.loader import AppConfig
from studio.config.registry import Per1mTokens, ResolvedProfile
from studio.core.ids import Ids
from studio.core.pricing import estimate_cost_musd
from studio.db.repos import StudioRepos
from studio.domain.enums import Aspect, Language
from studio.domain.errors import StudioError
from studio.providers.base import LLMRequest

_S1_MAX_TOKENS = 4096
_S2_MAX_TOKENS = 4096
_VERIFY_MAX_TOKENS = 1024
_S3_MAX_TOKENS = 4096
_S4_MAX_TOKENS = 1024
_PROMPT_CHARS_EST = 512  # fixed overhead of the user-side instruction block


@dataclass(frozen=True)
class ProductionSeed:
    series_id: str
    episode_id: str
    track_ids: list[str]
    deliverable_ids: list[str]
    languages: list[Language]
    aspects: list[Aspect]
    series_created: bool


def seed_production(
    config: AppConfig,
    repos: StudioRepos,
    *,
    concept: str,
    series_id: str | None,
    languages: Iterable[str | Language] | None,
    aspects: Iterable[str | Aspect] | None,
    ids: Ids,
) -> ProductionSeed:
    """Create (or reuse) a series and seed one episode with tracks and
    deliverables. Does NOT create a narrator: real voices are wired in
    Phase 4, and mock voice ids must not leak into production rows."""
    concept = concept.strip()
    if not concept:
        raise StudioError("--concept must not be empty")
    settings = config.settings
    resolved_languages = (
        resolve_languages(settings, languages)
        if languages is not None
        else resolve_languages(settings)
    )
    if aspects is None:
        resolved_aspects = list(settings.aspects)
    else:
        resolved_aspects = []
        seen: set[str] = set()
        for item in aspects:
            aspect = item if isinstance(item, Aspect) else Aspect(str(item))
            if aspect.value in seen:
                raise StudioError(f"duplicate aspect: {aspect.value}")
            seen.add(aspect.value)
            resolved_aspects.append(aspect)
        if not resolved_aspects:
            raise StudioError("at least one aspect is required")

    if series_id is not None:
        series = repos.content.get_series(series_id)
        if series is None:
            raise StudioError(f"series not found: {series_id}")
        series_created = False
    else:
        short = ids.new()[:8].lower()
        slug_source = "".join(ch if ch.isalnum() else "-" for ch in concept.lower())
        slug = f"production-{slug_source[:40].strip('-')}-{short}" or f"production-{short}"
        series = repos.content.create_series(name=concept[:80], slug=slug)
        series_created = True

    title = " ".join(concept.split())[:80]
    episode = repos.content.create_episode(
        series_id=series.id,
        title=title,
        concept=concept,
        languages_json=json.dumps(
            [lang.value for lang in resolved_languages], separators=(",", ":")
        ),
        aspects_json=json.dumps(
            [a.value for a in resolved_aspects], separators=(",", ":")
        ),
    )
    track_ids: list[str] = []
    deliverable_ids: list[str] = []
    for aspect in resolved_aspects:
        track = repos.content.create_track(episode_id=episode.id, aspect=aspect.value)
        track_ids.append(track.id)
        for language in resolved_languages:
            channel_id = channel_for(language, aspect, config.channels)
            deliverable = repos.content.create_deliverable(
                track_id=track.id, language=language.value, channel_id=channel_id
            )
            deliverable_ids.append(deliverable.id)
    return ProductionSeed(
        series_id=series.id,
        episode_id=episode.id,
        track_ids=track_ids,
        deliverable_ids=deliverable_ids,
        languages=resolved_languages,
        aspects=resolved_aspects,
        series_created=series_created,
    )


def project_llm_cost_musd(
    config: AppConfig,
    resolved: ResolvedProfile,
    *,
    n_tracks: int,
    n_deliverables: int,
    director_chars: int,
    pack_cap_chars: int,
    tashkeel_retries: int,
) -> list[tuple[str, int]]:
    """Worst-case cost per LLM call of the S1-S4 subset, in micro-USD.

    Input tokens are upper-bounded by ceil((director + pack-cap + fixed
    prompt) * worst_case); output tokens by each stage's max_tokens.
    Actual charges come from provider-reported usage and are reconciled by
    the gateway; this projection exists so the operator can refuse before
    a single token is spent.
    """
    worst = config.settings.pricing.llm_tokens_per_char_worst_case
    system_chars = director_chars + pack_cap_chars + 64  # separators/newlines

    def estimate(model_key: str, max_tokens: int) -> int:
        entry = config.registry.models[model_key]
        if not isinstance(entry.pricing, Per1mTokens):
            raise StudioError(f"model {model_key!r} is not per_1m_tokens priced")
        req = LLMRequest(
            system="x" * system_chars,
            prompt="x" * _PROMPT_CHARS_EST,
            max_tokens=max_tokens,
        )
        return estimate_cost_musd(entry, req, tokens_per_char_worst_case=worst)

    creative = resolved.model_key_for("llm_creative")
    light = resolved.model_key_for("llm_light")

    rows: list[tuple[str, int]] = []
    rows.append(("S1 bible_upsert (creative)", estimate(creative, _S1_MAX_TOKENS)))
    rows.append(("S2 narration_script (creative)", estimate(creative, _S2_MAX_TOKENS)))
    for attempt in range(tashkeel_retries + 1):
        label = f"S2 tashkeel_verify #{attempt + 1} (light)"
        rows.append((label, estimate(light, _VERIFY_MAX_TOKENS)))
    for index in range(n_tracks):
        label = f"S3 shot_plan[{index + 1}/{n_tracks}] (creative)"
        rows.append((label, estimate(creative, _S3_MAX_TOKENS)))
    for index in range(n_deliverables):
        label = f"S4 metadata_safety[{index + 1}/{n_deliverables}] (light)"
        rows.append((label, estimate(light, _S4_MAX_TOKENS)))
    return rows
