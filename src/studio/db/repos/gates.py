"""Approval gates. Unconstrained by subject uniqueness: multiple rows per
(run, gate, subject) are *rounds*; the latest row is the active one."""

from __future__ import annotations

from typing import Protocol

from studio.domain.models import GateRow

from .base import SqliteRepo, row_to_dict


class GatesRepo(Protocol):
    def create(
        self,
        *,
        run_id: str,
        gate: str,
        stage: str,
        scope: str | None,
        subject_id: str | None,
        round: int,
        snapshot_hash: str | None,
        presented_json: str | None = None,
    ) -> GateRow: ...

    def get(self, gate_id: str) -> GateRow | None: ...

    def latest(self, run_id: str, stage: str) -> GateRow | None: ...

    def count_rounds(self, run_id: str, stage: str) -> int: ...

    def decide(
        self,
        gate_id: str,
        *,
        status: str,
        note: str | None,
        checklist_json: str | None = None,
    ) -> GateRow | None: ...

    def list_pending(self, run_id: str | None = None) -> list[GateRow]: ...

    def list_for_run(self, run_id: str) -> list[GateRow]: ...


class SqliteGatesRepo(SqliteRepo):
    def create(
        self,
        *,
        run_id: str,
        gate: str,
        stage: str,
        scope: str | None,
        subject_id: str | None,
        round: int,
        snapshot_hash: str | None,
        presented_json: str | None = None,
    ) -> GateRow:
        now = self._now()
        gate_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO approval_gates(id, run_id, gate, stage, scope, subject_id,
                                       round, status, snapshot_hash, note,
                                       created_at, decided_at, presented_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, NULL, ?)
            """,
            (
                gate_id,
                run_id,
                gate,
                stage,
                scope,
                subject_id,
                round,
                snapshot_hash,
                now,
                presented_json,
            ),
        )
        row = conn.execute("SELECT * FROM approval_gates WHERE id = ?", (gate_id,)).fetchone()
        assert row is not None
        return GateRow.model_validate(row_to_dict(row))

    def get(self, gate_id: str) -> GateRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM approval_gates WHERE id = ?", (gate_id,))
            .fetchone()
        )
        return GateRow.model_validate(row_to_dict(row)) if row else None

    def latest(self, run_id: str, stage: str) -> GateRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM approval_gates WHERE run_id = ? AND stage = ? "
                "ORDER BY round DESC, created_at DESC, id DESC LIMIT 1",
                (run_id, stage),
            )
            .fetchone()
        )
        return GateRow.model_validate(row_to_dict(row)) if row else None

    def count_rounds(self, run_id: str, stage: str) -> int:
        row = (
            self.db.connect()
            .execute(
                "SELECT COUNT(*) AS n FROM approval_gates WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            )
            .fetchone()
        )
        return int(row["n"])

    def decide(
        self,
        gate_id: str,
        *,
        status: str,
        note: str | None,
        checklist_json: str | None = None,
    ) -> GateRow | None:
        now = self._now()
        conn = self.db.connect()
        conn.execute(
            "UPDATE approval_gates SET status = ?, note = ?, decided_at = ?, "
            "checklist_json = COALESCE(?, checklist_json) WHERE id = ?",
            (status, note, now, checklist_json, gate_id),
        )
        return self.get(gate_id)

    def list_pending(self, run_id: str | None = None) -> list[GateRow]:
        if run_id is None:
            rows = (
                self.db.connect()
                .execute(
                    "SELECT * FROM approval_gates WHERE status = 'pending' ORDER BY created_at, id"
                )
                .fetchall()
            )
        else:
            rows = (
                self.db.connect()
                .execute(
                    "SELECT * FROM approval_gates WHERE status = 'pending' AND run_id = ? "
                    "ORDER BY created_at, id",
                    (run_id,),
                )
                .fetchall()
            )
        return [GateRow.model_validate(row_to_dict(r)) for r in rows]

    def list_for_run(self, run_id: str) -> list[GateRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM approval_gates WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            )
            .fetchall()
        )
        return [GateRow.model_validate(row_to_dict(r)) for r in rows]
