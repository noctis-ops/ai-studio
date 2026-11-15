"""`studio` command-line interface (Typer)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from studio.config.loader import AppConfig, load_all
from studio.core.clock import SystemClock
from studio.core.ids import UlidIds
from studio.db import build_repos, migrate
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.domain.enums import BudgetScope, Profile
from studio.domain.errors import RegistryError, StudioError
from studio.domain.money import musd_to_usd, usd_to_musd
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService, ShotRejection
from studio.pipeline.stage import StageSpec
from studio.pipeline.worker import Worker

app = typer.Typer(
    add_completion=False, help="AI Studio — Phase 1 (mock-only, no real provider calls)."
)
db_app = typer.Typer(help="Database commands.")
gates_app = typer.Typer(help="Human approval gates.")
budget_app = typer.Typer(help="Budget ledger.")
app.add_typer(db_app, name="db")
app.add_typer(gates_app, name="gates")
app.add_typer(budget_app, name="budget")

console = Console()
err_console = Console(stderr=True)


def _home() -> Path:
    return Path(os.environ.get("STUDIO_HOME", ".")).resolve()


def _connect() -> tuple[AppConfig, Database, SystemClock, UlidIds, StudioRepos]:
    home = _home()
    config = load_all(home)
    db_path = Path(config.settings.paths.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    clock = SystemClock()
    ids = UlidIds()
    repos = build_repos(db, clock, ids)
    return config, db, clock, ids, repos


T = TypeVar("T")


def _run_async(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _fail(message: str) -> NoReturn:
    err_console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=1)


# ==================================================================
# init / doctor / db
# ==================================================================


@app.command()
def init() -> None:
    """Create the home layout and run migrations."""
    home = _home()
    for rel in ("data", "data/cas", "data/exports", "data/projects", "data/mock"):
        (home / rel).mkdir(parents=True, exist_ok=True)
    config = load_all(home)
    db_path = Path(config.settings.paths.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    applied = migrate(db)
    console.print(f"initialized {home}")
    console.print(f"migrations applied: {applied or 'already up to date'}")


@app.command()
def doctor() -> None:
    """Environment checks. NEVER fails on the NVENC probe."""
    checks: list[tuple[str, str, str]] = []  # name, status, detail

    version = sys.version_info
    py_ok = version >= (3, 11)
    checks.append(
        (
            "python >= 3.11",
            "PASS" if py_ok else "FAIL",
            f"{version.major}.{version.minor}.{version.micro}",
        )
    )

    sqlite_version = sqlite3.sqlite_version
    try:
        major, minor = (int(part) for part in sqlite_version.split(".")[:2])
        sqlite_ok = (major, minor) >= (3, 35)
    except ValueError:
        sqlite_ok = False
    checks.append(("sqlite >= 3.35", "PASS" if sqlite_ok else "FAIL", sqlite_version))

    def _which(binary: str) -> str | None:
        from studio.video.ffmpeg import find_binary

        try:
            return find_binary(binary)
        except Exception:
            return None

    ffmpeg = _which("ffmpeg")
    checks.append(("ffmpeg on PATH", "PASS" if ffmpeg else "FAIL", ffmpeg or "not found"))
    ffprobe = _which("ffprobe")
    checks.append(("ffprobe on PATH", "PASS" if ffprobe else "FAIL", ffprobe or "not found"))

    # Informational NVENC probe: 1-frame h264_nvenc encode. WARN only.
    nvenc = "WARN"
    detail = "skipped (ffmpeg missing)"
    if ffmpeg:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64",
                    "-frames:v",
                    "1",
                    "-c:v",
                    "h264_nvenc",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0:
                nvenc, detail = "PASS", "h264_nvenc encode OK"
            else:
                detail = (
                    proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:160]
                    or "encode failed"
                )
        except Exception as exc:  # pragma: no cover - informational only
            detail = f"probe failed: {exc}"
    checks.append(("NVENC h264 probe (informational)", nvenc, detail))

    home = _home()
    try:
        probe_dir = home / "data"
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_file = probe_dir / ".doctor-write-test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        checks.append((f"write access to {probe_dir}", "PASS", str(probe_dir)))
    except OSError as exc:
        checks.append((f"write access to {home / 'data'}", "FAIL", str(exc)))

    table = Table(title="studio doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    failures = 0
    for name, status, detail in checks:
        style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[status]
        table.add_row(name, f"[{style}]{status}[/{style}]", detail)
        if status == "FAIL":
            failures += 1
    console.print(table)
    if failures:
        raise typer.Exit(code=1)


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending SQL migrations."""
    home = _home()
    config = load_all(home)
    db = Database(Path(config.settings.paths.db))
    applied = migrate(db)
    console.print(f"migrations applied: {applied or 'already up to date'}")


