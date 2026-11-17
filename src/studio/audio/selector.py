"""Track selection primitives for the Phase 3 audio plan (PHASE3 §H8).

Pure and deterministic: **no RNG anywhere**. Ties are broken by ascending
``sha256(f"{episode_id}:{segment_idx}:{track_sha256}")`` so every run — and
every language of the same episode — makes the identical choice.

The scoring formula (spec §H8):

    score = 3 * mood_match - |intensity_diff| - recent_penalty - loop_penalty
            - same_as_previous_segment_penalty

- ``mood_match`` is 1.0 for an exact tag, the ``mood_affinity`` weight for a
  neighbour of the requested mood, else 0.
- ``recent_penalty`` applies when the track was used in the last
  ``recent_penalty_episodes`` episodes of the series.
- ``loop_penalty`` applies when the track is shorter than the segment but is
  loopable (looping is fine, just not free of charge in the scoring).
- ``same_as_previous_segment_penalty`` — the spec lists it in the formula but
  attaches no config key. A documented constant (0.5) discourages back-to-back
  reuse across segment boundaries. Recorded under Decisions in PHASE3_REPORT.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SAME_AS_PREVIOUS_SEGMENT_PENALTY = 0.5


def row_tags(row: Any) -> tuple[str, ...]:
    """Controlled-vocab tags carried by a library row (``moods_json``)."""
    try:
        data = json.loads(getattr(row, "moods_json", "[]") or "[]")
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(str(item) for item in data)


def mood_match(moods: tuple[str, ...], mood: str | None,
               affinity: dict[str, dict[str, float]]) -> float:
    if not mood:
        return 0.0
    if mood in moods:
        return 1.0
    best = 0.0
    neighbors = affinity.get(mood, {}) if isinstance(affinity, dict) else {}
    for neighbor, weight in neighbors.items():
        if str(neighbor) in moods:
            try:
                best = max(best, float(weight))
            except (TypeError, ValueError):
                continue
    return best


def tie_break_key(episode_id: str, segment_idx: int, track_sha256: str) -> str:
    return hashlib.sha256(f"{episode_id}:{segment_idx}:{track_sha256}".encode()).hexdigest()


def score_music_track(
    row: Any,
    *,
    mood: str | None,
    intensity: int,
    episode_id: str,
    segment_idx: int,
    segment_duration_ms: int,
    recent_shas: set[str],
    previous_sha: str | None,
    affinity: dict[str, dict[str, float]],
    recent_penalty: float,
    loop_penalty: float,
) -> float:
    moods = row_tags(row)
    match = mood_match(moods, mood, affinity)
    track_intensity = row.intensity if row.intensity is not None else intensity
    score = 3.0 * match
    score -= abs(int(track_intensity) - int(intensity))
    if row.sha256 in recent_shas:
        score -= float(recent_penalty)
    if int(row.duration_ms) < int(segment_duration_ms) and int(row.loopable) == 1:
        score -= float(loop_penalty)
    if previous_sha is not None and row.sha256 == previous_sha:
        score -= SAME_AS_PREVIOUS_SEGMENT_PENALTY
    return score


@dataclass(frozen=True)
class RankedTrack:
    score: float
    tie_break: str
    row: Any


def rank_music_tracks(
    rows: list[Any],
    *,
    mood: str | None,
    intensity: int,
    episode_id: str,
    segment_idx: int,
    segment_duration_ms: int,
    recent_shas: set[str],
    previous_sha: str | None,
    affinity: dict[str, dict[str, float]],
    recent_penalty: float,
    loop_penalty: float,
) -> list[RankedTrack]:
    ranked = [
        RankedTrack(
            score=score_music_track(
                row,
                mood=mood,
                intensity=intensity,
                episode_id=episode_id,
                segment_idx=segment_idx,
                segment_duration_ms=segment_duration_ms,
                recent_shas=recent_shas,
                previous_sha=previous_sha,
                affinity=affinity,
                recent_penalty=recent_penalty,
                loop_penalty=loop_penalty,
            ),
            tie_break=tie_break_key(episode_id, segment_idx, row.sha256),
            row=row,
        )
        for row in rows
    ]
    ranked.sort(key=lambda item: (-item.score, item.tie_break))
    return ranked


def any_mood_match(rows: list[Any], mood: str | None,
                   affinity: dict[str, dict[str, float]]) -> bool:
    if not mood:
        return False
    return any(mood_match(row_tags(row), mood, affinity) > 0.0 for row in rows)


def closest_by_intensity(rows: list[Any], intensity: int) -> Any | None:
    if not rows:
        return None
    return min(rows, key=lambda row: abs(int(row.intensity or 3) - int(intensity)))


def select_transition_sfx(
    rows: list[Any], *, tag: str, episode_id: str, boundary_idx: int
) -> Any | None:
    matching = [row for row in rows if tag in row_tags(row)]
    if not matching:
        return None
    matching.sort(key=lambda row: tie_break_key(episode_id, boundary_idx, row.sha256))
    return matching[0]


def select_ambience(
    rows: list[Any], *, tags: list[str], episode_id: str, segment_idx: int
) -> Any | None:
    if not tags:
        return None
    wanted = set(tags)

    def overlap(row: Any) -> int:
        return len(wanted & set(row_tags(row)))

    best = max((overlap(row) for row in rows), default=0)
    if best <= 0:
        return None
    contenders = [row for row in rows if overlap(row) == best]
    contenders.sort(key=lambda row: tie_break_key(episode_id, segment_idx, row.sha256))
    return contenders[0]
