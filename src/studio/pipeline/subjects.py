"""Subject construction helpers (lineage, relatedness, upstream hashing)."""

from __future__ import annotations

from studio.domain.enums import Scope
from studio.domain.refs import Lineage, SubjectRef, subjects_related

__all__ = [
    "deliverable_subject",
    "episode_lineage",
    "episode_subject",
    "lineage_for",
    "related",
    "shot_subject",
    "track_subject",
]


def lineage_for(
    series_id: str | None, episode_id: str | None, track_id: str | None = None
) -> Lineage:
    lineage: list[tuple[Scope, str]] = []
    if series_id:
        lineage.append((Scope.SERIES, series_id))
    if episode_id:
        lineage.append((Scope.EPISODE, episode_id))
    if track_id:
        lineage.append((Scope.TRACK, track_id))
    return tuple(lineage)


def episode_lineage(series_id: str | None) -> Lineage:
    return lineage_for(series_id, None)


def episode_subject(series_id: str | None, episode_id: str) -> SubjectRef:
    return SubjectRef(scope=Scope.EPISODE, id=episode_id, lineage=episode_lineage(series_id))


def track_subject(series_id: str | None, episode_id: str, track_id: str) -> SubjectRef:
    return SubjectRef(
        scope=Scope.TRACK,
        id=track_id,
        lineage=lineage_for(series_id, episode_id),
    )


def deliverable_subject(
    series_id: str | None, episode_id: str, track_id: str, deliverable_id: str
) -> SubjectRef:
    return SubjectRef(
        scope=Scope.DELIVERABLE,
        id=deliverable_id,
        lineage=lineage_for(series_id, episode_id, track_id),
    )


def shot_subject(series_id: str | None, episode_id: str, track_id: str, shot_id: str) -> SubjectRef:
    return SubjectRef(
        scope=Scope.SHOT,
        id=shot_id,
        lineage=lineage_for(series_id, episode_id, track_id),
    )


def related(a: SubjectRef, b: SubjectRef) -> bool:
    return subjects_related(a, b)
