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
    add_completion=False,
    help="AI Studio — Phase 2 (demo pipeline on mocks; operator-gated paid LLM subset).",
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


def _llm_registry_rows() -> list[tuple[str, str, str]]:
    """Phase 2 rows: both LLM entries must be verified (dates shown)."""
    from studio.config.registry import Per1mTokens

    home = _home()
    try:
        config = load_all(home)
    except StudioError as exc:
        return [("studio config", "FAIL", f"load_all({home}): {exc}"[:200])]
    rows: list[tuple[str, str, str]] = []
    expectations = (
        ("llm.creative", "anthropic", "claude-sonnet-4-5-20250929", "Anthropic LLM"),
        ("llm.light", "openai", "gpt-4o-mini", "OpenAI LLM"),
    )
    for key, provider_name, expected_model, label in expectations:
        entry = config.registry.models.get(key)
        if entry is None:
            rows.append((f"{label} (missing from registry)", "FAIL", key))
            continue
        verified_on = entry.pricing_verified_on
        verified_text = verified_on.isoformat() if verified_on else "never"
        name = f"{label} ({entry.model_id}, verified {verified_text})"
        problems: list[str] = []
        if entry.provider != provider_name:
            problems.append(f"provider={entry.provider!r}, expected {provider_name!r}")
        if entry.model_id != expected_model:
            problems.append(
                "model_id changed; operator must re-verify and re-approve"
            )
        if entry.needs_confirmation:
            problems.append("needs_confirmation is true")
        if verified_on is None:
            problems.append("pricing never verified")
        if isinstance(entry.pricing, Per1mTokens):
            price_detail = f"in=${entry.pricing.in_usd}/MTok out=${entry.pricing.out_usd}/MTok"
        else:
            price_detail = f"unit={entry.pricing.unit}"
        if problems:
            rows.append((name, "FAIL", "; ".join(problems)))
            continue
        licence = (
            "commercial use verified"
            if entry.commercial_use_verified
            else "commercial use NOT verified (final blocked)"
        )
        rows.append((name, "PASS", f"verified {verified_text} · {price_detail} · {licence}"))
    return rows


def _llm_live_rows() -> list[tuple[str, str, str]]:
    """Opt-in live 1-token ping per adapter (never run in CI)."""
    import asyncio

    from studio.core.pricing import actual_cost_musd
    from studio.providers.base import LLMRequest
    from studio.providers.factory import build_provider

    home = _home()
    try:
        config = load_all(home)
    except StudioError as exc:
        return [("live LLM ping", "FAIL", f"load_all({home}): {exc}"[:200])]
    rows: list[tuple[str, str, str]] = []
    for key in ("llm.creative", "llm.light"):
        entry = config.registry.models[key]
        adapter = build_provider(entry, settings=config.settings)
        req = LLMRequest(
            system="You reply with the single word: ok",
            prompt="ok",
            max_tokens=1,
            temperature=0.0,
        )
        label = f"{key} live ping ({entry.model_id})"
        try:
            result = asyncio.run(adapter.complete(req))
        except Exception as exc:
            rows.append((label, "FAIL", f"{type(exc).__name__}: {exc}"[:200]))
            continue
        spent = actual_cost_musd(entry, req, result)
        rows.append(
            (
                label,
                "PASS",
                f"in={result.tokens_in} out={result.tokens_out} · cost={spent} musd",
            )
        )
    return rows


@app.command()
def doctor(
    llm: bool = typer.Option(
        False,
        "--llm",
        help="Opt-in live 1-token ping per LLM adapter (never used in CI).",
    ),
) -> None:
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

    # Phase 2: both LLM registry entries must be verified (dates shown).
    checks.extend(_llm_registry_rows())
    if llm:
        checks.extend(_llm_live_rows())

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


@db_app.command("bible")
def db_bible(
    series: str | None = typer.Option(None, "--series", help="Filter by series id."),
) -> None:
    """Print Series Bible versions with sha256 prefixes."""
    _, db, _clock, _ids, repos = _connect()
    migrate(db)
    if series:
        series_ids = [series]
    else:
        rows = (
            db.connect()
            .execute("SELECT DISTINCT series_id FROM series_bible ORDER BY series_id")
            .fetchall()
        )
        series_ids = [str(row[0]) for row in rows]
    if not series_ids:
        console.print("no series bibles yet (run the text pipeline first)")
        return
    table = Table(title="series bibles")
    for column in ("series_id", "version", "sha256", "title", "created_by", "created_at"):
        table.add_column(column)
    for series_id in series_ids:
        bibles = repos.bible.list_versions(series_id)
        if series and not bibles:
            console.print(f"series {series_id}: no bible versions")
            return
        for bible in bibles:
            table.add_row(
                series_id,
                str(bible.version),
                bible.sha256[:12],
                bible.title,
                bible.created_by,
                bible.created_at,
            )
    console.print(table)


# ==================================================================
# run / worker / resume
# ==================================================================


