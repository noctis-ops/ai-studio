"""Purge execution and garbage collection (Phase 5 §M, S15).

`run_purge` deletes a plan's files, then marks each asset purged with its
reason, and writes **one** batch audit row (`s15_purge_run`) with counts and
bytes. The order is deliberate:

1. **Protection is re-checked at deletion time**, not just at plan time: an
   asset that became protected between planning and deleting (a new run, an
   approved sheet) is skipped and reported.
2. A locked file is retried three times with 0.5 s backoff (Windows AV/indexers
   hold handles); a file that stays locked is skipped, reported and makes the
   command exit non-zero — it is never silently dropped.
3. A file that is already gone counts as deleted, so a crash between unlink and
   the DB update resumes cleanly, and a second run deletes nothing.

`run_gc` is the only path that touches files with no `assets` row at all.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from studio.domain.errors import StudioError

from .plan import PurgeItem, PurgePlan, build_plan, gc_plan
from .protect import build_protection_index

LOCK_RETRIES = 3
LOCK_BACKOFF_S = 0.5
_DELETE_REASON_PREFIX = "s15_purge"


@dataclass
class PurgeResult:
    episode_id: str
    dry_run: bool
    deleted: list[PurgeItem] = field(default_factory=list)
    skipped_locked: list[PurgeItem] = field(default_factory=list)
    newly_protected: list[PurgeItem] = field(default_factory=list)
    missing: list[PurgeItem] = field(default_factory=list)

    @property
    def deleted_bytes(self) -> int:
        return sum(item.size_bytes for item in self.deleted)

    @property
    def ok(self) -> bool:
        return not self.skipped_locked

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "dry_run": self.dry_run,
            "deleted": len(self.deleted),
            "deleted_bytes": self.deleted_bytes,
            "skipped_locked": [item.sha256 for item in self.skipped_locked],
            "newly_protected": [item.sha256 for item in self.newly_protected],
            "missing": [item.sha256 for item in self.missing],
        }


def _unlink_with_retries(
    path: Path, *, retries: int = LOCK_RETRIES, backoff_s: float = LOCK_BACKOFF_S
) -> bool:
    """True when the file is gone; False when it stayed locked.

    `retries` is the number of *re*-tries, so a Windows handle is waited for
    three times (0.5 s apart) before the file is reported as skipped.
    """
    for attempt in range(retries + 1):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:  # PermissionError on Windows: a held handle
            if attempt >= retries:
                return False
            time.sleep(backoff_s)
    return False  # pragma: no cover - loop always returns


def run_purge(
    repos: Any,
    db: Any,
    *,
    episode_id: str,
    settings: Any,
    clock: Any,
    dry_run: bool = True,
    actor: str = "operator",
    unlink: Callable[[Path], bool] | None = None,
    plan: PurgePlan | None = None,
) -> PurgeResult:
    """Delete this episode's purgeable assets (or only report them)."""
    episode = repos.content.get_episode(episode_id)
    if episode is None:
        raise StudioError(f"episode not found: {episode_id}")
    if not dry_run and episode.status != "published":
        raise StudioError(
            f"episode {episode_id} is {episode.status}; purge run needs a published episode"
        )

    resolved = plan or build_plan(repos, db, episode_id=episode_id, settings=settings, clock=clock)
    result = PurgeResult(episode_id=episode_id, dry_run=dry_run)
    cas_root = Path(settings.paths.cas)
    remove = unlink or _unlink_with_retries

    if dry_run:
        result.deleted = list(resolved.items)
        repos.audit.append(
            actor=actor,
            entity=f"episode:{episode_id}",
            event="s15_purge_dry_run",
            payload={**result.as_dict(), "classes": resolved.summary()},
        )
        return result

    # Re-check protection at deletion time (§M): work may have arrived while the
    # plan was being reviewed.
    protection = build_protection_index(db, purging_episode_id=episode_id)
    for item in resolved.items:
        if item.sha256 in protection:
            result.newly_protected.append(item)
            continue
        path = cas_root / item.rel_path
        if not path.exists():
            result.missing.append(item)
        elif not remove(path):
            result.skipped_locked.append(item)
            continue
        else:
            result.deleted.append(item)
        repos.assets.mark_purged(
            item.sha256, reason=f"{_DELETE_REASON_PREFIX}:{item.purge_class}: episode {episode_id}"
        )

    repos.audit.append(
        actor=actor,
        entity=f"episode:{episode_id}",
        event="s15_purge_run",
        payload={
            **result.as_dict(),
            "classes": resolved.summary(),
            "skipped_classes": sorted({item.purge_class for item in result.skipped_locked}),
        },
    )
    if result.skipped_locked:
        repos.audit.append(
            actor=actor,
            entity=f"episode:{episode_id}",
            event="s15_purge_skipped_locked",
            payload={"shas": [item.sha256 for item in result.skipped_locked]},
        )
    return result


def run_gc(
    repos: Any,
    db: Any,
    *,
    settings: Any,
    clock: Any,
    dry_run: bool = True,
    actor: str = "operator",
    unlink: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    """Remove CAS orphans, `data/tmp/**` and stale `.tmp-*` export directories."""
    candidates = gc_plan(db, settings=settings, clock=clock)
    remove = unlink or _unlink_with_retries
    removed: dict[str, list[str]] = {key: [] for key in candidates}
    if not dry_run:
        for key, paths in candidates.items():
            for raw in paths:
                path = Path(raw)
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed[key].append(raw)
                elif remove(path):
                    removed[key].append(raw)
        repos.audit.append(
            actor=actor,
            event="s15_purge_gc",
            entity="cas:gc",
            payload={"removed": {key: len(value) for key, value in removed.items()}},
        )
    return {
        "dry_run": dry_run,
        "candidates": {key: len(value) for key, value in candidates.items()},
        "removed": {key: len(value) for key, value in removed.items()},
        "paths": candidates,
    }
