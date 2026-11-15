"""Numbered, idempotent SQL migrations recorded in `schema_migrations`."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from studio.core.clock import SystemClock, format_ts
from studio.domain.errors import ConfigError, StudioError

from .connection import Database, sqlite_supported

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{3,})_.*\.sql$")


def _statements(script: str) -> list[str]:
    """Split a migration into statements. Our migrations contain no semicolons
    inside string literals or triggers, so a plain split is safe and keeps the
    whole migration inside ONE transaction."""
    chunks = script.split(";")
    return [chunk for chunk in (c.strip() for c in chunks) if chunk]


def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def applied_versions(db: Database) -> set[int]:
    conn = db.connect()
    _ensure_meta(conn)
    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(db: Database, *, migrations_dir: Path | None = None) -> list[int]:
    ok, detail = sqlite_supported()
    if not ok:
        raise StudioError(f"SQLite {detail}; need >= 3.35 for RETURNING")
    directory = migrations_dir or MIGRATIONS_DIR
    files = sorted(p for p in directory.glob("*.sql") if _NAME.match(p.name))
    if not files:
        raise ConfigError(f"no migration files found in {directory}")
    conn = db.connect()
    _ensure_meta(conn)
    done = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
    applied: list[int] = []
    for path in files:
        match = _NAME.match(path.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in done:
            continue
        script = path.read_text(encoding="utf-8")
        stamp = format_ts(SystemClock().now())
        try:
            conn.execute("BEGIN")
            for stmt in _statements(script):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, stamp),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
