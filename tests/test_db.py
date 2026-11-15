"""T-DB-* (BEGIN_PROMPT section I)."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from studio.db import Database, migrate

# --------------------------------------------------------------- T-DB-01


def test_migration_idempotent_and_connection_pragmas(tmp_path):
    path = tmp_path / "studio.db"
    db = Database(path)
    migrate(db)
    migrate(db)  # second run must be a no-op, not an error
    fresh = Database(path)
    conn = fresh.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


# --------------------------------------------------------------- T-DB-02


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.connect().execute(
            "INSERT INTO tracks(id, episode_id, aspect, created_at) VALUES ('t1', 'nope', '9:16', '2026-01-01T00:00:00.000Z')"
        )


def test_check_constraints_reject_bad_rows(db):
    conn = db.connect()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assets(sha256, rel_path, kind, mime, size_bytes, created_at) "
            "VALUES (?, 'ab/cd/x.png', 'image', 'image/png', 0, '2026-01-01T00:00:00.000Z')",
            ("a" * 64,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs(id, kind, payload_json, status, max_attempts, run_after, "
            "created_at, updated_at) VALUES ('j1', 'k', '{}', 'bogus', 1, 'x', 'x', 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pipeline_runs(id, profile, status, auto_approve, config_snapshot_json, "
            "created_at, updated_at) VALUES ('r1', 'sandbox', 'pending', 0, '{not json', 'x', 'x')"
        )


# --------------------------------------------------------------- T-DB-03


def test_eight_threads_claim_fifty_jobs_exactly_once(db, repos):
    for i in range(50):
        repos.jobs.enqueue(kind="advance_run", payload={"i": i}, dedupe_key=f"job:{i}")

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        local = []
        while True:
            row = repos.jobs.claim(name)
            if row is None:
                break
            local.append(row.id)
        with lock:
            claimed.extend(local)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, [f"w{n}" for n in range(8)]))

    assert len(claimed) == 50
    assert len(set(claimed)) == 50
    rows = db.connect().execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    assert {r["status"]: r["n"] for r in rows} == {"running": 50}


# --------------------------------------------------------------- T-DB-04


def test_dedupe_key_blocks_second_live_job_and_allows_reenqueue(db, repos):
    first = repos.jobs.enqueue(kind="demo", payload={"a": 1}, dedupe_key="dup:1")
    assert first is not None
    second = repos.jobs.enqueue(kind="demo", payload={"a": 1}, dedupe_key="dup:1")
    assert second is None  # idempotent enqueue
    # the partial unique index also blocks at the SQL level
    with pytest.raises(sqlite3.IntegrityError):
        db.connect().execute(
            "INSERT INTO jobs(id, kind, payload_json, dedupe_key, status, attempt, max_attempts, "
            "run_after, created_at, updated_at) VALUES ('raw1', 'demo', '{}', 'dup:1', 'queued', "
            "0, 3, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
        )
    repos.jobs.complete(first.id)
    third = repos.jobs.enqueue(kind="demo", payload={"a": 1}, dedupe_key="dup:1")
    assert third is not None


# --------------------------------------------------------------- T-DB-05


def test_writer_and_reader_threads_coexist_in_wal(db, repos):
    stop = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        try:
            for i in range(30):
                repos.jobs.enqueue(kind="demo", payload={"n": i}, dedupe_key=f"wal:{i}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            stop.set()

    def reader() -> None:
        try:
            while not stop.is_set():
                db.connect().execute("SELECT count(*) FROM jobs").fetchone()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    r.start()
    w.start()
    w.join(timeout=30)
    stop.set()
    r.join(timeout=30)
    assert errors == []
    count = db.connect().execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert count == 30


# --------------------------------------------------------------- T-DB-06


def test_global_global_budget_row_is_unique(db):
    conn = db.connect()
    conn.execute(
        "INSERT INTO budgets(id, scope, scope_ref, cap_musd, kill_switch_tripped, tripped_at, created_at, updated_at) "
        "VALUES ('b1', 'global', 'global', 1000, 0, NULL, 'x', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO budgets(id, scope, scope_ref, cap_musd, kill_switch_tripped, tripped_at, created_at, updated_at) "
            "VALUES ('b2', 'global', 'global', 2000, 0, NULL, 'x', 'x')"
        )
