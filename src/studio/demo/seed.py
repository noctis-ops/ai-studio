"""Seed a demo series/episode: narrator, tracks (one per aspect), and one
deliverable per (track x language) via the channel map."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from studio.config.channels import channel_for
from studio.config.languages import resolve_languages
from studio.config.loader import AppConfig
from studio.core.clock import format_ts
from studio.core.ids import Ids, UlidIds
from studio.db.repos import StudioRepos
from studio.demo.library import ensure_demo_library
from studio.domain.enums import Aspect, Language

_DEMO_TITLE = "Studio Demo — The Lantern"
_DEMO_CONCEPT = (
    "A short fable about a lighthouse keeper who befriends a storm. "
    "Three beats, one hook, one turn, one landing."
)


@dataclass(frozen=True)
class SeedResult:
    series_id: str
    episode_id: str
    narrator_id: str
    track_ids: list[str]
    deliverable_ids: list[str]
    languages: list[Language]
    aspects: list[Aspect]


def seed_demo(
    config: AppConfig,
    repos: StudioRepos,
    *,
    languages: Iterable[str | Language] | None = None,
    aspects: Iterable[str | Aspect] | None = None,
    ids: Ids | None = None,
) -> SeedResult:
    ids = ids if ids is not None else UlidIds()
    settings = config.settings
    resolved_languages = (
        resolve_languages(settings, languages)
        if languages is not None
        else resolve_languages(settings)
    )
    if aspects is None:
        resolved_aspects = list(settings.aspects)
    else:
        raw = list(aspects)
        resolved_aspects = []
        seen: set[str] = set()
        for item in raw:
            aspect = item if isinstance(item, Aspect) else Aspect(str(item))
            if aspect.value in seen:
                raise ValueError(f"duplicate aspect: {aspect.value}")
            seen.add(aspect.value)
            resolved_aspects.append(aspect)
        if not resolved_aspects:
            raise ValueError("at least one aspect is required")

    narrator_voices = {lang.value: f"mock-voice-{lang.value}" for lang in resolved_languages}
    short = ids.new()[:8].lower()
    series = repos.content.create_series(name="Studio Demo", slug=f"studio-demo-{short}")
    narrator = repos.content.create_character(
        series_id=series.id,
        role="narrator",
        name="Narrator",
        voices_json=json.dumps(narrator_voices, sort_keys=True, separators=(",", ":")),
    )
    # Phase 3: seed character_voices so the sandbox runs the real audio chain
    # (mock TTS via the mock.tts registry entry). Attestation stamped so the
    # demo is also valid for attestation checks; model_key pins the mock
    # provider (never the placeholder real entries).
    attested_at = format_ts(datetime.now(UTC))
    for language in resolved_languages:
        repos.audio.upsert_character_voice(
            series_id=series.id,
            character_slug=narrator.role,
            language=language.value,
            voice_id=f"mock-voice-{language.value}",
            settings_json=json.dumps(
                {
                    "model_key": "mock.tts",
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "license_attested_at": attested_at,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    episode = repos.content.create_episode(
        series_id=series.id,
        title=_DEMO_TITLE,
        concept=_DEMO_CONCEPT,
        languages_json=json.dumps([lang.value for lang in resolved_languages],
                              separators=(",", ":")),
        aspects_json=json.dumps([a.value for a in resolved_aspects], separators=(",", ":")),
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
    # Phase 3: generate the sandbox audio library (fixture WAVs + index
    # sidecars) and upsert the rows, so S7 has an active library to plan with.
    ensure_demo_library(config, repos)
    return SeedResult(
        series_id=series.id,
        episode_id=episode.id,
        narrator_id=narrator.id,
        track_ids=track_ids,
        deliverable_ids=deliverable_ids,
        languages=resolved_languages,
        aspects=resolved_aspects,
    )


def narrator_voice(repos: StudioRepos, series_id: str, language: Language) -> str:
    narrator = repos.content.get_narrator(series_id)
    if narrator is None:
        raise ValueError(f"series {series_id} has no narrator character")
    voices = json.loads(narrator.voices_json)
    voice = voices.get(language.value)
    if voice is None:
        raise ValueError(f"narrator has no voice configured for language {language.value}")
    return str(voice)