# ==================================================================
# run / worker / resume
# ==================================================================


@app.command()
def run(
    demo: bool = typer.Option(False, "--demo", help="Seed and run the demo pipeline."),
    profile: str = typer.Option("sandbox", "--profile", help="sandbox | draft | final"),
    languages: str | None = typer.Option(None, "--languages", help="e.g. ar,en"),
    aspects: str | None = typer.Option(None, "--aspects", help="e.g. 9:16,16:9"),
    auto_approve: bool = typer.Option(
        False, "--auto-approve", help="Auto-approve gates (never for final)."
    ),
) -> None:
    """Create a run. Only --demo is supported in Phase 1."""
    if not demo:
        _fail("only --demo is supported in Phase 1")
    try:
        profile_enum = Profile(profile.strip().lower())
    except ValueError:
        _fail(f"unknown profile {profile!r} (known: sandbox, draft, final)")
    if auto_approve and profile_enum is Profile.FINAL:
        _fail("the final profile never auto-approves; human review is mandatory")

    language_list = _split_csv(languages) if languages else None
    aspect_list = _split_csv(aspects) if aspects else None

    config, db, _clock, ids, repos = _connect()
    migrate(db)

    from studio.demo.seed import seed_demo

    try:
        resolved = config.registry.resolve_profile(profile_enum.value)
    except RegistryError as exc:
        _fail(str(exc))
    if profile_enum is Profile.FINAL:
        try:
            config.registry.assert_exportable(resolved, audit=repos.audit)
        except StudioError as exc:
            _fail(str(exc))

    seeded = seed_demo(config, repos, languages=language_list, aspects=aspect_list, ids=ids)
    snapshot = config.registry.registry_snapshot(resolved)
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id,
        profile=profile_enum.value,
        auto_approve=auto_approve,
        config_snapshot_json=json.dumps(
            snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ),
    )
    repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": run_row.id}, dedupe_key=f"advance:{run_row.id}"
    )
    console.print(f"run id:    {run_row.id}")
    console.print(f"episode:   {seeded.episode_id}")
    console.print(f"profile:   {profile_enum.value}")
    console.print(f"tracks:    {', '.join(seeded.track_ids)}")
    console.print(f"deliverables: {len(seeded.deliverable_ids)}")
    console.print("next: studio worker --until-idle")


@app.command()
def worker(
    until_idle: bool = typer.Option(False, "--until-idle", help="Stop when the queue is drained."),
    max_jobs: int | None = typer.Option(None, "--max-jobs", help="Execute at most N jobs."),
) -> None:
    """Run the worker loop."""
    config, db, clock, ids, repos = _connect()
    migrate(db)
    from studio.demo.stages import demo_specs

    dag = Dag(demo_specs())
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids)
    instance = Worker(db, engine, repos, config, clock)

    jobs = _run_async(instance.run(until_idle=until_idle, max_jobs=max_jobs))
    console.print(f"jobs executed: {jobs}")


@app.command()
def resume(run_id: str = typer.Argument(...)) -> None:
    """Resume a halted_budget or failed run."""
    config, db, clock, ids, repos = _connect()
    from studio.demo.stages import demo_specs

    dag = Dag(demo_specs())
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids)
    try:
        engine.resume_run(run_id)
    except StudioError as exc:
        _fail(str(exc))
    console.print(f"run {run_id} resumed; next: studio worker --until-idle")


