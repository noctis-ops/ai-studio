"""Provider calls, cache entries and the cost ledger append path.

Mutating methods accept an optional `conn` so multi-step operations (budget
reserve/settle, cache insert) can share ONE transaction with the ledger;
when omitted, statements run on the thread-local connection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from studio.domain.models import CacheEntryRow, LedgerEntryRow, ProviderCallRow

from .base import SqliteRepo, row_to_dict


@dataclass(frozen=True)
class NewCacheEntry:
    cache_key: str
    capability: str
    model_key: str
    assets_json: str  # canonical JSON list of sha256 hex strings
    inline_json: str | None
    cost_musd: int


@dataclass(frozen=True)
class NewCall:
    episode_id: str | None
    stage_run_id: str | None
    capability: str
    model_key: str
    provider: str
    purpose: str
    cache_key: str
    request_json: str | None
    est_cost_musd: int
    extra: dict[str, str] = field(default_factory=dict)


class ProvidersRepo(Protocol):
    def cache_get(self, cache_key: str) -> CacheEntryRow | None: ...

    def cache_find_live_hit(self, cache_key: str) -> CacheEntryRow | None: ...

    def cache_put(
        self, entries: list[NewCacheEntry], *, conn: sqlite3.Connection | None = None
    ) -> None: ...

    def find_inflight(self, cache_key: str) -> list[ProviderCallRow]: ...

    def insert_call(
        self, call: NewCall, *, conn: sqlite3.Connection | None = None
    ) -> ProviderCallRow: ...

    def get_call(self, call_id: str) -> ProviderCallRow | None: ...

    def mark_submit_started(
        self, call_id: str, *, conn: sqlite3.Connection | None = None
    ) -> None: ...

    def mark_submitted(
        self,
        call_id: str,
        remote_request_id: str,
        remote_handle_json: str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None: ...

    def set_remote_request_id(self, call_id: str, remote_request_id: str) -> None: ...

    def bump_attempt(self, call_id: str) -> None: ...

    def set_terminal(
        self,
        call_id: str,
        *,
        status: str,
        error_class: str | None,
        error_message: str | None,
        actual_cost_musd: int | None,
        conn: sqlite3.Connection | None = None,
    ) -> None: ...

    def reserve_scopes_for_call(self, call_id: str) -> list[tuple[str, str]]: ...

    def ledger_append(
        self,
        *,
        scope: str,
        scope_ref: str,
        entry_type: str,
        amount_musd: int,
        call_id: str | None,
        conn: sqlite3.Connection | None = None,
    ) -> None: ...

    def ledger_for_call(self, call_id: str) -> list[LedgerEntryRow]: ...


class SqliteProvidersRepo(SqliteRepo):
    # ---- cache -----------------------------------------------------------

    def cache_get(self, cache_key: str) -> CacheEntryRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM cache_entries WHERE cache_key = ?", (cache_key,))
            .fetchone()
        )
        return CacheEntryRow.model_validate(row_to_dict(row)) if row else None

    def cache_find_live_hit(self, cache_key: str) -> CacheEntryRow | None:
        # Existence only; the gateway additionally checks that referenced assets are live.
        return self.cache_get(cache_key)

    def cache_put(
        self, entries: list[NewCacheEntry], *, conn: sqlite3.Connection | None = None
    ) -> None:
        target = conn if conn is not None else self.db.connect()
        now = self._now()
        for entry in entries:
            target.execute(
                """
                INSERT INTO cache_entries(cache_key, capability, model_key, assets_json,
                                          inline_json, cost_musd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO NOTHING
                """,
                (
                    entry.cache_key,
                    entry.capability,
                    entry.model_key,
                    entry.assets_json,
                    entry.inline_json,
                    entry.cost_musd,
                    now,
                ),
            )

    # ---- calls -----------------------------------------------------------

    def find_inflight(self, cache_key: str) -> list[ProviderCallRow]:
        rows = (
            self.db.connect()
            .execute(
                """
            SELECT * FROM provider_calls
            WHERE cache_key = ? AND status IN ('reserved','submitted')
            ORDER BY created_at DESC, id DESC
            """,
                (cache_key,),
            )
            .fetchall()
        )
        return [ProviderCallRow.model_validate(row_to_dict(r)) for r in rows]

    def insert_call(
        self, call: NewCall, *, conn: sqlite3.Connection | None = None
    ) -> ProviderCallRow:
        target = conn if conn is not None else self.db.connect()
        now = self._now()
        call_id = self._new_id()
        target.execute(
            """
            INSERT INTO provider_calls(id, episode_id, stage_run_id, capability, model_key,
                                       provider, purpose, cache_key, request_json, status,
                                       attempt, est_cost_musd, actual_cost_musd,
                                       remote_request_id, submit_started_at,
                                       error_class, error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 0, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (
                call_id,
                call.episode_id,
                call.stage_run_id,
                call.capability,
                call.model_key,
                call.provider,
                call.purpose,
                call.cache_key,
                call.request_json,
                call.est_cost_musd,
                now,
                now,
            ),
        )
        row = target.execute("SELECT * FROM provider_calls WHERE id = ?", (call_id,)).fetchone()
        assert row is not None
        return ProviderCallRow.model_validate(row_to_dict(row))

    def get_call(self, call_id: str) -> ProviderCallRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM provider_calls WHERE id = ?", (call_id,))
            .fetchone()
        )
        return ProviderCallRow.model_validate(row_to_dict(row)) if row else None

    def mark_submit_started(self, call_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        target = conn if conn is not None else self.db.connect()
        target.execute(
            "UPDATE provider_calls SET submit_started_at = ?, updated_at = ? "
            "WHERE id = ? AND submit_started_at IS NULL",
            (self._now(), self._now(), call_id),
        )

    def mark_submitted(
        self,
        call_id: str,
        remote_request_id: str,
        remote_handle_json: str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        target = conn if conn is not None else self.db.connect()
        now = self._now()
        target.execute(
            "UPDATE provider_calls SET remote_request_id = ?, remote_handle_json = ?, "
            "status = 'submitted', updated_at = ? WHERE id = ?",
            (remote_request_id, remote_handle_json, now, call_id),
        )

    def bump_attempt(self, call_id: str) -> None:
        now = self._now()
        self.db.connect().execute(
            "UPDATE provider_calls SET attempt = attempt + 1, updated_at = ? WHERE id = ?",
            (now, call_id),
        )

    def set_remote_request_id(self, call_id: str, remote_request_id: str) -> None:
        now = self._now()
        self.db.connect().execute(
            "UPDATE provider_calls SET remote_request_id = ?, updated_at = ? "
            "WHERE id = ? AND remote_request_id IS NULL",
            (remote_request_id, now, call_id),
        )

    def set_terminal(
        self,
        call_id: str,
        *,
        status: str,
        error_class: str | None,
        error_message: str | None,
        actual_cost_musd: int | None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        target = conn if conn is not None else self.db.connect()
        target.execute(
            """
            UPDATE provider_calls
            SET status = ?, error_class = ?, error_message = ?, actual_cost_musd = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error_class, error_message, actual_cost_musd, self._now(), call_id),
        )

    # ---- ledger ----------------------------------------------------------

    def reserve_scopes_for_call(self, call_id: str) -> list[tuple[str, str]]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT DISTINCT scope, scope_ref FROM cost_ledger "
                "WHERE call_id = ? AND entry_type = 'reserve'",
                (call_id,),
            )
            .fetchall()
        )
        return [(str(r["scope"]), str(r["scope_ref"])) for r in rows]

    def ledger_append(
        self,
        *,
        scope: str,
        scope_ref: str,
        entry_type: str,
        amount_musd: int,
        call_id: str | None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        target = conn if conn is not None else self.db.connect()
        target.execute(
            """
            INSERT INTO cost_ledger(id, ts, scope, scope_ref, entry_type, amount_musd, call_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (self._new_id(), self._now(), scope, scope_ref, entry_type, amount_musd, call_id),
        )

    def ledger_for_call(self, call_id: str) -> list[LedgerEntryRow]:
        rows = (
            self.db.connect()
            .execute("SELECT * FROM cost_ledger WHERE call_id = ? ORDER BY ts, id", (call_id,))
            .fetchall()
        )
        return [LedgerEntryRow.model_validate(row_to_dict(r)) for r in rows]
