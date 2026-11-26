"""Content-addressed storage.

Files are written to `<root>/ab/cd/<sha256>.<ext>` via a temp file in the
destination directory + `os.replace`. The file is written BEFORE the DB row;
an orphan file is tolerated, an orphan row is not. Zero-byte content is
rejected before anything touches disk.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from pathlib import Path

from studio.core.clock import Clock
from studio.db.connection import Database
from studio.db.repos.assets import AssetsRepo, SqliteAssetsRepo
from studio.domain.errors import AssetNotFoundError, ZeroByteAssetError
from studio.domain.models import AssetRow
from studio.domain.refs import AssetRef
from studio.video.probe import probe

_HASH_CHUNK = 1024 * 1024
_PROBE_KINDS = {"audio", "video", "image"}


class ContentStore:
    def __init__(
        self,
        root: Path,
        db: Database,
        clock: Clock,
        assets: AssetsRepo | None = None,
    ) -> None:
        self.root = Path(root)
        self.db = db
        self.clock = clock
        self.assets: AssetsRepo = assets if assets is not None else SqliteAssetsRepo(db, clock)

    # ---- writes --------------------------------------------------------

    def put_file(
        self,
        src: Path,
        *,
        kind: str,
        ext: str,
        retention: str = "intermediate",
        move: bool = True,
        purge_after: str | None = None,
    ) -> AssetRef:
        src = Path(src)
        if not src.is_file():
            raise FileNotFoundError(f"CAS source does not exist: {src}")
        sha, size = self._hash_file(src)
        if size == 0:
            raise ZeroByteAssetError(f"refusing to store zero-byte file: {src}")
        ext = _clean_ext(ext)
        probed = probe(src) if kind in _PROBE_KINDS else None
        dest = self._dest(sha, ext)
        rel_path = _rel_path(sha, ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            self._copy_atomic(src, dest)
        if move:
            try:
                src.unlink(missing_ok=True)
            except OSError:
                pass
        return self._upsert(
            sha256=sha,
            rel_path=rel_path,
            kind=kind,
            ext=ext,
            size=size,
            retention=retention,
            purge_after=purge_after,
            duration_ms=probed.duration_ms if probed else None,
            width=probed.width if probed else None,
            height=probed.height if probed else None,
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: str,
        ext: str,
        retention: str = "intermediate",
        purge_after: str | None = None,
    ) -> AssetRef:
        if len(data) == 0:
            raise ZeroByteAssetError("refusing to store zero-byte payload")
        sha = hashlib.sha256(data).hexdigest()
        ext = _clean_ext(ext)
        dest = self._dest(sha, ext)
        rel_path = _rel_path(sha, ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            self._write_atomic(dest, data)
        return self._upsert(
            sha256=sha,
            rel_path=rel_path,
            kind=kind,
            ext=ext,
            size=len(data),
            retention=retention,
            purge_after=purge_after,
            duration_ms=None,
            width=None,
            height=None,
        )

    # ---- reads ---------------------------------------------------------

    def resolve(self, sha256: str) -> Path:
        row = self.assets.get(sha256)
        if row is None:
            raise AssetNotFoundError(f"asset not found in CAS index: {sha256}")
        if row.purged_at is not None:
            raise AssetNotFoundError(f"asset was purged: {sha256}")
        path = self.root / row.rel_path
        if not path.is_file():
            raise AssetNotFoundError(f"asset file missing on disk: {row.rel_path}")
        return path

    def exists_live(self, sha256: str) -> bool:
        if not self.assets.exists_live(sha256):
            return False
        row = self.assets.get(sha256)
        if row is None:
            return False
        return (self.root / row.rel_path).is_file()

    def mark_purged(self, sha256: str, *, reason: str | None = None) -> None:
        row = self.assets.get(sha256)
        self.assets.mark_purged(sha256, reason=reason)
        if row is not None:
            try:
                (self.root / row.rel_path).unlink(missing_ok=True)
            except OSError:
                # Purge is best-effort on disk; the DB flag is the source of truth.
                pass

    def verify(self, sha256: str) -> bool:
        try:
            path = self.resolve(sha256)
        except AssetNotFoundError:
            return False
        actual, _ = self._hash_file(path)
        return actual == sha256

    # ---- internals -------------------------------------------------------

    def _upsert(
        self,
        *,
        sha256: str,
        rel_path: str,
        kind: str,
        ext: str,
        size: int,
        retention: str,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
        purge_after: str | None = None,
    ) -> AssetRef:
        row = self.assets.upsert(
            sha256=sha256,
            rel_path=rel_path,
            kind=kind,
            mime=_mime_for(ext),
            size_bytes=size,
            duration_ms=duration_ms,
            width=width,
            height=height,
            retention=retention,
            purge_after=purge_after,
        )
        return _to_ref(row)

    def _hash_file(self, src: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with src.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _dest(self, sha256: str, ext: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.{ext}"

    def _copy_atomic(self, src: Path, dest: Path) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.stem}.", suffix=".tmp", dir=str(dest.parent)
        )
        tmp = Path(tmp_name)
        try:
            with src.open("rb") as handle, os.fdopen(fd, "wb") as out:
                while True:
                    chunk = handle.read(_HASH_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
            if dest.exists():
                # Another writer stored identical content first; keep theirs.
                tmp.unlink(missing_ok=True)
                return
            os.replace(tmp, dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _write_atomic(self, dest: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.stem}.", suffix=".tmp", dir=str(dest.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(tmp, dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


def _rel_path(sha256: str, ext: str) -> str:
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}.{ext}"


def _clean_ext(ext: str) -> str:
    ext = ext.strip().lower().lstrip(".")
    if not ext or "/" in ext or "\\" in ext or len(ext) > 16:
        raise ValueError(f"invalid extension: {ext!r}")
    return ext


def _mime_for(ext: str) -> str:
    guess, _ = mimetypes.guess_type(f"asset.{ext}")
    return guess or "application/octet-stream"


def _to_ref(row: AssetRow) -> AssetRef:
    return AssetRef(
        sha256=row.sha256,
        rel_path=row.rel_path,
        kind=row.kind,
        mime=row.mime,
        size_bytes=row.size_bytes,
        duration_ms=row.duration_ms,
        width=row.width,
        height=row.height,
    )
