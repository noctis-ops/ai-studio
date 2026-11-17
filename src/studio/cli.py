"""`studio` command-line interface (Typer)."""

from __future__ import annotations

import asyncio
import hashlib
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
from studio.core.budget import LedgerService
from studio.core.clock import SystemClock, format_ts
from studio.core.ids import UlidIds
from studio.db import build_repos, migrate
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.domain.enums import BudgetScope, Language, Profile
from studio.domain.errors import RegistryError, StudioError
from studio.domain.money import musd_to_usd, usd_to_musd
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService, ShotRejection
from studio.pipeline.stage import StageSpec
from studio.pipeline.worker import Worker

app = typer.Typer(
    add_completion=False,
    help=(
        "AI Studio — Phase 3 (demo pipeline on mocks incl. the local audio engine; "
        "operator-gated paid LLM subset)."
    ),
)
db_app = typer.Typer(help="Database commands.")
gates_app = typer.Typer(help="Human approval gates.")
budget_app = typer.Typer(help="Budget ledger.")
app.add_typer(db_app, name="db")
app.add_typer(gates_app, name="gates")
app.add_typer(budget_app, name="budget")
voice_app = typer.Typer(help="Character voices (TTS).")
narration_app = typer.Typer(help="Narration lines (post-Gate-1 edits).")
library_app = typer.Typer(help="Audio library scan/list/check.")
tts_app = typer.Typer(help="TTS utilities (audition).")
audio_app = typer.Typer(help="Audio outputs: preview, QC report, calibrate.")
app.add_typer(voice_app, name="voice")
app.add_typer(narration_app, name="narration")
app.add_typer(library_app, name="library")
app.add_typer(tts_app, name="tts")
app.add_typer(audio_app, name="audio")

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

def _tts_registry_rows() -> list[tuple[str, str, str]]:
    """Phase 3 rows: every non-simulated TTS entry must be placeholder-free;
    an unverified price warns (the operator must confirm before `final`)."""
    home = _home()
    try:
        config = load_all(home)
    except StudioError as exc:
        return [("studio config", "FAIL", f"load_all({home}): {exc}"[:200])]
    rows: list[tuple[str, str, str]] = []
    for key, entry in sorted(config.registry.models.items()):
        if entry.capability != "tts" or entry.simulated:
            continue
        fails: list[str] = []
        warns: list[str] = []
        if entry.placeholder:
            fails.append("placeholder model_id (confirm the ElevenLabs model first)")
        if entry.needs_confirmation:
            warns.append("needs_confirmation is true")
        if entry.pricing_verified_on is None:
            warns.append(
                "price never verified (set pricing_verified_on after checking your dashboard)"
            )
        if fails:
            rows.append((f"TTS registry {key}", "FAIL", "; ".join(fails + warns)))
        elif warns:
            rows.append((f"TTS registry {key}", "WARN", "; ".join(warns)))
        else:
            rows.append(
                (
                    f"TTS registry {key}",
                    "PASS",
                    f"verified {entry.pricing_verified_on}; "
                    f"commercial_use_verified={entry.commercial_use_verified}",
                )
            )
    if not rows:
        rows.append(
            ("TTS registry", "WARN", "no non-simulated TTS entries (sandbox only?)")
        )
    return rows


