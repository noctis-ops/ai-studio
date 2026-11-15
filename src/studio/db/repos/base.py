"""Shared base for SQLite-backed repositories.

Queries are deliberately portable SQL so a Postgres migration stays possible;
the single exception is the job-claim UPDATE ... RETURNING in jobs.py.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from studio.core.clock import Clock
from studio.core.ids import Ids, UlidIds

from ..connection import Database


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class SqliteRepo:
    def __init__(self, db: Database, clock: Clock, ids: Ids | None = None) -> None:
        self.db = db
        self.clock = clock
        self.ids: Ids = ids if ids is not None else UlidIds()

    def _now(self) -> str:
        return self.clock.now_iso()

    def _new_id(self) -> str:
        return self.ids.new()
