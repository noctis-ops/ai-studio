"""Publish marker (Phase 5 §M, S15 — a service, not an engine node).

`mark_published` is the only thing that turns an export into a *published*
deliverable:

- it requires an `exports` row, so a deliverable is never "published" without a
  package on disk;
- it stamps `published_at` / `published_url` and writes an audit row;
- when every deliverable of the episode is published, the episode becomes
  `published` — and from then on the barrier refuses paid stages
  (`EpisodeClosed`) while free stages (a re-export) still run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from studio.domain.errors import StudioError

_ACCEPTED_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def parse_published_at(value: str | None, *, clock: Any) -> str:
    """ISO-8601 (UTC) for the marker; defaults to the clock's now."""
    if value is None:
        now: datetime = clock.now()
        return now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    text = value.strip()
    if not text:
        raise StudioError("--published-at must not be empty")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in _ACCEPTED_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise StudioError(
                f"--published-at {value!r} is not ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
            ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mark_published(
    repos: Any,
    *,
    deliverable_id: str,
    url: str,
    published_at: str | None = None,
    clock: Any,
    actor: str = "operator",
) -> dict[str, Any]:
    """Record a publication; returns what changed (deliverable + episode)."""
    deliverable = repos.content.get_deliverable(deliverable_id)
    if deliverable is None:
        raise StudioError(f"unknown deliverable {deliverable_id}")
    if not url.strip():
        raise StudioError("--url is required: a publication without a URL is not a fact")
    exports = repos.exports.list_for_deliverable(deliverable_id)
    if not exports:
        raise StudioError(
            f"deliverable {deliverable_id} has no export yet; run `studio export` first"
        )
    stamp = parse_published_at(published_at, clock=clock)
    repos.content.set_deliverable_published(
        deliverable_id, published_at=stamp, published_url=url.strip()
    )
    repos.audit.append(
        actor=actor,
        entity=f"deliverable:{deliverable_id}",
        event="s15_publish_mark",
        payload={
            "deliverable_id": deliverable_id,
            "url": url.strip(),
            "published_at": stamp,
            "export_dir": exports[-1].dir_path,
        },
    )

    track = repos.content.get_track(deliverable.track_id)
    episode_id = track.episode_id if track is not None else None
    episode_published = False
    if episode_id is not None:
        deliverables = repos.content.list_deliverables(episode_id)
        if deliverables and all(item.published_at for item in deliverables):
            repos.content.set_episode_status(episode_id, "published")
            episode_published = True
            repos.audit.append(
                actor=actor,
                entity=f"episode:{episode_id}",
                event="s15_episode_published",
                payload={
                    "deliverables": [item.id for item in deliverables],
                    "published_at": stamp,
                },
            )
    return {
        "deliverable_id": deliverable_id,
        "episode_id": episode_id,
        "published_at": stamp,
        "url": url.strip(),
        "episode_published": episode_published,
    }