# ==================================================================
# gates
# ==================================================================


@gates_app.command("list")
def gates_list(run_id: str | None = typer.Option(None, "--run", help="Filter by run id.")) -> None:
    """List pending gates."""
    _, db, clock, ids, repos = _connect()
    dag = Dag(_demo_specs_light())
    service = GateService(db, repos, clock, dag, ids)
    pending = service.list_pending(run_id)
    if not pending:
        console.print("no pending gates")
        return
    table = Table(title="pending gates")
    for column in ("id", "run_id", "stage", "gate", "round", "created_at"):
        table.add_column(column)
    for gate in pending:
        table.add_row(gate.id, gate.run_id, gate.stage, gate.gate, str(gate.round), gate.created_at)
    console.print(table)


@gates_app.command("approve")
def gates_approve(
    gate_id: str = typer.Argument(...),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Approve a pending gate; the run continues."""
    _, _, clock, ids, repos = _connect()
    dag = Dag(_demo_specs_light())
    service = GateService(_, repos, clock, dag, ids)
    try:
        service.approve(gate_id, note)
    except StudioError as exc:
        _fail(str(exc))
    console.print(f"gate {gate_id} approved; next: studio worker --until-idle")


@gates_app.command("reject")
def gates_reject(
    gate_id: str = typer.Argument(...),
    shot: str = typer.Option(..., "--shot", help="Shot id to reject."),
    prompt: str | None = typer.Option(None, "--prompt", help="Edited prompt for the next attempt."),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Reject a shot's output at a gate; the stage re-executes."""
    _, db, clock, ids, repos = _connect()
    dag = Dag(_demo_specs_light())
    service = GateService(db, repos, clock, dag, ids)
    try:
        service.request_changes(gate_id, note, [ShotRejection(shot_id=shot, edited_prompt=prompt)])
    except StudioError as exc:
        _fail(str(exc))
    console.print(f"gate {gate_id} marked changes_requested; next: studio worker --until-idle")


# ==================================================================
# budget
# ==================================================================


@budget_app.command("status")
def budget_status() -> None:
    """Show all budget rows with hold and kill-switch state."""
    _, _, _clock, _, repos = _connect()
    rows = repos.budgets.list_all()
    if not rows:
        console.print("no budget rows yet")
        return
    table = Table(title="budget status")
    for column in ("scope", "scope_ref", "cap_usd", "held_usd", "kill_switch"):
        table.add_column(column)
    for row in rows:
        held = repos.budgets.held_musd(row.scope, row.scope_ref)
        cap = musd_to_usd(row.cap_musd) if row.cap_musd is not None else "∞"
        table.add_row(
            row.scope,
            row.scope_ref,
            str(cap),
            str(musd_to_usd(held)),
            "TRIPPED" if row.kill_switch_tripped else "ok",
        )
    console.print(table)


@budget_app.command("reset")
def budget_reset(
    scope: str = typer.Argument(..., help="episode | month | global"),
    ref: str = typer.Argument(..., help="episode id, YYYY-MM, or 'global'"),
    cap: float | None = typer.Option(None, "--cap", help="New cap in USD."),
) -> None:
    """Reset a tripped kill switch (optionally raising the cap)."""
    config, db, clock, ids, _repos = _connect()
    from studio.core.budget import LedgerService

    try:
        scope_enum = BudgetScope(scope.strip().lower())
    except ValueError:
        _fail(f"unknown scope {scope!r}")
    new_cap = usd_to_musd(str(cap)) if cap is not None else None
    ledger = LedgerService(db, clock, config.settings, ids)
    try:
        ledger.reset_kill_switch(scope_enum.value, ref, new_cap)
    except StudioError as exc:
        _fail(str(exc))
    console.print(f"kill switch reset for {scope_enum.value}:{ref}")


# ==================================================================
# helpers
# ==================================================================


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _demo_specs_light() -> list[StageSpec]:
    from studio.demo.stages import demo_specs

    return demo_specs()


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":
    main()
