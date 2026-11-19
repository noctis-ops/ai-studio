"""Character reference sheets (Phase 4 §H).

Only one approved row may exist per (character_id, role) — enforced by a
partial unique index — and older approved rows are superseded in the same
transaction that approves new candidates.
"""

from __future__ import annotations

from typing import Any, Protocol

from studio.domain.models import CharacterSheetRow

from .base import SqliteRepo, row_to_dict


class SheetsRepo(Protocol):
    def get(self, sheet_id: str) -> CharacterSheetRow | None: ...

    def approved(
        self, series_id: str, character_id: str, role: str
    ) -> CharacterSheetRow | None: ...

    def approved_for_series(self, series_id: str) -> list[CharacterSheetRow]: ...

    def list_for_series(self, series_id: str) -> list[CharacterSheetRow]: ...

    def latest_version(self, character_id: str, role: str) -> int: ...

    def create(
        self,
        *,
        series_id: str,
        character_id: str,
        role: str,
        version: int,
        asset_id: str,
        spec_hash: str,
        nonce: str,
        status: str = "candidate",
    ) -> CharacterSheetRow: ...

    def reject(self, sheet_ids: list[str]) -> int:
        """Mark rows 'rejected' (never approved). Returns the count."""
        ...

    def approve_latest_candidates(self, series_id: str) -> list[CharacterSheetRow]:
        """Flip the newest candidate per (character_id, role) to approved and
        supersede previously approved rows. Returns the newly approved rows."""
        ...


class SqliteSheetsRepo(SqliteRepo):
    def get(self, sheet_id: str) -> CharacterSheetRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM character_sheets WHERE id = ?", (sheet_id,))
            .fetchone()
        )
        return CharacterSheetRow.model_validate(row_to_dict(row)) if row else None

    def approved(
        self, series_id: str, character_id: str, role: str
    ) -> CharacterSheetRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM character_sheets "
                "WHERE series_id = ? AND character_id = ? AND role = ? AND status = 'approved'",
                (series_id, character_id, role),
            )
            .fetchone()
        )
        return CharacterSheetRow.model_validate(row_to_dict(row)) if row else None

    def approved_for_series(self, series_id: str) -> list[CharacterSheetRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM character_sheets WHERE series_id = ? AND status = 'approved'",
                (series_id,),
            )
            .fetchall()
        )
        return [CharacterSheetRow.model_validate(row_to_dict(row)) for row in rows]

    def list_for_series(self, series_id: str) -> list[CharacterSheetRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM character_sheets WHERE series_id = ? "
                "ORDER BY character_id, role, version",
                (series_id,),
            )
            .fetchall()
        )
        return [CharacterSheetRow.model_validate(row_to_dict(row)) for row in rows]

    def latest_version(self, character_id: str, role: str) -> int:
        row = (
            self.db.connect()
            .execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM character_sheets "
                "WHERE character_id = ? AND role = ?",
                (character_id, role),
            )
            .fetchone()
        )
        return int(row["v"]) if row else 0

    def create(
        self,
        *,
        series_id: str,
        character_id: str,
        role: str,
        version: int,
        asset_id: str,
        spec_hash: str,
        nonce: str,
        status: str = "candidate",
    ) -> CharacterSheetRow:
        now = self._now()
        sheet_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO character_sheets(id, series_id, character_id, role, version,
                                         asset_id, status, spec_hash, nonce, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sheet_id,
                series_id,
                character_id,
                role,
                int(version),
                asset_id,
                status,
                spec_hash,
                nonce,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM character_sheets WHERE id = ?", (sheet_id,)
        ).fetchone()
        assert row is not None
        return CharacterSheetRow.model_validate(row_to_dict(row))

    def reject(self, sheet_ids: list[str]) -> int:
        if not sheet_ids:
            return 0
        placeholders = ",".join("?" * len(sheet_ids))
        conn = self.db.connect()
        cur = conn.execute(
            f"UPDATE character_sheets SET status = 'rejected' "  # noqa: S608
            f"WHERE id IN ({placeholders}) AND status IN ('candidate', 'approved')",
            tuple(sheet_ids),
        )
        return int(cur.rowcount or 0)

    def approve_latest_candidates(self, series_id: str) -> list[CharacterSheetRow]:
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, character_id, role FROM character_sheets
                WHERE series_id = ? AND status = 'candidate'
                ORDER BY character_id, role, version
                """,
                (series_id,),
            ).fetchall()
            # newest candidate per (character_id, role) wins
            newest: dict[tuple[str, str], Any] = {}
            for row in rows:
                newest[(str(row["character_id"]), str(row["role"]))] = row["id"]
            superseded_ids = [str(row["id"]) for row in rows if row["id"] not in newest.values()]
            if superseded_ids:
                conn.execute(
                    f"UPDATE character_sheets SET status = 'superseded' WHERE id IN "  # noqa: S608
                    f"({','.join('?' * len(superseded_ids))})",
                    tuple(superseded_ids),
                )
            approved: list[CharacterSheetRow] = []
            for (character_id, role), sheet_id in sorted(newest.items()):
                conn.execute(
                    "UPDATE character_sheets SET status = 'superseded' "
                    "WHERE character_id = ? AND role = ? AND status = 'approved' AND id <> ?",
                    (character_id, role, str(sheet_id)),
                )
                conn.execute(
                    "UPDATE character_sheets SET status = 'approved', "
                    "approved_at = ? WHERE id = ?",
                    (self._now(), str(sheet_id)),
                )
                approved_row = conn.execute(
                    "SELECT * FROM character_sheets WHERE id = ?", (str(sheet_id),)
                ).fetchone()
                assert approved_row is not None
                approved.append(CharacterSheetRow.model_validate(row_to_dict(approved_row)))
            conn.execute("COMMIT")
            return approved
        except Exception:
            conn.execute("ROLLBACK")
            raise