@app.command()
def run(
    demo: bool = typer.Option(False, "--demo", help="Seed and run the demo pipeline."),
    production: bool = typer.Option(
        False,
        "--production",
        help=(
            "Seed a production episode and run the LLM-only subset (S1-S4) with real, "
            "paid LLM calls. Requires --allow-paid-llm; media stages are skipped."
        ),
    ),
    allow_paid_llm: bool = typer.Option(
        False,
        "--allow-paid-llm",
        help="Explicit operator consent for real (paid) LLM calls.",
    ),
    concept: str | None = typer.Option(None, "--concept", help="Episode concept."),
    series_id: str | None = typer.Option(
        None, "--series-id", help="Reuse an existing series (default: create a new one)."
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="sandbox | draft | final (default: sandbox for --demo, draft for --production)",
    ),
    languages: str | None = typer.Option(None, "--languages", help="e.g. ar,en"),
    aspects: str | None = typer.Option(None, "--aspects", help="e.g. 9:16,16:9"),
    auto_approve: bool = typer.Option(
        False, "--auto-approve", help="Auto-approve gates (never for final)."
    ),
) -> None:
    """Create a run: --demo (mock sandbox) or --production (paid LLM subset)."""
    if demo and production:
        _fail("--demo and --production are mutually exclusive")
    if production:
        _run_production(
            allow_paid_llm=allow_paid_llm,
            concept=concept,
            series_id=series_id,
            profile=profile,
            languages=languages,
            aspects=aspects,
            auto_approve=auto_approve,
        )
        return
    if not demo:
        _fail("pass --demo (mock) or --production (paid LLM subset)")
    try:
        profile_enum = Profile((profile or "sandbox").strip().lower())
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


def _run_production(
    *,
    allow_paid_llm: bool,
    concept: str | None,
    series_id: str | None,
    profile: str | None,
    languages: str | None,
    aspects: str | None,
    auto_approve: bool,
) -> None:
    """LLM-only production path (S1-S4), real paid providers, operator-gated."""
    if not allow_paid_llm:
        _fail(
            "--production calls real, paid LLM providers; "
            "re-run with --allow-paid-llm to consent (zero calls are made otherwise)"
        )
    if concept is None or not concept.strip():
        _fail("--production requires --concept")
    try:
        profile_enum = Profile((profile or "draft").strip().lower())
    except ValueError:
        _fail(f"unknown profile {profile!r} (known: draft, final)")
    if profile_enum is Profile.SANDBOX:
        _fail("--production does not use the sandbox profile; for mocks use --demo")
    if auto_approve and profile_enum is Profile.FINAL:
        _fail("the final profile never auto-approves; human review is mandatory")
    language_list = _split_csv(languages) if languages else None
    aspect_list = _split_csv(aspects) if aspects else None

    config, db, _clock, ids, repos = _connect()
    migrate(db)
    try:
        resolved = config.registry.resolve_llm_subset(profile_enum.value)
    except RegistryError as exc:
        _fail(str(exc))
    if profile_enum is Profile.FINAL:
        try:
            config.registry.assert_exportable(resolved, audit=repos.audit)
        except StudioError as exc:
            _fail(str(exc))

    from studio.config.prompts import load_prompt
    from studio.production import project_llm_cost_musd, seed_production

    director_text, director_sha = load_prompt("director_prompt", home=_home())
    pack_cap_chars = config.settings.text_pipeline.context_token_budget * 2
    n_tracks = len(aspect_list) if aspect_list else len(config.settings.aspects)
    n_languages = len(language_list) if language_list else len(config.settings.languages)
    cost_rows = project_llm_cost_musd(
        config,
        resolved,
        n_tracks=n_tracks,
        n_deliverables=n_tracks * n_languages,
        director_chars=len(director_text),
        pack_cap_chars=pack_cap_chars,
        tashkeel_retries=config.settings.text_pipeline.tashkeel_retry,
    )
    total = sum(musd for _label, musd in cost_rows)

    console.print(
        "[bold]projected worst-case LLM cost[/bold] "
        "(pack at the token-budget cap; real cost is usually far lower — "
        "actuals are reconciled from provider usage):"
    )
    for label, musd in cost_rows:
        console.print(f"  {label:48s} {musd_to_usd(musd)} USD")
    console.print(f"  {'TOTAL':48s} {musd_to_usd(total)} USD")

    try:
        seeded = seed_production(
            config,
            repos,
            concept=concept,
            series_id=series_id,
            languages=language_list,
            aspects=aspect_list,
            ids=ids,
        )
    except StudioError as exc:
        _fail(str(exc))
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
        kind="advance_run",
        payload={"run_id": run_row.id},
        dedupe_key=f"advance:{run_row.id}",
    )
    llm_map = ", ".join(f"{role}={key}" for role, key in sorted(resolved.model_keys.items()))
    console.print(f"run id:       {run_row.id}")
    console.print(
        f"series:       {seeded.series_id}"
        + (" (new)" if seeded.series_created else " (reused)")
    )
    console.print(f"episode:      {seeded.episode_id}")
    console.print(f"profile:      {profile_enum.value} (LLM subset: {llm_map})")
    console.print(f"tracks:       {', '.join(seeded.track_ids)}")
    console.print(f"deliverables: {len(seeded.deliverable_ids)}")
    console.print(f"director:     sha256 {director_sha[:16]}")
    console.print(
        "[yellow]media stages are SKIPPED: TTS/image/video are not yet verified in "
        "model_registry.yaml (Phase 4). The run executes S1-S4 only and parks at G1.[/yellow]"
    )
    console.print("drive it:     studio worker --until-idle --production")
    console.print("approve G1:   studio gates approve <gate-id>   (then re-run the worker)")


