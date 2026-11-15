"""Budget rows + kill switch state. `held` is derived from the signed ledger."""

from __future__ import annotations

import sqlite3
from typing import Protocol

from studio.domain.models import BudgetRow

from .base import SqliteRepo, row_to_dict


class BudgetsRepo(Protocol):
    def ensure(
        self,
        scope: str,
        scope_ref: str,
        cap_musd: int | None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> BudgetRow: ...

    def get(self, scope: str, scope_ref: str) -> BudgetRow | None: ...

    def held_musd(
        self, scope: str, scope_ref: str, *, conn: sqlite3.Connection | None = None
    ) -> int: ...

    def trip(
        self, scope: str, scope_ref: str, *, conn: sqlite3.Connection | None = None
    ) -> None: ...

    def reset_kill_switch(
        self,
        scope: str,
        scope_ref: str,
        *,
        new_cap_musd: int | None,
        conn: sqlite3.Connection | None = None,
    ) -> bool: ...

    def list_all(self) -> list[BudgetRow]: ...


class SqliteBudgetsRepo(SqliteRepo):
    def ensure(
        self,
        scope: str,
        scope_ref: str,
        cap_musd: int | None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> BudgetRow:
        target = conn if conn is not None else self.db.connect()
        now = self._now()
        target.execute(
            """
            INSERT OR IGNORE INTO budgets(id, scope, scope_ref, cap_musd, kill_switch_tripped,
                                          tripped_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, NULL, ?, ?)
            """,
            (self._new_id(), scope, scope_ref, cap_musd, now, now),
        )
        row = target.execute(
            "SELECT * FROM budgets WHERE scope = ? AND scope_ref = ?", (scope, scope_ref)
        ).fetchone()
        assert row is not None
        return BudgetRow.model_validate(row_to_dict(row))

    def get(self, scope: str, scope_ref: str) -> BudgetRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM budgets WHERE scope = ? AND scope_ref = ?", (scope, scope_ref))
            .fetchone()
        )
        return BudgetRow.model_validate(row_to_dict(row)) if row else None

    def held_musd(
        self, scope: str, scope_ref: str, *, conn: sqlite3.Connection | None = None
    ) -> int:
        target = conn if conn is not None else self.db.connect()
        row = target.execute(
            "SELECT COALESCE(SUM(amount_musd), 0) AS held FROM cost_ledger "
            "WHERE scope = ? AND scope_ref = ?",
            (scope, scope_ref),
        ).fetchone()
        return int(row["held"])

    def trip(self, scope: str, scope_ref: str, *, conn: sqlite3.Connection | None = None) -> None:
        target = conn if conn is not None else self.db.connect()
        now = self._now()
        target.execute(
            """
            UPDATE budgets SET kill_switch_tripped = 1, tripped_at = ?, updated_at = ?
            WHERE scope = ? AND scope_ref = ?
            """,
            (now, now, scope, scope_ref),
        )

    def reset_kill_switch(
        self,
        scope: str,
        scope_ref: str,
        *,
        new_cap_musd: int | None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        target = conn if conn is not None else self.db.connect()
        now = self._now()
        if new_cap_musd is not None:
            cur = target.execute(
                """
                UPDATE budgets SET kill_switch_tripped = 0, tripped_at = NULL,
                                   cap_musd = ?, updated_at = ?
                WHERE scope = ? AND scope_ref = ?
                """,
                (new_cap_musd, now, scope, scope_ref),
            )
        else:
            cur = target.execute(
                """
                UPDATE budgets SET kill_switch_tripped = 0, tripped_at = NULL, updated_at = ?
                WHERE scope = ? AND scope_ref = ?
                """,
                (now, scope, scope_ref),
            )
        return cur.rowcount > 0

    def list_all(self) -> list[BudgetRow]:
        rows = (
            self.db.connect().execute("SELECT * FROM budgets ORDER BY scope, scope_ref").fetchall()
        )
        return [BudgetRow.model_validate(row_to_dict(r)) for r in rows]
