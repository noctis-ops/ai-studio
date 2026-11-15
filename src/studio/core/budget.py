"""LedgerService: every method is exactly one transaction (BEGIN IMMEDIATE).

Budget model:
- each paid call reserves `est_cost_musd` into `cost_ledger` (one row per scope)
- completing settles: `release(est)` + `settle(actual)` per scope
- failing releases, and settles only when `settle_musd > 0`
- `held = SUM(amount_musd)` per scope (reserve +est, release -est, settle +actual)
- exceeding a cap trips that scope's kill switch; the trip is committed even
  though the call is refused
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from studio.config.settings import Settings
from studio.core.clock import Clock
from studio.core.ids import Ids, UlidIds
from studio.db.connection import Database
from studio.db.repos.audit import SqliteAuditRepo
from studio.db.repos.budgets import SqliteBudgetsRepo
from studio.db.repos.providers import NewCacheEntry, NewCall, SqliteProvidersRepo
from studio.domain.enums import BudgetScope, budget_scopes_for_call
from studio.domain.errors import BudgetExceeded, KillSwitchTripped, StudioError
from studio.domain.models import ProviderCallRow
from studio.domain.money import usd_to_musd


class LedgerService:
    def __init__(self, db: Database, clock: Clock, settings: Settings, ids: Ids | None = None):
        self.db = db
        self.clock = clock
        self.settings = settings
        self.ids: Ids = ids if ids is not None else UlidIds()
        self.providers = SqliteProvidersRepo(db, clock, self.ids)
        self.budgets = SqliteBudgetsRepo(db, clock, self.ids)
        self.audit = SqliteAuditRepo(db, clock, self.ids)

    # ---- scopes / defaults ------------------------------------------------

    def scopes_for_call(self, episode_id: str | None) -> list[tuple[BudgetScope, str]]:
        return budget_scopes_for_call(episode_id, self.clock.month_tag())

    def _default_cap(self, scope: BudgetScope) -> int | None:
        budgets = self.settings.budgets
        if scope is BudgetScope.EPISODE:
            return usd_to_musd(budgets.default_episode_cap_usd)
        if scope is BudgetScope.MONTH:
            return usd_to_musd(budgets.monthly_cap_usd)
        return usd_to_musd(budgets.global_cap_usd) if budgets.global_cap_usd is not None else None

    # ---- begin_call ---------------------------------------------------------

    def begin_call(
        self, call: NewCall, *, conn: sqlite3.Connection | None = None
    ) -> ProviderCallRow:
        """Atomic budget check + reservation. `conn` is accepted for tests only;
        production paths call with no argument and get one IMMEDIATE transaction."""
        if conn is not None:
            return self._begin_call_tx(call, conn, own_tx=False)
        own = self.db.connect()
        own.execute("BEGIN IMMEDIATE")
        try:
            inserted = self._begin_call_tx(call, own, own_tx=True)
        except BudgetExceeded:
            # The kill-switch trip was already committed inside (fail closed,
            # and the trip persists even though the call is refused).
            raise
        except Exception:
            own.execute("ROLLBACK")
            raise
        else:
            own.execute("COMMIT")
            return inserted

    def _begin_call_tx(
        self, call: NewCall, conn: sqlite3.Connection, *, own_tx: bool
    ) -> ProviderCallRow:
        scopes = self.scopes_for_call(call.episode_id)
        warn_pct = self.settings.budgets.warn_pct

        ensured = {
            (scope.value, ref): self.budgets.ensure(
                scope.value, ref, self._default_cap(scope), conn=conn
            )
            for scope, ref in scopes
        }

        # 1) kill switches first — a tripped scope refuses every paid call.
        for scope, ref in scopes:
            row = ensured[(scope.value, ref)]
            if row.kill_switch_tripped:
                held = self.budgets.held_musd(scope.value, ref, conn=conn)
                if own_tx:
                    conn.execute("COMMIT")
                raise KillSwitchTripped(
                    f"kill switch tripped for {scope.value}:{ref}; reset it to spend again",
                    scope=scope.value,
                    scope_ref=ref,
                    cap_musd=row.cap_musd,
                    held_musd=held,
                    requested_musd=call.est_cost_musd,
                )

        # 2) cap check — trip persists even though the call is refused.
        for scope, ref in scopes:
            row = ensured[(scope.value, ref)]
            cap = row.cap_musd
            if cap is None:
                continue
            held = self.budgets.held_musd(scope.value, ref, conn=conn)
            if held + call.est_cost_musd > cap:
                self.budgets.trip(scope.value, ref, conn=conn)
                self.audit.append(
                    actor="budget",
                    entity=f"{scope.value}:{ref}",
                    event="kill_switch_tripped",
                    payload={
                        "scope": scope.value,
                        "scope_ref": ref,
                        "cap_musd": cap,
                        "held_musd": held,
                        "requested_musd": call.est_cost_musd,
                        "reason": "budget_exceeded",
                    },
                    conn=conn,
                )
                if own_tx:
                    conn.execute("COMMIT")
                raise BudgetExceeded(
                    f"budget exceeded for {scope.value}:{ref}: "
                    f"held {held} + requested {call.est_cost_musd} > cap {cap} micro-USD",
                    scope=scope.value,
                    scope_ref=ref,
                    cap_musd=cap,
                    held_musd=held,
                    requested_musd=call.est_cost_musd,
                )

        # 3) reserve.
        inserted = self.providers.insert_call(call, conn=conn)
        for scope, ref in scopes:
            self.providers.ledger_append(
                scope=scope.value,
                scope_ref=ref,
                entry_type="reserve",
                amount_musd=call.est_cost_musd,
                call_id=inserted.id,
                conn=conn,
            )

        # 4) warn_pct crossing, at most one budget_warning per scope.
        if warn_pct > 0:
            for scope, ref in scopes:
                row = ensured[(scope.value, ref)]
                cap = row.cap_musd
                if cap is None or cap <= 0:
                    continue
                held = self.budgets.held_musd(scope.value, ref, conn=conn)
                if held * 100 >= cap * warn_pct and not self.audit.has_event(
                    "budget_warning", f"{scope.value}:{ref}", conn=conn
                ):
                    self.audit.append(
                        actor="budget",
                        entity=f"{scope.value}:{ref}",
                        event="budget_warning",
                        payload={
                            "scope": scope.value,
                            "scope_ref": ref,
                            "cap_musd": cap,
                            "held_musd": held,
                            "warn_pct": warn_pct,
                        },
                        conn=conn,
                    )
        return inserted

    # ---- complete_call / fail_call -------------------------------------------

    def complete_call(
        self,
        call_id: str,
        actual_musd: int,
        cache_rows: list[NewCacheEntry],
    ) -> None:
        conn = self.db.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            call = self.providers.get_call(call_id)
            if call is None:
                raise StudioError(f"unknown provider call: {call_id}")
            est = call.est_cost_musd
            scopes = self.providers.reserve_scopes_for_call(call_id)
            self.providers.set_terminal(
                call_id,
                status="completed",
                error_class=None,
                error_message=None,
                actual_cost_musd=actual_musd,
                conn=conn,
            )
            for scope, ref in scopes:
                self.providers.ledger_append(
                    scope=scope,
                    scope_ref=ref,
                    entry_type="release",
                    amount_musd=-est,
                    call_id=call_id,
                    conn=conn,
                )
                self.providers.ledger_append(
                    scope=scope,
                    scope_ref=ref,
                    entry_type="settle",
                    amount_musd=actual_musd,
                    call_id=call_id,
                    conn=conn,
                )
            self.providers.cache_put(cache_rows, conn=conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def fail_call(
        self,
        call_id: str,
        error_class: str,
        message: str,
        *,
        settle_musd: int = 0,
    ) -> None:
        conn = self.db.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            call = self.providers.get_call(call_id)
            if call is None:
                raise StudioError(f"unknown provider call: {call_id}")
            est = call.est_cost_musd
            scopes = self.providers.reserve_scopes_for_call(call_id)
            self.providers.set_terminal(
                call_id,
                status="failed",
                error_class=error_class,
                error_message=message[:1000],
                actual_cost_musd=settle_musd,
                conn=conn,
            )
            for scope, ref in scopes:
                self.providers.ledger_append(
                    scope=scope,
                    scope_ref=ref,
                    entry_type="release",
                    amount_musd=-est,
                    call_id=call_id,
                    conn=conn,
                )
                if settle_musd > 0:
                    self.providers.ledger_append(
                        scope=scope,
                        scope_ref=ref,
                        entry_type="settle",
                        amount_musd=settle_musd,
                        call_id=call_id,
                        conn=conn,
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---- kill switch ----------------------------------------------------------

    def reset_kill_switch(
        self,
        scope: str,
        scope_ref: str,
        new_cap_musd: int | None = None,
    ) -> None:
        if scope not in (s.value for s in BudgetScope):
            raise StudioError(f"unknown budget scope {scope!r}")
        conn = self.db.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            ok = self.budgets.reset_kill_switch(
                scope, scope_ref, new_cap_musd=new_cap_musd, conn=conn
            )
            if not ok:
                raise StudioError(f"no budget row for {scope}:{scope_ref}")
            self.audit.append(
                actor="operator",
                entity=f"{scope}:{scope_ref}",
                event="kill_switch_reset",
                payload={
                    "scope": scope,
                    "scope_ref": scope_ref,
                    "new_cap_musd": new_cap_musd,
                },
                conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def fmt_cap(cap_musd: int | None) -> str:
    if cap_musd is None:
        return "(no cap)"
    return f"${Decimal(cap_musd) / Decimal(1_000_000):.2f}"