@app.command()
def worker(
    until_idle: bool = typer.Option(False, "--until-idle", help="Stop when the queue is drained."),
    max_jobs: int | None = typer.Option(None, "--max-jobs", help="Execute at most N jobs."),
    production: bool = typer.Option(
        False, "--production", help="Drive the LLM-only text pipeline (S1-S4)."
    ),
) -> None:
    """Run the worker loop."""
    config, db, clock, ids, repos = _connect()
    migrate(db)
    dag = Dag(_pipeline_specs(production))
    engine = Engine(
        db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids,
        llm_subset_only=production,
    )
    instance = Worker(db, engine, repos, config, clock)

    jobs = _run_async(instance.run(until_idle=until_idle, max_jobs=max_jobs))
    console.print(f"jobs executed: {jobs}")


@app.command()
def resume(
    run_id: str = typer.Argument(...),
    production: bool = typer.Option(
        False, "--production", help="Resume a text-pipeline (S1-S4) run."
    ),
) -> None:
    """Resume a halted_budget or failed run."""
    config, db, clock, ids, repos = _connect()
    dag = Dag(_pipeline_specs(production))
    engine = Engine(
        db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids,
        llm_subset_only=production,
    )
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
    dag = _dag_for_pending(repos, run_id)
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
    dag = _dag_for_gate(repos, gate_id)
    service = GateService(_, repos, clock, dag, ids)
    try:
        service.approve(gate_id, note)
    except StudioError as exc:
        _fail(str(exc))
    _hint = " --production" if _is_text_gate(repos, gate_id) else ""
    console.print(f"gate {gate_id} approved; next: studio worker --until-idle{_hint}")


@gates_app.command("reject")
def gates_reject(
    gate_id: str = typer.Argument(...),
    shot: str = typer.Option(..., "--shot", help="Shot id to reject."),
    prompt: str | None = typer.Option(None, "--prompt", help="Edited prompt for the next attempt."),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Reject a shot's output at a gate; the stage re-executes."""
    _, db, clock, ids, repos = _connect()
    dag = _dag_for_gate(repos, gate_id)
    service = GateService(db, repos, clock, dag, ids)
    try:
        service.request_changes(gate_id, note, [ShotRejection(shot_id=shot, edited_prompt=prompt)])
    except StudioError as exc:
        _fail(str(exc))
    _hint = " --production" if _is_text_gate(repos, gate_id) else ""
    console.print(
        f"gate {gate_id} marked changes_requested; next: studio worker --until-idle{_hint}"
    )


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


_TEXT_STAGES = frozenset({"bible_upsert", "narration_script", "shot_plan", "metadata_safety"})


def _pipeline_specs(production: bool) -> list[StageSpec]:
    if production:
        from studio.pipeline.stages_text import text_specs

        return text_specs()
    return _demo_specs_light()


def _dag_for_run(repos: StudioRepos, run_id: str) -> Dag:
    """Pick the text DAG when the run contains text stages, else the demo DAG."""
    try:
        rows = repos.pipeline.list_stage_runs(run_id)
    except Exception:  # diagnostics only; never block the operator
        rows = []
    if any(row.stage in _TEXT_STAGES for row in rows):
        from studio.pipeline.stages_text import text_specs

        return Dag(text_specs())
    return Dag(_demo_specs_light())


def _is_text_gate(repos: StudioRepos, gate_id: str) -> bool:
    gate = repos.gates.get(gate_id)
    return gate is not None and gate.stage in _TEXT_STAGES


def _dag_for_gate(repos: StudioRepos, gate_id: str) -> Dag:
    gate = repos.gates.get(gate_id)
    if gate is not None:
        return _dag_for_run(repos, gate.run_id)
    return Dag(_demo_specs_light())


def _dag_for_pending(repos: StudioRepos, run_id: str | None) -> Dag:
    if run_id:
        return _dag_for_run(repos, run_id)
    try:
        pending = repos.gates.list_pending(None)
    except Exception:
        pending = []
    if any(gate.stage in _TEXT_STAGES for gate in pending):
        from studio.pipeline.stages_text import text_specs

        return Dag(text_specs())
    return Dag(_demo_specs_light())


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":
    main()