def _phase3_doctor_rows() -> list[tuple[str, str, str]]:
    """Phase 3 checks: ffmpeg filtergraph option, fps, sample rate, audio
    library, TTS registry, voice attestations, ELEVENLABS_API_KEY, diacritics."""
    import json as _json

    rows: list[tuple[str, str, str]] = []
    home = _home()
    try:
        config = load_all(home)
    except StudioError as exc:
        return [("studio config", "FAIL", f"load_all({home}): {exc}"[:200])]
    settings = config.settings

    # ffmpeg filtergraph file option (informational)
    try:
        from studio.video.ffmpeg import ffmpeg_version

        ff_version = asyncio.run(asyncio.wait_for(ffmpeg_version(), timeout=30.0))
        via = ("-/filter_complex (ffmpeg >= 7)" if ff_version >= (7, 0, 0)
                else "-filter_complex_script (ffmpeg < 7)")
        rows.append(("ffmpeg filtergraph file option", "PASS", via))
    except Exception as exc:
        rows.append(("ffmpeg filtergraph file option", "WARN", str(exc)[:160]))

    # sample rate divisible by fps
    fps = settings.render.fps
    sample_rate = settings.audio.sample_rate
    if sample_rate % fps == 0:
        rows.append(
            (f"sample rate % fps ({sample_rate} % {fps})", "PASS",
             f"{sample_rate // fps} samples/frame")
        )
    else:
        rows.append((
            f"sample rate % fps ({sample_rate} % {fps})",
            "FAIL",
            f"{sample_rate} must be divisible by render.fps={fps}",
        ))

    # ELEVENLABS_API_KEY presence (never the value itself)
    if os.environ.get("ELEVENLABS_API_KEY"):
        rows.append(("ELEVENLABS_API_KEY", "PASS", "set (value hidden)"))
    else:
        rows.append((
            "ELEVENLABS_API_KEY",
            "WARN",
            "not set — required for real TTS calls (sandbox/mock does not need it)",
        ))

    # tts.keep_diacritics (informational)
    keep = settings.tts.keep_diacritics
    rows.append((
        "tts.keep_diacritics",
        "PASS",
        ("sends diacritized text as-is (higher char cost, better Arabic tashkeel)"
         if keep else
         "strips diacritics before sending (A/B knob; changes TTS cache keys)"),
    ))

    # audio library status
    try:
        db = Database(Path(settings.paths.db))
        migrate(db)
        clock = SystemClock()
        repos = build_repos(db, clock, UlidIds())
        library = repos.audio.list_library(include_inactive=True)
        if not library:
            rows.append(("audio library", "WARN", "empty — run `studio library scan`"))
        else:
            active = [r for r in library if r.active]
            per_kind: dict[str, int] = {}
            for row in active:
                per_kind[row.kind] = per_kind.get(row.kind, 0) + 1
            counts = ", ".join(f"{kind}={n}" for kind, n in sorted(per_kind.items()))
            rows.append((
                "audio library",
                "PASS",
                f"active: {counts}; inactive: {len(library) - len(active)}",
            ))
            cells: set[tuple[str, int]] = set()
            for row in active:
                if row.kind != "music" or row.intensity is None:
                    continue
                try:
                    tags = _json.loads(row.moods_json)
                except _json.JSONDecodeError:
                    tags = []
                for tag in tags:
                    cells.add((str(tag), int(row.intensity)))
            empty = [f"{mood}@{i}" for mood in config.vocab.moods
                    for i in range(1, 6) if (mood, i) not in cells]
            if empty:
                rows.append((
                    "library mood x intensity",
                    "WARN",
                    f"{len(empty)} empty cells (e.g. {', '.join(empty[:5])}) — selection falls "
                    f"back to neighbors",
                ))
            else:
                rows.append(
                    ("library mood x intensity", "PASS",
                     f"all {len(config.vocab.moods)} x 5 cells covered")
                )

        # voices without attestation (any series in this home)
        voice_rows = db.connect().execute(
            "SELECT settings_json FROM character_voices"
        ).fetchall()
        unattested = 0
        for voice in voice_rows:
            try:
                from studio.audio.voice import parse_voice_settings

                parsed = parse_voice_settings(str(voice["settings_json"]))
                attested = parsed.license_attested_at
            except Exception:
                attested = None
            if not attested:
                unattested += 1
        if not voice_rows:
            rows.append(("voice attestations", "PASS", "no voices configured in this home"))
        elif unattested:
            rows.append((
                "voice attestations",
                "FAIL",
                f"{unattested}/{len(voice_rows)} voices lack license_attested_at "
                f"(re-run `studio voice set ... --attest-commercial` before final)",
            ))
        else:
            rows.append(("voice attestations", "PASS", f"{len(voice_rows)} voices attested"))
    except Exception as exc:
        rows.append(("audio library / voices", "FAIL", str(exc)[:200]))

    rows.extend(_tts_registry_rows())
    return rows

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

    # D3: version >= 5.0 + the exact audio filter set Phase 3 relies on.
    # Uses the same helper as tests/test_ffmpeg_capabilities.py.
    try:
        from studio.video.ffmpeg import REQUIRED_FILTERS, capability_check

        # Timeout is config-driven, but the check must still run when config
        # itself is broken — fall back to the schema default (30s).
        timeout_s = 30.0
        try:
            timeout_s = float(load_all(_home()).settings.doctor.probe_timeout_s)
        except Exception as cfg_exc:  # config broken — still probe, default timeout
            timeout_s = 30.0
            _ = cfg_exc
        ff_version, missing = asyncio.run(asyncio.wait_for(capability_check(), timeout=timeout_s))
        version_ok = ff_version >= (5, 0, 0)
        checks.append(
            (
                "ffmpeg >= 5.0",
                "PASS" if version_ok else "FAIL",
                ".".join(str(part) for part in ff_version),
            )
        )
        checks.append(
            (
                "ffmpeg audio filters",
                "PASS" if not missing else "FAIL",
                ", ".join(missing) if missing else f"{len(set(REQUIRED_FILTERS))} filters present",
            )
        )
    except Exception as exc:
        checks.append(("ffmpeg capability check", "FAIL", str(exc)[:200]))

    # Phase 3: ffmpeg filtergraph option, fps, library, TTS registry, voices,
    # ELEVENLABS_API_KEY, tts.keep_diacritics.
    checks.extend(_phase3_doctor_rows())

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
# voice / narration (Phase 3)
# ==================================================================

