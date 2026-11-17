"""Assets table repository."""

from __future__ import annotations

from typing import Protocol

from studio.domain.errors import ConfigError
from studio.domain.models import AssetRow

from .base import SqliteRepo, row_to_dict

# Retention lifetime ranking, longest last-write wins. `export` ranks between
# `keep_n_days` and `intermediate` (it is a durable export of an intermediate
# artifact, never shorter-lived than `intermediate`).
_RETENTION_RANK = {"intermediate": 1, "export": 2, "keep_n_days": 3, "permanent": 4}


def _retention_winner(
    old: str | None,
    old_purge_after: str | None,
    new: str,
    new_purge_after: str | None,
) -> tuple[str, str | None]:
    """Return the longer-lived of (old, new); never downgrades (D1).

    Order: permanent > keep_n_days (later purge_after wins) > export > intermediate.
    `old` is None when no row exists yet.
    """
    if old is None:
        return new, new_purge_after
    rank_old = _RETENTION_RANK.get(old, 1)
    rank_new = _RETENTION_RANK.get(new, 1)
    if rank_new > rank_old:
        return new, new_purge_after
    if rank_new < rank_old:
        return old, old_purge_after
    # Same rank: keep_n_days resolves by the later purge_after; ties keep the
    # stored row untouched. Unknown values keep the stored row.
    if old == "keep_n_days":
        if new_purge_after and (old_purge_after is None or new_purge_after > old_purge_after):
            return new, new_purge_after
        return old, old_purge_after
    return old, old_purge_after


class AssetsRepo(Protocol):
    def upsert(
        self,
        *,
        sha256: str,
        rel_path: str,
        kind: str,
        mime: str,
        size_bytes: int,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
        retention: str,
        purge_after: str | None = None,
    ) -> AssetRow: ...

    def get(self, sha256: str) -> AssetRow | None: ...

    def mark_purged(self, sha256: str) -> bool: ...

    def exists_live(self, sha256: str) -> bool: ...


class SqliteAssetsRepo(SqliteRepo):
    def upsert(
        self,
        *,
        sha256: str,
        rel_path: str,
        kind: str,
        mime: str,
        size_bytes: int,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
        retention: str,
        purge_after: str | None = None,
    ) -> AssetRow:
        if retention == "keep_n_days" and purge_after is None:
            raise ConfigError("retention 'keep_n_days' requires a purge_after timestamp")
        now = self._now()
        conn = self.db.connect()
        existing = conn.execute("SELECT * FROM assets WHERE sha256 = ?", (sha256,)).fetchone()
        if existing is not None:
            old_retention = str(existing["retention"])
            old_purge = existing["purge_after"] if "purge_after" in existing.keys() else None
            retention, purge_after = _retention_winner(
                old_retention, old_purge, retention, purge_after
            )
        conn.execute(
            """
            INSERT INTO assets(sha256, rel_path, kind, mime, size_bytes, duration_ms,
                               width, height, retention, created_at, purged_at, purge_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                retention = excluded.retention,
                purge_after = excluded.purge_after,
                duration_ms = COALESCE(assets.duration_ms, excluded.duration_ms),
                width = COALESCE(assets.width, excluded.width),
                height = COALESCE(assets.height, excluded.height)
            """,
            (sha256, rel_path, kind, mime, size_bytes, duration_ms, width, height, retention,
             now, purge_after),
        )
        row = conn.execute("SELECT * FROM assets WHERE sha256 = ?", (sha256,)).fetchone()
        assert row is not None  # the row was just inserted or already existed
        return AssetRow.model_validate(row_to_dict(row))

    def get(self, sha256: str) -> AssetRow | None:
        row = (
            self.db.connect().execute("SELECT * FROM assets WHERE sha256 = ?", (sha256,)).fetchone()
        )
        return AssetRow.model_validate(row_to_dict(row)) if row else None

    def mark_purged(self, sha256: str) -> bool:
        now = self._now()
        cur = self.db.connect().execute(
            "UPDATE assets SET purged_at = ? WHERE sha256 = ? AND purged_at IS NULL",
            (now, sha256),
        )
        return cur.rowcount > 0

    def exists_live(self, sha256: str) -> bool:
        row = (
            self.db.connect()
            .execute("SELECT 1 FROM assets WHERE sha256 = ? AND purged_at IS NULL", (sha256,))
            .fetchone()
        )
        return row is not None
