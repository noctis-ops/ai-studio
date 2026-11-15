"""Thread-local SQLite connections with explicit transaction control.

Rules that matter (see BEGIN_PROMPT §D/§H3):
- `isolation_level=None`: the application decides when a transaction begins/ends.
- WAL + foreign_keys + busy_timeout + synchronous=NORMAL on EVERY connection.
- A transaction begins and ends inside one function on one thread.
- Only the job-claim UPDATE ... RETURNING is SQLite-specific
  (with `BEGIN IMMEDIATE` for cross-thread exclusion).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIN_SQLITE = (3, 35, 0)


def sqlite_supported() -> tuple[bool, str]:
    version = sqlite3.sqlite_version_info
    ok = version >= MIN_SQLITE
    return ok, f"{sqlite3.sqlite_version} (need >= {'.'.join(str(p) for p in MIN_SQLITE)})"


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._local.conn = conn
        return conn

    @contextmanager
    def tx(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Explicit transaction. `Exception` rolls back; BaseException
        (SimulatedCrash) propagates untouched, matching a killed process —
        and crash points are always placed between transactions by design."""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    async def arun(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous repository function on a worker thread."""

        return await asyncio.to_thread(fn, *args, **kwargs)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