def _resolve_series(repos: StudioRepos, series_ref: str) -> Any:
    series = repos.content.get_series(series_ref)
    if series is None:
        series = repos.content.get_series_by_slug(series_ref)
    if series is None:
        _fail(f"series not found: {series_ref!r} (pass a series id or slug)")
    return series


@voice_app.command("set")
def voice_set(
    character_slug: str = typer.Argument(..., help="Character slug (matched on characters.role)."),
    series: str = typer.Option(..., "--series", help="Series id or slug."),
    language: str = typer.Option(..., "--language", help="ar | en"),
    voice_id: str = typer.Option(..., "--voice-id", help="Provider voice id (never logged)."),
    model_key: str = typer.Option(
        "tts.main", "--model-key", help="Registry key for the TTS model."
    ),
    stability: float | None = typer.Option(None, "--stability", min=0.0, max=1.0),
    similarity_boost: float | None = typer.Option(None, "--similarity-boost", min=0.0, max=1.0),
    style: float | None = typer.Option(None, "--style", min=0.0, max=1.0),
    speaker_boost: bool | None = typer.Option(
        None, "--speaker-boost/--no-speaker-boost", help="Maps to ElevenLabs use_speaker_boost."
    ),
    speed: float | None = typer.Option(None, "--speed", help="Provider speech speed (> 0)."),
    mix_gain_db: float = typer.Option(
        0.0, "--mix-gain-db", help="Extra line gain applied at conform."
    ),
    attest_commercial: bool = typer.Option(
        False, "--attest-commercial",
        help="Stamp the commercial-use attestation (required for final).",
    ),
) -> None:
    """Upsert the voice for (character, language) in a series."""
    config, db, clock, _ids, repos = _connect()
    migrate(db)
    series_row = _resolve_series(repos, series)
    character = repos.content.get_character_by_slug(series_row.id, character_slug)
    if character is None:
        _fail(
            f"no character with role/slug {character_slug!r} in series {series_row.slug!r}; "
            f"create it first (the demo seed creates 'narrator')"
        )
    try:
        language_enum = Language(language.strip())
    except ValueError:
        _fail(f"unknown language {language!r} (allowed: ar, en)")
    if config.registry.models.get(model_key) is None:
        _fail(f"unknown model_key {model_key!r} in model_registry.yaml")

    from studio.audio.voice import VoiceSettings, parse_voice_settings, voice_settings_json

    existing = repos.audio.get_character_voice(
        series_id=series_row.id, character_slug=character_slug, language=language_enum.value
    )
    attested_at = format_ts(clock.now()) if attest_commercial else (
        parse_voice_settings(existing.settings_json).license_attested_at if existing else None
    )
    settings = VoiceSettings(
        model_key=model_key,
        stability=stability,
        similarity_boost=similarity_boost,
        style=style,
        speaker_boost=speaker_boost,
        speed=speed,
        mix_gain_db=mix_gain_db,
        license_attested_at=attested_at,
    )
    repos.audio.upsert_character_voice(
        series_id=series_row.id,
        character_slug=character_slug,
        language=language_enum.value,
        voice_id=voice_id,
        settings_json=voice_settings_json(settings),
    )
    console.print(f"voice set: series={series_row.slug} character={character_slug} "
                  f"language={language_enum.value} voice_id={voice_id} model={model_key}")
    console.print(f"attestation: {attested_at or 'none'}")


