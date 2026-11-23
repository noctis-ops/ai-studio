"""Export directory layout and the atomic write protocol (Phase 5 §K).

```
<root>/<channel_key>/<YYYYMMDD>_<slug>/            first export
<root>/<channel_key>/<YYYYMMDD>_<slug>_r2/         a later approved render
```

The date comes from the deliverable's *first* export (persisted on the
`exports` row), so re-running S13 never mints a new date directory. A new
approved render is a new revision: prior directories are never overwritten.

An export directory is built in `<dir>.tmp-<uuid>`, fsynced, then renamed into
place, so a crash leaves only a `.tmp-*` directory (never a half-written
`metadata.json`).
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# §K: warn (never fail) when the whole path gets long enough to bite on
# Windows' legacy MAX_PATH.
PATH_WARN_CHARS = 240

_REVISION_SUFFIX = "_r"


@dataclass(frozen=True)
class ExportTarget:
    root: Path
    channel_key: str
    date_str: str
    slug: str
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("revision starts at 1")
        if not self.slug or self.slug != self.slug.lower():
            raise ValueError(f"slug must be lowercase and non-empty: {self.slug!r}")

    @property
    def dir_name(self) -> str:
        base = f"{self.date_str}_{self.slug}"
        return base if self.revision == 1 else f"{base}{_REVISION_SUFFIX}{self.revision}"

    @property
    def channel_dir(self) -> Path:
        return self.root / self.channel_key

    @property
    def path(self) -> Path:
        return self.channel_dir / self.dir_name

    def path_warning(self) -> str | None:
        text = str(self.path.resolve())
        if len(text) > PATH_WARN_CHARS:
            return (
                f"export path is {len(text)} characters long (> {PATH_WARN_CHARS}); "
                "some tools still fail past MAX_PATH"
            )
        return None


def export_root(settings: Any, *, profile: str) -> tuple[Path, str]:
    """(root directory, kind) for a run profile; draft-like profiles go to
    `export.draft_root` so a draft never looks like a published deliverable."""
    home = Path(settings.paths.home)
    if profile == "final":
        return home / settings.export.root, "final"
    return home / settings.export.draft_root, "draft"


def date_str(when: date) -> str:
    return when.strftime("%Y%m%d")


def next_revision(
    root: Path, *, channel_key: str, date_str_value: str, slug: str
) -> int:
    """The first free revision for this (channel, date, slug)."""
    channel_dir = root / channel_key
    base = f"{date_str_value}_{slug}"
    revision = 1
    while True:
        name = base if revision == 1 else f"{base}{_REVISION_SUFFIX}{revision}"
        if not (channel_dir / name).exists():
            return revision
        revision += 1


def temp_dir_for(target: ExportTarget) -> Path:
    """Sibling scratch directory; only `.tmp-*` can survive a crash."""
    return target.channel_dir / f".tmp-{target.dir_name}-{uuid.uuid4().hex[:8]}"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform-dependent
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_paths(root: Path) -> None:
    """fsync every file under `root`, then the directories themselves."""
    for item in sorted(root.rglob("*")):
        if item.is_file():
            with item.open("rb") as handle:
                os.fsync(handle.fileno())
    for item in sorted(root.rglob("*"), reverse=True):
        if item.is_dir():
            _fsync_dir(item)
    _fsync_dir(root)


def publish_dir(tmp_dir: Path, final_dir: Path) -> None:
    """Atomically move a finished scratch directory into place."""
    if final_dir.exists():
        raise FileExistsError(f"export directory already exists: {final_dir}")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    fsync_paths(tmp_dir)
    os.replace(tmp_dir, final_dir)
    _fsync_dir(final_dir.parent)


def remove_temp_dir(tmp_dir: Path) -> None:
    shutil.rmtree(tmp_dir, ignore_errors=True)


def leftover_temp_dirs(root: Path) -> list[Path]:
    """Scratch directories a crashed export left behind (doctor + `purge gc`)."""
    if not root.exists():
        return []
    return sorted(p for p in root.rglob(".tmp-*") if p.is_dir())
