"""Assets table repository."""

from __future__ import annotations

from typing import Protocol

from studio.domain.models import AssetRow

from .base import SqliteRepo, row_to_dict


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
    ) -> AssetRow:
        now = self._now()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO assets(sha256, rel_path, kind, mime, size_bytes, duration_ms,
                               width, height, retention, created_at, purged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(sha256) DO NOTHING
            """,
            (sha256, rel_path, kind, mime, size_bytes, duration_ms, width, height, retention, now),
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