@voice_app.command("list")
def voice_list(
    series: str = typer.Option(..., "--series", help="Series id or slug."),
) -> None:
    """List voices in a series."""
    config, db, clock, ids, repos = _connect()
    migrate(db)
    del config, clock, ids
    from studio.audio.voice import parse_voice_settings

    series_row = _resolve_series(repos, series)
    table = Table(title=f"voices in {series_row.slug}")
    for column in ("character", "language", "voice_id", "model_key", "mix_gain_db", "attested"):
        table.add_column(column)
    rows = repos.audio.list_character_voices(series_id=series_row.id)
    if not rows:
        console.print("no voices set")
        return
    for row in rows:
        try:
            settings = parse_voice_settings(row.settings_json)
        except StudioError:
            settings = None
        if settings is None:
            table.add_row(row.character_slug, row.language, row.voice_id, "INVALID", "-", "INVALID")
        else:
            table.add_row(
                row.character_slug,
                row.language,
                row.voice_id,
                settings.model_key,
                f"{settings.mix_gain_db:+.1f}",
                settings.license_attested_at or "never",
            )
    console.print(table)


@narration_app.command("show")
def narration_show(
    episode: str = typer.Argument(..., help="Episode id."),
) -> None:
    """Show narration lines per deliverable (text, voicing state)."""
    config, _db, clock, ids, repos = _connect()
    del clock, ids
    episode_row = repos.content.get_episode(episode)
    if episode_row is None:
        _fail(f"episode not found: {episode}")
    tracks = {t.id: t for t in repos.content.list_tracks(episode_row.id)}
    deliverables = repos.content.list_deliverables(episode_row.id)
    shot_no = {
        s.id: s.shot_no for t in tracks.values() for s in repos.content.shots_for_track(t.id)
    }
    for deliverable in deliverables:
        console.print(
            f"[bold]deliverable {deliverable.id}[/bold]  track={deliverable.track_id} "
            f"language={deliverable.language}"
        )
        lines = repos.content.narration_lines(deliverable.id)
        table = Table()
        for column in ("shot", "line", "status", "conf_ms", "nonce", "text"):
            table.add_column(column)
        for line in lines:
            conf_ms = (
                round(line.conformed_samples / config.settings.audio.sample_rate * 1000)
                if line.conformed_samples is not None
                else None
            )
            text = line.approved_text.replace("\n", " ")
            if len(text) > 80:
                text = text[:77] + "…"
            table.add_row(
                str(shot_no.get(line.shot_id, "?")),
                str(line.line_no),
                line.status,
                str(conf_ms) if conf_ms is not None else "—",
                line.tts_nonce,
                text,
            )
        console.print(table)


@narration_app.command("edit")
def narration_edit(
    line_id: str = typer.Argument(..., help="Narration line id."),
    text: str | None = typer.Option(None, "--text", help="Replacement text (UTF-8)."),
    file: Path | None = typer.Option(None, "--file", help="UTF-8 file with the replacement text."),  # noqa: B008
) -> None:
    """Edit a line's text after Gate 1 (audit row, nonce unchanged)."""
    if (text is None) == (file is None):
        _fail("pass exactly one of --text or --file")
    if text is not None:
        new_text = text.strip()
    else:
        assert file is not None
        new_text = file.read_text(encoding="utf-8").strip()
    if not new_text:
        _fail("replacement text is empty")
    config, _db, clock, ids, repos = _connect()
    del config, clock, ids
    line = repos.content.get_narration_line(line_id)
    if line is None:
        _fail(f"narration line not found: {line_id}")
    old_text = line.approved_text
    if old_text == new_text:
        console.print("no change")
        return
    repos.content.set_line_text_final(line_id, new_text)
    repos.audit.append(
        actor="operator",
        entity=f"narration_line:{line_id}",
        event="post_gate_edit",
        payload={
            "line_id": line_id,
            "old_text_sha256": hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
            "new_text_sha256": hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
        },
    )
    console.print(
        f"line {line_id} updated; run `studio resume <run-id>` to re-voice only this line"
    )


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



# ======================================================================
# library — audio library scan / list / check (Phase 3, §H13)
# ======================================================================


@library_app.command("scan")
def library_scan() -> None:
    """Reconcile <library_root> with the audio_library table (idempotent)."""
    from studio.audio.library import resolve_library_root, scan_library

    config, db, clock, _ids, repos = _connect()
    del clock
    migrate(db)
    root = resolve_library_root(config.settings)
    if not root.is_dir():
        console.print(
            f"[yellow]library root {root} does not exist — nothing scanned "
            f"(set audio.library_root in settings.yaml, or run "
            f"`studio run --demo` to seed fixtures)[/yellow]"
        )
        return
    report = _run_async(
        scan_library(
            library_root=root,
            repos=repos.audio,
            vocab=config.vocab,
            scan_extensions=tuple(config.settings.audio.scan_extensions),
        )
    )
    active = repos.audio.list_library(include_inactive=False)
    inactive = [r for r in repos.audio.list_library(include_inactive=True) if not r.active]
    console.print(f"library scan: {root}")
    console.print(f"  scanned (active-eligible files): {report.scanned}")
    console.print(f"  activated this run: {report.activated}")
    console.print(f"  updated this run: {report.updated}")
    console.print(f"  active tracks: {len(active)}")
    console.print(f"  inactive tracks: {len(inactive)}")
    if report.missing_on_disk:
        console.print("  missing on disk (kept, marked inactive):")
        for rel in report.missing_on_disk:
            console.print(f"    - {rel}")
    if report.invalid:
        console.print("  invalid (registered inactive with scan_error):")
        for rel, err in report.invalid:
            console.print(f"    - {rel}: {err}")
    if not report.ok:
        console.print("[yellow]library scan completed with problems (see above)[/yellow]")


