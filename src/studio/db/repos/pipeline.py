"""pipeline_runs and stage_runs repositories."""

from __future__ import annotations

from typing import Protocol

from studio.domain.enums import Scope
from studio.domain.models import PipelineRunRow, StageRunRow
from studio.domain.refs import SubjectRef, lineage_to_json

from .base import SqliteRepo, row_to_dict


class PipelineRepo(Protocol):
    def create_run(
        self,
        *,
        episode_id: str | None,
        profile: str,
        auto_approve: bool,
        config_snapshot_json: str | None,
    ) -> PipelineRunRow: ...

    def get_run(self, run_id: str) -> PipelineRunRow | None: ...

    def list_runs(self, status: str | None = None) -> list[PipelineRunRow]: ...

    def set_run_status(self, run_id: str, status: str) -> None: ...

    def create_stage_run(
        self, *, run_id: str, stage: str, subject: SubjectRef, status: str = "pending"
    ) -> StageRunRow: ...

    def get_stage_run(self, stage_run_id: str) -> StageRunRow | None: ...

    def get_stage_run_for(
        self, run_id: str, stage: str, subject: SubjectRef
    ) -> StageRunRow | None: ...

    def list_stage_runs(self, run_id: str, stage: str | None = None) -> list[StageRunRow]: ...

    def set_stage_run(
        self,
        stage_run_id: str,
        *,
        status: str | None = None,
        input_hash: str | None = None,
        output_json: str | None = None,
        clear_input_hash: bool = False,
    ) -> None: ...

    def mark_stale(self, stage_run_ids: list[str]) -> int: ...

    def mark_enumerated(self, run_id: str, stage: str) -> None: ...

    def is_enumerated(self, run_id: str, stage: str) -> bool: ...

    def list_running_stage_runs(self) -> list[StageRunRow]: ...


