"""Series Bible repository (migration 003)."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Protocol

from studio.domain.errors import StudioError
from studio.domain.series_bible import BibleContent, SeriesBible

from .base import SqliteRepo, row_to_dict


class BibleRepo(Protocol):
    def create_version(
        self, series_id: str, content: BibleContent, *, created_by: str = "s1"
    ) -> tuple[SeriesBible, bool]: ...

    def latest(self, series_id: str) -> SeriesBible | None: ...

    def get(self, series_id: str, version: int) -> SeriesBible | None: ...

    def list_versions(self, series_id: str) -> list[SeriesBible]: ...


class SqliteBibleRepo(SqliteRepo):
    def create_version(
        self, series_id: str, content: BibleContent, *, created_by: str = "s1"
    ) -> tuple[SeriesBible, bool]:
        """Insert a new version unless the content is identical to `latest`.

        Returns (bible, created). Identical content (same sha256) is a no-op:
        the existing row is returned with created=False, so re-running S1 on
        unchanged inputs never mints empty versions.
        """
        candidate = SeriesBible.from_content(series_id, version=1, content=content)
        current = self.latest(series_id)
        if current is not None and current.sha256 == candidate.sha256:
            return current, False
        version = current.version + 1 if current is not None else 1
        bible = SeriesBible.from_content(series_id, version, content)
        created_at = self._now()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO series_bible(id, series_id, version, content_json, content_sha256,
                                     created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._new_id(),
                series_id,
                version,
                bible.content_json,
                bible.sha256,
                created_by,
                created_at,
            ),
        )
        return bible.model_copy(update={"created_by": created_by, "created_at": created_at}), True

    def latest(self, series_id: str) -> SeriesBible | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM series_bible WHERE series_id = ? ORDER BY version DESC LIMIT 1",
                (series_id,),
            )
            .fetchone()
        )
        return self._from_row(row) if row else None

    def get(self, series_id: str, version: int) -> SeriesBible | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM series_bible WHERE series_id = ? AND version = ?",
                (series_id, version),
            )
            .fetchone()
        )
        return self._from_row(row) if row else None

    def list_versions(self, series_id: str) -> list[SeriesBible]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM series_bible WHERE series_id = ? ORDER BY version ASC",
                (series_id,),
            )
            .fetchall()
        )
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SeriesBible:
        data = row_to_dict(row)
        bible = SeriesBible.from_row(
            series_id=str(data["series_id"]),
            version=int(data["version"]),
            content_json=str(data["content_json"]),
            content_sha256=str(data["content_sha256"]),
            created_by=str(data["created_by"]),
            created_at=str(data["created_at"]),
        )
        recomputed = hashlib.sha256(bible.content_json.encode("utf-8")).hexdigest()
        if recomputed != bible.sha256:
            raise StudioError(
                f"series_bible row v{bible.version} failed integrity check: "
                f"stored sha {bible.sha256[:12]} != recomputed {recomputed[:12]}"
            )
        return bible

