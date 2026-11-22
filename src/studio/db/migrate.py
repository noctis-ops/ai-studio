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
# A migration that rebuilds a table the application cascades from (SQLite cannot
# alter a CHECK in place) must run with foreign keys disabled, otherwise
# `DROP TABLE episodes` fires ON DELETE CASCADE and silently wipes pipeline_runs
# and every child row below it. Such a file declares the requirement in a
# header directive, and migrate() verifies `PRAGMA foreign_key_check` is empty
# before it commits.
_FK_OFF = re.compile(r"^--\s*migrate:\s*foreign_keys\s*=\s*off\s*$", re.MULTILINE)


def _fk_violations(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]


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
        fk_off = _FK_OFF.search(script) is not None
        if fk_off and conn.in_transaction:
            raise StudioError(
                f"migration {version:03d} needs foreign_keys=OFF but a transaction is open"
            )
        if fk_off:
            conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN")
            for stmt in _statements(script):
                conn.execute(stmt)
            if fk_off:
                violations = _fk_violations(conn)
                if violations:
                    raise StudioError(
                        f"migration {version:03d} left foreign key violations: {violations[:5]}"
                    )
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, stamp),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            if fk_off:
                conn.execute("PRAGMA foreign_keys=ON")
        applied.append(version)
    return applied
