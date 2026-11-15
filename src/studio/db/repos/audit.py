"""Append-only audit trail."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Protocol

from studio.domain.models import AuditEventRow

from .base import SqliteRepo, row_to_dict


class AuditRepo(Protocol):
    def append(
        self,
        actor: str,
        entity: str,
        event: str,
        payload: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> AuditEventRow: ...

    def list(
        self, event: str | None = None, entity: str | None = None, limit: int = 200
    ) -> list[AuditEventRow]: ...

    def has_event(
        self, event: str, entity: str, *, conn: sqlite3.Connection | None = None
    ) -> bool: ...


class SqliteAuditRepo(SqliteRepo):
    def append(
        self,
        actor: str,
        entity: str,
        event: str,
        payload: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> AuditEventRow:
        target = conn if conn is not None else self.db.connect()
        now = self._now()
        event_id = self._new_id()
        payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        target.execute(
            "INSERT INTO audit_events(id, ts, actor, entity, event, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, now, actor, entity, event, payload_json),
        )
        row = target.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
        assert row is not None
        return AuditEventRow.model_validate(row_to_dict(row))

    def list(
        self, event: str | None = None, entity: str | None = None, limit: int = 200
    ) -> list[AuditEventRow]:
        query = "SELECT * FROM audit_events"
        clauses: list[str] = []
        params: list[object] = []
        if event is not None:
            clauses.append("event = ?")
            params.append(event)
        if entity is not None:
            clauses.append("entity = ?")
            params.append(entity)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts, id LIMIT ?"
        params.append(limit)
        rows = self.db.connect().execute(query, params).fetchall()
        return [AuditEventRow.model_validate(row_to_dict(r)) for r in rows]

    def has_event(self, event: str, entity: str, *, conn: sqlite3.Connection | None = None) -> bool:
        target = conn if conn is not None else self.db.connect()
        row = target.execute(
            "SELECT 1 FROM audit_events WHERE event = ? AND entity = ? LIMIT 1",
            (event, entity),
        ).fetchone()
        return row is not None