@library_app.command("list")
def library_list(
    kind: str | None = typer.Option(None, "--kind", help="music | sfx | ambience"),
    inactive: bool = typer.Option(False, "--inactive", help="Include inactive tracks."),
) -> None:
    """List audio library entries (active only by default)."""
    if kind is not None and kind not in ("music", "sfx", "ambience"):
        _fail(f"unknown --kind {kind!r} (expected music|sfx|ambience)")
    config, db, clock, _ids, repos = _connect()
    del config, db, clock
    rows = repos.audio.list_library(kind=kind, include_inactive=inactive)
    if not rows:
        console.print("no library entries (run `studio library scan` first)")
        return
    table = Table(title=f"audio library{' — all' if inactive else ''}")
    for column in ("kind", "title", "tags", "intensity", "dur", "lufs", "license", "state"):
        table.add_column(column)
    for row in rows:
        try:
            tags = ",".join(json.loads(row.moods_json))
        except json.JSONDecodeError:
            tags = "INVALID"
        lufs = f"{row.loudness_lufs:+.1f}" if row.loudness_lufs is not None else "-"
        state = "active" if row.active else f"inactive ({row.scan_error or 'unknown'})"
        table.add_row(
            row.kind,
            row.title,
            tags,
            str(row.intensity) if row.intensity is not None else "-",
            f"{row.duration_ms / 1000:.1f}s",
            lufs,
            row.license or "-",
            state,
        )
    console.print(table)


@library_app.command("check")
def library_check() -> None:
    """Print mood x intensity coverage for ACTIVE tracks; warn on empty cells."""
    config, db, clock, _ids, repos = _connect()
    del db, clock
    music = repos.audio.list_library(kind="music", include_inactive=False)
    tag_of: dict[str, set[str]] = {}
    cells: dict[tuple[str, int], int] = {}
    empty_cells: list[str] = []
    for row in music:
        try:
            tags = json.loads(row.moods_json)
        except json.JSONDecodeError:
            tags = []
        if row.intensity is None:
            continue
        for tag in tags:
            tag_of.setdefault(tag, set()).add(str(row.intensity))
            cells[(tag, int(row.intensity))] = cells.get((tag, int(row.intensity)), 0) + 1
    table = Table(title="music coverage (active tracks): cells = track count")
    table.add_column("mood")
    for intensity in range(1, 6):
        table.add_column(str(intensity))
    for mood in config.vocab.moods:
        row_cells: list[str] = []
        for intensity in range(1, 6):
            count = cells.get((mood, intensity), 0)
            if count == 0:
                row_cells.append("[yellow]·[/yellow]")
                empty_cells.append(f"{mood} @ intensity {intensity}")
            else:
                row_cells.append(str(count))
        table.add_row(mood, *row_cells)
    console.print(table)

    for kind, tags in (("ambience", config.vocab.ambience_tags), ("sfx", config.vocab.sfx_tags)):
        rows = repos.audio.list_library(kind=kind, include_inactive=False)
        counts = {tag: 0 for tag in tags}
        for row in rows:
            try:
                row_tags = json.loads(row.moods_json)
            except json.JSONDecodeError:
                row_tags = []
            for tag in row_tags:
                if tag in counts:
                    counts[tag] += 1
        counts_text = ", ".join(f"{tag}={n}" for tag, n in sorted(counts.items()))
        console.print(f"{kind} tags: {counts_text}")
        uncovered = [tag for tag, n in counts.items() if n == 0]
        if uncovered:
            missing_text = ", ".join(sorted(uncovered))
            console.print(f"[yellow]{kind}: no active track for: {missing_text}[/yellow]")

    if empty_cells:
        console.print(
            f"[yellow]warning: {len(empty_cells)} empty mood x intensity cells "
            f"(scoring may fall back to neighbors or loop)[/yellow]"
        )


