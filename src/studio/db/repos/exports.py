"""Export rows (migration 007): one row per (deliverable, approved render).

The row is what makes the export directory's *date* stable and its revisions
monotonic: the first export of a deliverable writes `YYYYMMDD_<slug>/`, a later
approved render writes `..._r2/`, and neither is ever overwritten (T-EXP-07).
"""

from __future__ import annotations

from typing import Protocol

from studio.domain.models import ExportRow

from .base import SqliteRepo, row_to_dict


class ExportsRepo(Protocol):
    def create(
        self,
        *,
        deliverable_id: str,
        render_asset_id: str,
        dir_path: str,
        profile: str,
        status: str = "written",
    ) -> ExportRow: ...

    def get(self, deliverable_id: str, render_asset_id: str) -> ExportRow | None: ...

    def list_for_deliverable(self, deliverable_id: str) -> list[ExportRow]: ...

    def latest(self, deliverable_id: str) -> ExportRow | None: ...


class SqliteExportsRepo(SqliteRepo):
    def create(
        self,
        *,
        deliverable_id: str,
        render_asset_id: str,
        dir_path: str,
        profile: str,
        status: str = "written",
    ) -> ExportRow:
        export_id = self._new_id()
        now = self._now()
        self.db.connect().execute(
            """
            INSERT INTO exports(id, deliverable_id, render_asset_id, dir_path,
                                profile, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deliverable_id, render_asset_id) DO UPDATE SET
              dir_path = excluded.dir_path,
              profile = excluded.profile,
              status = excluded.status
            """,
            (export_id, deliverable_id, render_asset_id, dir_path, profile, status, now),
        )
        row = self.get(deliverable_id, render_asset_id)
        assert row is not None
        return row

    def get(self, deliverable_id: str, render_asset_id: str) -> ExportRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM exports WHERE deliverable_id = ? AND render_asset_id = ?",
                (deliverable_id, render_asset_id),
            )
            .fetchone()
        )
        return ExportRow(**row_to_dict(row)) if row else None

    def list_for_deliverable(self, deliverable_id: str) -> list[ExportRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM exports WHERE deliverable_id = ? ORDER BY created_at, id",
                (deliverable_id,),
            )
            .fetchall()
        )
        return [ExportRow(**row_to_dict(row)) for row in rows]

    def latest(self, deliverable_id: str) -> ExportRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM exports WHERE deliverable_id = ? ORDER BY created_at DESC, id DESC "
                "LIMIT 1",
                (deliverable_id,),
            )
            .fetchone()
        )
        return ExportRow(**row_to_dict(row)) if row else None