class SqlitePipelineRepo(SqliteRepo):
    def create_run(
        self,
        *,
        episode_id: str | None,
        profile: str,
        auto_approve: bool,
        config_snapshot_json: str | None,
    ) -> PipelineRunRow:
        now = self._now()
        run_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO pipeline_runs(id, episode_id, profile, status, auto_approve,
                                      config_snapshot_json, created_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (run_id, episode_id, profile, 1 if auto_approve else 0, config_snapshot_json, now, now),
        )
        row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
        assert row is not None
        return PipelineRunRow.model_validate(row_to_dict(row))

    def list_runs(self, status: str | None = None) -> list[PipelineRunRow]:
        if status is None:
            rows = (
                self.db.connect()
                .execute("SELECT * FROM pipeline_runs ORDER BY created_at, id")
                .fetchall()
            )
        else:
            rows = (
                self.db.connect()
                .execute(
                    "SELECT * FROM pipeline_runs WHERE status = ? ORDER BY created_at, id",
                    (status,),
                )
                .fetchall()
            )
        return [PipelineRunRow.model_validate(row_to_dict(r)) for r in rows]

    def get_run(self, run_id: str) -> PipelineRunRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,))
            .fetchone()
        )
        return PipelineRunRow.model_validate(row_to_dict(row)) if row else None

    def set_run_status(self, run_id: str, status: str) -> None:
        now = self._now()
        self.db.connect().execute(
            "UPDATE pipeline_runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, run_id),
        )

    def create_stage_run(
        self, *, run_id: str, stage: str, subject: SubjectRef, status: str = "pending"
    ) -> StageRunRow:
        import json

        now = self._now()
        lineage_json = json.dumps(lineage_to_json(subject.lineage), separators=(",", ":"))
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO stage_runs(id, run_id, stage, subject_scope, subject_id,
                                   lineage_json, status, input_hash, output_json,
                                   created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(run_id, stage, subject_scope, subject_id) DO NOTHING
            """,
            (
                self._new_id(),
                run_id,
                stage,
                subject.scope.value,
                subject.id,
                lineage_json,
                status,
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM stage_runs
            WHERE run_id = ? AND stage = ? AND subject_scope = ? AND subject_id = ?
            """,
            (run_id, stage, subject.scope.value, subject.id),
        ).fetchone()
        assert row is not None
        return StageRunRow.model_validate(row_to_dict(row))

    def get_stage_run(self, stage_run_id: str) -> StageRunRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM stage_runs WHERE id = ?", (stage_run_id,))
            .fetchone()
        )
        return StageRunRow.model_validate(row_to_dict(row)) if row else None

    def get_stage_run_for(self, run_id: str, stage: str, subject: SubjectRef) -> StageRunRow | None:
        row = (
            self.db.connect()
            .execute(
                """
            SELECT * FROM stage_runs
            WHERE run_id = ? AND stage = ? AND subject_scope = ? AND subject_id = ?
            """,
                (run_id, stage, subject.scope.value, subject.id),
            )
            .fetchone()
        )
        return StageRunRow.model_validate(row_to_dict(row)) if row else None

    def list_stage_runs(self, run_id: str, stage: str | None = None) -> list[StageRunRow]:
        if stage is None:
            rows = (
                self.db.connect()
                .execute(
                    "SELECT * FROM stage_runs WHERE run_id = ? ORDER BY created_at, id", (run_id,)
                )
                .fetchall()
            )
        else:
            rows = (
                self.db.connect()
                .execute(
                    "SELECT * FROM stage_runs WHERE run_id = ? AND stage = ? "
                    "ORDER BY created_at, id",
                    (run_id, stage),
                )
                .fetchall()
            )
        return [StageRunRow.model_validate(row_to_dict(r)) for r in rows]

    def set_stage_run(
        self,
        stage_run_id: str,
        *,
        status: str | None = None,
        input_hash: str | None = None,
        output_json: str | None = None,
        clear_input_hash: bool = False,
    ) -> None:
        row = self.get_stage_run(stage_run_id)
        if row is None:
            return
        new_status = status if status is not None else row.status
        if clear_input_hash:
            new_input_hash = None
        else:
            new_input_hash = input_hash if input_hash is not None else row.input_hash
        new_output = output_json if output_json is not None else row.output_json
        now = self._now()
        self.db.connect().execute(
            "UPDATE stage_runs SET status = ?, input_hash = ?, output_json = ?, updated_at = ? "
            "WHERE id = ?",
            (new_status, new_input_hash, new_output, now, stage_run_id),
        )

    def mark_stale(self, stage_run_ids: list[str]) -> int:
        if not stage_run_ids:
            return 0
        now = self._now()
        total = 0
        conn = self.db.connect()
        for stage_run_id in stage_run_ids:
            cur = conn.execute(
                "UPDATE stage_runs SET status = 'stale', updated_at = ? "
                "WHERE id = ? AND status IN ('succeeded','pending','running')",
                (now, stage_run_id),
            )
            total += cur.rowcount
        return total

    def mark_enumerated(self, run_id: str, stage: str) -> None:
        now = self._now()
        self.db.connect().execute(
            "INSERT OR IGNORE INTO run_stage_marks(run_id, stage, enumerated_at) VALUES (?, ?, ?)",
            (run_id, stage, now),
        )

    def is_enumerated(self, run_id: str, stage: str) -> bool:
        row = (
            self.db.connect()
            .execute(
                "SELECT 1 FROM run_stage_marks WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            )
            .fetchone()
        )
        return row is not None

    def list_running_stage_runs(self) -> list[StageRunRow]:
        rows = (
            self.db.connect()
            .execute("SELECT * FROM stage_runs WHERE status = 'running' ORDER BY created_at, id")
            .fetchall()
        )
        return [StageRunRow.model_validate(row_to_dict(r)) for r in rows]


def subject_from_stage_run(row: StageRunRow) -> SubjectRef:
    import json

    from studio.domain.refs import lineage_from_json

    return SubjectRef(
        scope=Scope(row.subject_scope),
        id=row.subject_id,
        lineage=lineage_from_json(json.loads(row.lineage_json)),
    )
