"""Job queue. The claim query is the Blueprint §5 shape extended with
`attempt = attempt + 1`; it is the only SQLite-specific statement we ship.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from studio.domain.models import JobRow

from .base import SqliteRepo, row_to_dict


class JobsRepo(Protocol):
    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        run_after: str | None = None,
        max_attempts: int | None = None,
        stage_run_id: str | None = None,
    ) -> JobRow | None: ...

    def claim(self, worker_id: str) -> JobRow | None: ...

    def heartbeat(self, job_id: str, worker_id: str | None = None) -> None: ...

    def complete(self, job_id: str) -> None: ...

    def requeue(self, job_id: str, run_after: str) -> None: ...

    def mark_failed(self, job_id: str) -> None: ...

    def get(self, job_id: str) -> JobRow | None: ...

    def stale_running(self, stale_before_iso: str) -> list[JobRow]: ...

    def running_stage_run_ids(self) -> set[str]: ...


class SqliteJobsRepo(SqliteRepo):
    DEFAULT_MAX_ATTEMPTS = 3

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        run_after: str | None = None,
        max_attempts: int | None = None,
        stage_run_id: str | None = None,
    ) -> JobRow | None:
        """Idempotent enqueue: returns None instead of raising when a live
        (queued/running) job already holds `dedupe_key`."""
        now = self._now()
        run_after = run_after or now
        payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        attempts = max_attempts if max_attempts is not None else self.DEFAULT_MAX_ATTEMPTS
        with self.db.tx(immediate=True) as conn:
            if dedupe_key is not None:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued','running')",
                    (dedupe_key,),
                ).fetchone()
                if row is not None:
                    return None
            job_id = self._new_id()
            conn.execute(
                """
                INSERT INTO jobs(id, kind, payload_json, dedupe_key, status, attempt,
                                 max_attempts, run_after, worker_id, heartbeat_at,
                                 stage_run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    payload_json,
                    dedupe_key,
                    attempts,
                    run_after,
                    stage_run_id,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row is not None
        return JobRow.model_validate(row_to_dict(row))

    def claim(self, worker_id: str) -> JobRow | None:
        now = self._now()
        with self.db.tx(immediate=True) as conn:
            row = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', worker_id = ?, heartbeat_at = ?,
                    attempt = attempt + 1, updated_at = ?
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'queued' AND run_after <= ?
                    ORDER BY created_at, id
                    LIMIT 1
                )
                RETURNING *
                """,
                (worker_id, now, now, now),
            ).fetchone()
        return JobRow.model_validate(row_to_dict(row)) if row else None

    def heartbeat(self, job_id: str, worker_id: str | None = None) -> None:
        now = self._now()
        self.db.connect().execute(
            "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (now, now, job_id),
        )

    def complete(self, job_id: str) -> None:
        self.db.connect().execute(
            "UPDATE jobs SET status = 'succeeded', updated_at = ? WHERE id = ?",
            (self._now(), job_id),
        )

    def requeue(self, job_id: str, run_after: str) -> None:
        self.db.connect().execute(
            """
            UPDATE jobs SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
                             run_after = ?, updated_at = ?
            WHERE id = ?
            """,
            (run_after, self._now(), job_id),
        )

    def mark_failed(self, job_id: str) -> None:
        self.db.connect().execute(
            "UPDATE jobs SET status = 'failed', updated_at = ? WHERE id = ?",
            (self._now(), job_id),
        )

    def get(self, job_id: str) -> JobRow | None:
        row = self.db.connect().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRow.model_validate(row_to_dict(row)) if row else None

    def stale_running(self, stale_before_iso: str) -> list[JobRow]:
        rows = (
            self.db.connect()
            .execute(
                """
            SELECT * FROM jobs
            WHERE status = 'running'
              AND (heartbeat_at IS NULL OR heartbeat_at < ?)
            """,
                (stale_before_iso,),
            )
            .fetchall()
        )
        return [JobRow.model_validate(row_to_dict(r)) for r in rows]

    def running_stage_run_ids(self) -> set[str]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT DISTINCT stage_run_id FROM jobs "
                "WHERE status = 'running' AND stage_run_id IS NOT NULL"
            )
            .fetchall()
        )
        return {str(r["stage_run_id"]) for r in rows}