# ======================================================================
# tts — ElevenLabs audition (Phase 3, §H2)
# ======================================================================


@tts_app.command("audition")
def tts_audition(
    voice_id: str = typer.Option(..., "--voice-id", help="Provider voice id."),
    model_keys: str = typer.Option(
        "tts.main",
        "--model-keys",
        help="Comma-separated registry keys (audition candidates).",
    ),
    lines_file: str = typer.Option(..., "--lines-file", help="UTF-8 file with one line per call."),
    language: str = typer.Option(Language.AR.value, "--language", help="ar | en"),
    no_diacritics_variant: bool = typer.Option(
        False, "--no-diacritics-variant", help="Only the diacritized text, no stripped variant."
    ),
    cap_usd: float = typer.Option(
        0.50, "--cap-usd", min=0.0, help="Hard cap on total audition spend (worst case)."
    ),
    yes: bool = typer.Option(False, "--yes", help="Proceed (required to spend)."),
) -> None:
    """Render (model x line x diacritics-variant) through the Gateway.

    episode_id is NULL on purpose: month/global budgets apply. The worst-case
    total must fit under --cap-usd before ANY call is made; a running-total
    guard re-checks before every subsequent call. Deterministic nonces make
    re-running the same audition free (cache hits).
    """
    import hashlib
    import shutil
    from datetime import UTC, datetime

    from studio.core.gateway import CallContext, ProviderGateway
    from studio.core.pricing import estimate_cost_musd
    from studio.domain.money import usd_to_musd
    from studio.language.tashkeel import normalize_nfc, strip_tashkeel
    from studio.providers.base import TTSRequest

    config, db, clock, ids, repos = _connect()
    del repos
    migrate(db)
    try:
        language_enum = Language(language.strip())
    except ValueError:
        _fail(f"unknown language {language!r} (allowed: ar, en)")

    lines_path = Path(lines_file)
    if not lines_path.is_file():
        _fail(f"lines file not found: {lines_path}")
    try:
        raw = lines_path.read_bytes()
        text_file = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail(f"{lines_file} is not UTF-8 (save it as UTF-8; Windows consoles mangle Arabic argv)")
    lines = [line.strip() for line in text_file.splitlines() if line.strip()]
    if not lines:
        _fail(f"{lines_file} contains no non-empty lines")

    keys = [key.strip() for key in model_keys.split(",") if key.strip()]
    if not keys:
        _fail("--model-keys must name at least one registry key")
    entries: list[tuple[str, Any]] = []
    for key in keys:
        entry = config.registry.models.get(key)
        if entry is None:
            _fail(f"unknown model_key {key!r} in model_registry.yaml")
        if entry.capability != "tts":
            _fail(f"{key!r} has capability {entry.capability!r}, expected tts")
        entries.append((key, entry))

    keep_diacritics = bool(config.settings.tts.keep_diacritics)

    def variants(text: str) -> list[tuple[str, str]]:
        out = [("diacritics", text)] if keep_diacritics else [("plain", text)]
        if not no_diacritics_variant:
            plain = strip_tashkeel(normalize_nfc(text))
            if plain != out[0][1]:
                out.append(("plain", plain))
        return out

    plan: list[tuple[str, Any, str, str, str]] = []  # key, entry, text, variant, filename_slug
    for index, line in enumerate(lines, start=1):
        for key, entry in entries:
            for variant, variant_text in variants(line):
                slug = key.replace(".", "-")
                plan.append((key, entry, variant_text, variant, f"{index:03d}_{slug}_{variant}"))

    cap_musd = usd_to_musd(str(cap_usd))
    estimates = []
    for _key, entry, variant_text, _variant, _slug in plan:
        req = TTSRequest(text=variant_text, voice_id=voice_id, language=language_enum)
        estimates.append(estimate_cost_musd(entry, req))
    worst_case_total = sum(estimates)
    console.print(
        f"audition plan: {len(lines)} lines x {len(entries)} models "
        f"x up to 2 variants = {len(plan)} files"
    )
    console.print(f"worst-case cost: ${musd_to_usd(worst_case_total):.4f} (cap ${cap_usd:.4f})")
    if worst_case_total > cap_musd:
        _fail(
            f"worst-case audition cost exceeds --cap-usd "
            f"({musd_to_usd(worst_case_total):.4f} > {cap_usd:.4f}); "
            f"raise the cap or use fewer lines/models"
        )
    if not yes:
        _fail("aborted (nothing spent) — re-run with --yes to proceed")

    from studio.core.cas import ContentStore

    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    gateway = ProviderGateway(
        db=db,
        settings=config.settings,
        registry=config.registry,
        cas=cas,
        ledger=_ledger_for(db, config, clock, ids),
        clock=clock,
        ids=ids,
        poll_interval_s=0.05,
        max_polls=1000,
    )

    outdir = (
        Path(config.settings.paths.home)
        / "data"
        / "previews"
        / "audition"
        / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    outdir.mkdir(parents=True, exist_ok=True)

    report_lines = [
        "tts audition report",
        f"voice_id: {voice_id}",
        f"models: {', '.join(keys)}",
        f"cap_usd: {cap_usd}",
        "",
    ]
    total_spent = 0
    failures: list[str] = []
    for (key, _entry, variant_text, variant, slug), est in zip(plan, estimates, strict=True):
        if total_spent + est > cap_musd:
            failures.append(f"{slug}: skipped — running total would exceed the cap")
            continue
        nonce_material = f"{key}|{voice_id}|{variant}|{variant_text}".encode()
        nonce = hashlib.sha256(nonce_material).hexdigest()[:16]
        req = TTSRequest(text=variant_text, voice_id=voice_id, language=language_enum)
        ctx = CallContext(
            episode_id=None, stage_run_id=None, nonce=nonce, purpose="tts_audition"
        )
        try:
            result = _run_async(gateway.tts(key, req, ctx))
        except Exception as exc:
            failures.append(f"{slug}: {type(exc).__name__}: {exc}")
            continue
        ref = result.assets[0]
        source = cas.resolve(ref.sha256)
        target = outdir / f"{slug}{source.suffix or '.mp3'}"
        shutil.copy2(source, target)
        total_spent += int(result.cost_musd)
        report_lines.append(
            f"{target.name}\tmodel={key}\tvariant={variant}\tchars={len(variant_text)}\t"
            f"cost_usd={musd_to_usd(int(result.cost_musd)):.4f}"
        )
        console.print(f"  {target.name}  ${musd_to_usd(int(result.cost_musd)):.4f}")

    report_lines.append("")
    report_lines.append(f"total_spent_usd: {musd_to_usd(total_spent):.4f}")
    if failures:
        report_lines.append("failures:")
        report_lines.extend(f"  - {failure}" for failure in failures)
    (outdir / "README.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    console.print(f"audition written to {outdir}")
    console.print(f"total spent: ${musd_to_usd(total_spent):.4f}")
    if failures:
        console.print(f"[yellow]{len(failures)} call(s) failed/skipped (see README.txt)[/yellow]")


def _ledger_for(
    db: Database, config: AppConfig, clock: SystemClock, ids: UlidIds
) -> LedgerService:
    return LedgerService(db, clock, config.settings, ids)


# ======================================================================
# audio — preview / QC report / calibrate (Phase 3, §H13)
# ======================================================================


@audio_app.command("preview")
def audio_preview(
    deliverable_id: str = typer.Argument(..., help="Deliverable id."),
    stem: str = typer.Option("master", "--stem", help="master | narration"),
) -> None:
    """Copy the deliverable's audio into data/previews/ with a readable name."""
    import re
    import shutil

    from studio.core.cas import ContentStore

    if stem not in ("master", "narration"):
        _fail(f"unknown --stem {stem!r} (expected master|narration)")
    config, db, clock, _ids, repos = _connect()
    migrate(db)
    masters = repos.audio.list_masters(deliverable_id=deliverable_id)
    ready = [row for row in masters if row.status == "ready"]
    if not ready:
        _fail(f"no ready audio master for deliverable {deliverable_id} (run the pipeline first)")
    row = sorted(ready, key=lambda r: r.created_at)[-1]
    sha = row.master_sha256 if stem == "master" else row.narration_stem_sha256
    if not sha:
        _fail("the ready master has no narration stem recorded")
    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    source = cas.resolve(sha)

    deliverable = repos.content.get_deliverable(deliverable_id)
    language = deliverable.language if deliverable else "und"
    slug = "deliverable"
    if deliverable is not None:
        track = repos.content.get_track(deliverable.track_id)
        episode = repos.content.get_episode(track.episode_id) if track else None
        if episode is not None:
            slug = re.sub(r"[^a-z0-9]+", "-", episode.title.lower()).strip("-")[:40] or slug
    target_dir = Path(config.settings.paths.home) / "data" / "previews"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{slug}-{language}-{stem}-{row.input_hash[:8]}{source.suffix}"
    shutil.copy2(source, target)
    console.print(f"preview written to {target} ({target.stat().st_size} bytes)")


@audio_app.command("qc")
def audio_qc(
    deliverable_id: str = typer.Argument(..., help="Deliverable id."),
) -> None:
    """Print the QC results persisted for the latest ready master.

    Note: the music pre/post-duck stems are mix-time work files, so duck
    depth cannot be re-measured without a re-render. This prints exactly the
    QC captured when the master was produced.
    """
    _config, db, clock, _ids, repos = _connect()
    del _config, clock, _ids
    migrate(db)
    masters = repos.audio.list_masters(deliverable_id=deliverable_id)
    ready = [row for row in masters if row.status == "ready"]
    if not ready:
        _fail(f"no ready audio master for deliverable {deliverable_id}")
    row = sorted(ready, key=lambda r: r.created_at)[-1]
    console.print(
        f"master: deliverable={deliverable_id} total_samples={row.total_samples} "
        f"loudness_i={row.loudness_i} true_peak={row.true_peak} lra={row.lra} "
        f"duck_depth_db={row.duck_depth_db}"
    )
    try:
        stats = json.loads(row.stats_json or "{}")
    except json.JSONDecodeError:
        stats = {}
    qc = stats.get("qc") or []
    if not qc:
        console.print("[yellow]no persisted QC detail on this master row[/yellow]")
        return
    table = Table(title="audio QC (persisted at mix time)")
    for column in ("check", "status", "detail"):
        table.add_column(column)
    for result in qc:
        status = str(result.get("status", "?"))
        style = {"pass": "green", "warn": "yellow", "fail": "red"}.get(status, "white")
        table.add_row(
            str(result.get("name", "?")),
            f"[{style}]{status}[/{style}]",
            json.dumps(result.get("detail", {}), ensure_ascii=False, sort_keys=True),
        )
    console.print(table)


@audio_app.command("calibrate")
def audio_calibrate(
    episode: str | None = typer.Option(None, "--episode", help="Episode id (default: all)."),
) -> None:
    """Print measured words/second per language and a suggested setting.

    Reads narration_lines with voiced audio_duration_ms and word counts.
    Prints only — the config file is never modified.
    """
    from studio.domain.enums import Language as _Language
    from studio.language import count_words

    _config, db, clock, _ids, repos = _connect()
    del _config, clock, _ids, repos
    migrate(db)
    if episode is not None:
        row = db.connect().execute(
            "SELECT id FROM episodes WHERE id = ?", (episode,)
        ).fetchone()
        if row is None:
            _fail(f"episode not found: {episode}")
    rows = db.connect().execute(
        """
        SELECT d.language AS language,
               COALESCE(n.text_final, n.text) AS text,
               n.duration_ms AS duration_ms
        FROM narration_lines n
        JOIN deliverables d ON d.id = n.deliverable_id
        JOIN tracks t ON t.id = d.track_id
        JOIN episodes e ON e.id = t.episode_id
        WHERE (? IS NULL OR e.id = ?)
          AND n.duration_ms IS NOT NULL AND n.duration_ms > 0
        """,
        (episode, episode),
    ).fetchall()
    if not rows:
        _fail("no voiced lines found (run the pipeline first)")
    totals: dict[str, list[float]] = {}
    for row in rows:
        lang = str(row["language"])
        try:
            language_enum = _Language(lang)
        except ValueError:
            continue
        words = count_words(language_enum, str(row["text"]))
        seconds = float(row["duration_ms"]) / 1000.0
        entry = totals.setdefault(lang, [0.0, 0.0, 0.0])
        entry[0] += words
        entry[1] += seconds
        entry[2] += 1
    table = Table(title="measured speech rate (voiced lines)")
    for column in ("language", "lines", "words", "seconds", "words/second"):
        table.add_column(column)
    suggested: dict[str, float] = {}
    for lang, (total_words, total_seconds, line_count) in sorted(totals.items()):
        wps = total_words / total_seconds if total_seconds else 0.0
        suggested[lang] = round(wps, 2)
        table.add_row(
            lang, str(int(line_count)), str(int(total_words)),
            f"{total_seconds:.1f}", f"{wps:.2f}",
        )
    console.print(table)
    console.print(
        "suggested timing.words_per_second (paste into settings.yaml):"
    )
    suggested_text = ", ".join(f"{k}: {v}" for k, v in sorted(suggested.items()))
    console.print(f"  words_per_second: {{{suggested_text}}}")
    console.print("(calibrate never writes config)")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":
    main()
