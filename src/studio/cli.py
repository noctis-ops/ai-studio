"""`studio` command-line interface (Typer)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

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
from studio.domain.enums import BudgetScope, Language, Profile, RunStatus
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
        "AI Studio — Phase 5 (s9 animatic, s11 assembly, s12 render QC, s13 export, "
        "s14 bible proposals, s15 publish/purge; mock demo by default, paid paths "
        "are operator-gated)."
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
encode_app = typer.Typer(help="Encoder utilities (probe).")
app.add_typer(encode_app, name="encode")
render_app = typer.Typer(
    help="Final render utilities (rebuild is free: it never re-pays a provider).",
    no_args_is_help=True,
)
app.add_typer(render_app, name="render")
publish_app = typer.Typer(
    help="Publish marker (S15): an export becomes a fact only when it is marked.",
    no_args_is_help=True,
)
app.add_typer(publish_app, name="publish")
purge_app = typer.Typer(
    help="Purge (S15): plan is free, run deletes, gc cleans orphans. Dry run by default.",
    no_args_is_help=True,
)
app.add_typer(purge_app, name="purge")
bible_app = typer.Typer(
    help="Series Bible proposals (S14). Accepting one bumps the bible version.",
    no_args_is_help=True,
)
app.add_typer(bible_app, name="bible")

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


def _deliverable_by_key(
    repos: StudioRepos, run: Any, key: str
) -> str | None:
    """Resolve a deliverable by id, language code or channel id."""
    episode_id = getattr(run, "episode_id", None)
    if not episode_id:
        return None
    deliverables = repos.content.list_deliverables(episode_id)
    for row in deliverables:
        if row.id == key:
            return row.id
    for row in deliverables:
        if row.language == key:
            return row.id
    for row in deliverables:
        if row.channel_id == key:
            return row.id
    return None


def _flag_present(name: str) -> bool:
    """True when ``name`` was passed on the command line (typer default aside)."""
    return any(arg == name or arg.startswith(f"{name}=") for arg in sys.argv[1:])


def _render_rows(repos: StudioRepos, run_id: str, deliverable_id: str | None) -> list[str]:
    """Stage-run ids for S11/S12 of ``deliverable_id`` (or every deliverable)."""
    out: list[str] = []
    for row in repos.pipeline.list_stage_runs(run_id):
        if row.stage not in ("s11_assemble", "s12_render_qc"):
            continue
        if row.subject_scope != "deliverable":
            continue
        if deliverable_id is not None and row.subject_id != deliverable_id:
            continue
        out.append(row.id)
    return out


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

def _media_doctor_rows(missing_filters: list[str] | None) -> list[tuple[str, str, str]]:
    """Phase 4 checks: FAL_KEY, media registry confirmation/prices/commercial
    flags, ref limits + aspects/durations, failed_job_billing, the new
    ffmpeg filters, chain_mode, and one WARN per placeholder endpoint."""
    rows: list[tuple[str, str, str]] = []
    home = _home()
    try:
        config = load_all(home)
    except StudioError as exc:
        return [("studio config", "FAIL", f"load_all({home}): {exc}"[:200])]
    settings = config.settings

    # FAL_KEY presence (never the value itself)
    if os.environ.get("FAL_KEY"):
        rows.append(("FAL_KEY", "PASS", "set (value hidden)"))
    else:
        rows.append((
            "FAL_KEY",
            "WARN",
            "not set — required for real image/video calls (sandbox mocks do not need it)",
        ))

    # Media registry entries: confirmed, prices verified, commercial flags.
    media_entries = [
        (key, entry)
        for key, entry in sorted(config.registry.models.items())
        if entry.capability in ("image_reference_generate", "image_to_video")
    ]
    for key, entry in media_entries:
        problems: list[str] = []
        if entry.placeholder:
            problems.append(f"placeholder endpoint {entry.model_ref!r}")
        if entry.needs_confirmation:
            problems.append("needs_confirmation is true")
        if entry.pricing_verified_on is None and not entry.simulated:
            problems.append("price never verified")
        if entry.is_zero_price and not entry.simulated and not entry.pricing_acknowledged_free:
            problems.append("zero price without pricing_acknowledged_free")
        licence = (
            "commercial use verified"
            if entry.commercial_use_verified
            else "commercial use NOT verified (final blocked)"
        )
        verified_on = entry.pricing_verified_on
        verified_text = verified_on.isoformat() if verified_on else "never"
        name = f"media registry {key} (verified {verified_text})"
        if problems:
            rows.append((name, "WARN", "; ".join(problems) + f" · {licence}"))
        else:
            rows.append((name, "PASS", licence))
    placeholder_media = [
        (key, entry) for key, entry in media_entries if entry.placeholder
    ]
    if not media_entries:
        rows.append(("media registry", "WARN", "no media entries (sandbox only?)"))
    for key, entry in placeholder_media:
        rows.append((
            f"placeholder endpoint {key}",
            "WARN",
            f"{entry.model_ref!r} — the operator must confirm the endpoint, "
            "pricing and commercial licence before spending",
        ))

    # max_refs / supported aspects / durations (informational).
    for key, entry in media_entries:
        spec = entry.media
        if spec is None:
            rows.append((
                f"media spec {key}",
                "WARN",
                "no media wire contract — confirm it in model_registry.yaml",
            ))
            continue
        aspects = ",".join(spec.supported_aspects) or "(unset)"
        if entry.capability == "image_to_video":
            durations = ",".join(str(d) for d in spec.supported_durations_s) or "(unset)"
            detail = f"durations_s={durations} · aspects={aspects}"
        else:
            detail = f"max_refs={spec.max_refs} · aspects={aspects}"
        rows.append((f"media spec {key}", "PASS", detail))

    # failed_job_billing notice (explicitly unverified).
    billing = settings.media.failed_job_billing
    rows.append((
        "media.failed_job_billing",
        "WARN",
        f"{billing!r} — unverified how fal bills failed/flagged jobs; "
        "'charge' reserves worst case (conservative), 'free' releases on failure",
    ))

    # The new visual ffmpeg filters used by the media/QC chain.
    visual = ("signalstats", "blackdetect", "freezedetect", "ssim", "fps", "scale",
              "setpts", "tpad", "trim")
    if missing_filters is None:
        rows.append(("ffmpeg visual filters", "WARN", "capability probe did not run"))
    else:
        missing_visual = [name for name in visual if name in missing_filters]
        if missing_visual:
            rows.append(("ffmpeg visual filters", "FAIL",
                         "missing: " + ", ".join(missing_visual)))
        else:
            rows.append(("ffmpeg visual filters", "PASS",
                         ", ".join(visual)))

    # chain_mode must stay "off" until Phase 5.
    chain_mode = settings.media.chain_mode
    if chain_mode == "off":
        rows.append(("media.chain_mode", "PASS", "'off' (Phase 5 owns chaining)"))
    else:
        rows.append((
            "media.chain_mode",
            "FAIL",
            f"{chain_mode!r} — only 'off' is supported before Phase 5",
        ))
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


def _config_note_rows() -> list[tuple[str, str, str]]:
    """H5.1: one INFO row per deprecated-but-accepted settings key."""
    from studio.config.settings import deprecated_settings_in_file

    path = _home() / "configs" / "settings.yaml"
    keys = deprecated_settings_in_file(path)
    if not keys:
        return []
    from studio.config.settings import _DEPRECATED_SETTINGS

    return [
        (
            f"{key} (deprecated)",
            "INFO",
            _DEPRECATED_SETTINGS[key],
        )
        for key in keys
    ]


def _audit_config_notes() -> None:
    """Write one audit row per accepted deprecated key (never fatal)."""
    try:
        config = load_all(_home())
    except Exception:
        return
    if not config.config_notes:
        return
    try:
        _, db, _clock, _ids, repos = _connect()
        migrate(db)
        for note in config.config_notes:
            repos.audit.append(
                actor="config",
                entity="config:settings.yaml",
                event="config_deprecated_key_ignored",
                payload={"note": note},
            )
    except Exception:
        return


@app.command()
def doctor(
    llm: bool = typer.Option(
        False,
        "--llm",
        help="Opt-in live 1-token ping per LLM adapter (never used in CI).",
    ),
) -> None:
    """Environment checks. NEVER fails on the NVENC probe."""
    _audit_config_notes()
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

    missing: list[str] | None = None
    ffmpeg = _which("ffmpeg")
    checks.append(("ffmpeg on PATH", "PASS" if ffmpeg else "FAIL", ffmpeg or "not found"))
    ffprobe = _which("ffprobe")
    checks.append(("ffprobe on PATH", "PASS" if ffprobe else "FAIL", ffprobe or "not found"))

    # D3/H3: version >= 5.0 + the exact audio and visual filter sets.
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

    # Phase 4: carry the visual-filter result forward for the media rows.
    missing_filters = missing

    # Phase 3: ffmpeg filtergraph option, fps, library, TTS registry, voices,
    # ELEVENLABS_API_KEY, tts.keep_diacritics.
    checks.extend(_phase3_doctor_rows())

    # Phase 4: FAL_KEY, media registry confirmation/prices/commercial flags,
    # ref limits + aspects/durations, failed_job_billing, visual filters,
    # chain_mode, placeholder endpoints.
    checks.extend(_media_doctor_rows(missing_filters))

    # H5.1: deprecated settings are accepted-and-ignored, never silent.
    checks.extend(_config_note_rows())

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

    # Phase 5 §D: which encoder S11 will actually use, and why. Informational
    # only — a fallback to x264 is a valid outcome, never a failed doctor.
    if ffmpeg:
        try:
            from studio.video.encoders import SubprocessProbeRunner, driver_string
            from studio.video.encoders import resolve_encoder as _resolve_encoder
            from studio.video.ffmpeg import ffmpeg_version as _ffmpeg_version

            settings = load_all(_home()).settings
            resolution = asyncio.run(
                _resolve_encoder(
                    settings,
                    SubprocessProbeRunner(),
                    geometries=[(settings.render.fps, settings.render.fps)],
                    cache_path=Path(settings.paths.home)
                    / settings.encode.probe.cache_file,
                    ffmpeg_version=".".join(str(p) for p in asyncio.run(_ffmpeg_version())),
                    driver=driver_string(),
                )
            )
            checks.append(
                (
                    "encoder selection (Phase 5)",
                    "INFO",
                    f"{resolution.profile.name} ({resolution.reason})",
                )
            )
        except Exception as exc:
            checks.append(
                (
                    "encoder selection (Phase 5)",
                    "WARN",
                    f"could not resolve: {str(exc)[:160]}",
                )
            )

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

    # Phase 5 §N: export root writable, free disk, presets vs channels,
    # retention settings, leftover export scratch.
    checks.extend(_phase5_doctor_rows())

    table = Table(title="studio doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    failures = 0
    for name, status, detail in checks:
        style = {"PASS": "green", "WARN": "yellow", "FAIL": "red", "INFO": "cyan"}[status]
        table.add_row(name, f"[{style}]{status}[/{style}]", detail)
        if status == "FAIL":
            failures += 1
    console.print(table)
    if failures:
        raise typer.Exit(code=1)


def _phase5_doctor_rows() -> list[tuple[str, str, str]]:
    """Doctor rows added by Phase 5 (§N): export, disk, presets, retention, scratch."""
    import shutil as _shutil

    rows: list[tuple[str, str, str]] = []
    try:
        app_config = load_all(_home())
        settings = app_config.settings
    except Exception as exc:
        return [("phase 5 settings", "FAIL", str(exc)[:200])]

    # 1. export root writable (the final root, and the draft root's parent).
    for label, path in (
        ("export.root", Path(settings.paths.home) / settings.export.root),
        ("export.draft_root", Path(settings.paths.home) / settings.export.draft_root),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            rows.append((f"{label} writable", "PASS", str(path)))
        except OSError as exc:
            rows.append((f"{label} writable", "FAIL", f"{path}: {exc}"))

    # 2. free disk where renders land (warn under 5 GB, fail under 1 GB).
    try:
        usage = _shutil.disk_usage(Path(settings.paths.home))
        free_gb = usage.free / 1024**3
        status = "PASS" if free_gb >= 5 else ("WARN" if free_gb >= 1 else "FAIL")
        rows.append(("free disk (renders/exports)", status, f"{free_gb:.1f} GB free"))
    except OSError as exc:  # pragma: no cover - platform dependent
        rows.append(("free disk (renders/exports)", "WARN", str(exc)[:160]))

    # 3. presets vs channels: every channel key must have a matching preset.
    try:
        channels = set(app_config.channels.channels)
        missing = sorted(channels - set(app_config.presets.channels))
        extra = sorted(set(app_config.presets.channels) - channels)
        if missing or extra:
            rows.append(
                (
                    "presets vs channels",
                    "FAIL",
                    f"missing presets: {missing}; stale presets: {extra}",
                )
            )
        else:
            rows.append(
                ("presets vs channels", "PASS", f"{len(channels)} channel keys, 4 presets")
            )
    except Exception as exc:
        rows.append(("presets vs channels", "FAIL", str(exc)[:160]))

    # 4. retention sanity: a raw window of 0 days purges everything on day one.
    retention = settings.retention
    small = [
        name
        for name, value in (
            ("keep_raw_clips_days", retention.keep_raw_clips_days),
            ("keep_narration_audio_days", retention.keep_narration_audio_days),
            ("rejected_attempt_days", retention.rejected_attempt_days),
        )
        if int(value) < 1
    ]
    rows.append(
        (
            "retention windows",
            "WARN" if small else "PASS",
            f"raw clips {retention.keep_raw_clips_days}d, narration "
            f"{retention.keep_narration_audio_days}d, rejected attempts "
            f"{retention.rejected_attempt_days}d"
            + (f" (zero: {', '.join(small)})" if small else ""),
        )
    )

    # 5. leftover export scratch from a crashed run.
    leftovers: list[str] = []
    for root in (
        Path(settings.paths.home) / settings.export.root,
        Path(settings.paths.home) / settings.export.draft_root,
    ):
        if root.is_dir():
            leftovers.extend(str(path) for path in root.rglob(".tmp-*") if path.is_dir())
    rows.append(
        (
            "leftover export .tmp-* dirs",
            "WARN" if leftovers else "PASS",
            f"{len(leftovers)} leftover (removed by the next export)" if leftovers else "none",
        )
    )
    return rows


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
        "[yellow]media stages require confirmed fal endpoints and verified prices: "
        "the production DAG executes S1-S4, then the free s4p preflight fails "
        "closed until model_registry.yaml is confirmed (Phase 4).[/yellow]"
    )
    console.print("drive it:     studio worker --until-idle --production")
    console.print("approve G1:   studio gates approve <gate-id>   (then re-run the worker)")


@app.command()
def worker(
    until_idle: bool = typer.Option(False, "--until-idle", help="Stop when the queue is drained."),
    max_jobs: int | None = typer.Option(None, "--max-jobs", help="Execute at most N jobs."),
    production: bool = typer.Option(
        False, "--production", help="Drive the production DAG (S1-S4 + audio/media)."
    ),
) -> None:
    """Run the worker loop."""
    config, db, clock, ids, repos = _connect()
    migrate(db)
    dag = Dag(_pipeline_specs(production))
    engine = Engine(
        db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids,
        llm_subset_only=False,
    )
    instance = Worker(db, engine, repos, config, clock)

    jobs = _run_async(instance.run(until_idle=until_idle, max_jobs=max_jobs))
    console.print(f"jobs executed: {jobs}")


@app.command()
def resume(
    run_id: str = typer.Argument(...),
    production: bool = typer.Option(
        False, "--production", help="Resume a production-DAG run."
    ),
) -> None:
    """Resume a halted_budget or failed run."""
    config, db, clock, ids, repos = _connect()
    dag = Dag(_pipeline_specs(production))
    engine = Engine(
        db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids,
        llm_subset_only=False,
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
    checklist_file: str | None = typer.Option(
        None, "--checklist-file", help="JSON file with the G3 audience checklist."
    ),
    reviewed_full_render: bool | None = typer.Option(
        None, "--reviewed-full-render", help="G3: I watched the whole render."
    ),
    made_for_kids: bool | None = typer.Option(
        None, "--made-for-kids/--not-made-for-kids",
        help="G3: audience declaration (required in the final profile).",
    ),
    ai_disclosure: bool = typer.Option(
        True, "--ai-disclosure/--no-ai-disclosure", help="G3: AI disclosure."
    ),
    third_party_ip: bool | None = typer.Option(
        None, "--third-party-ip/--no-third-party-ip",
        help="G3: third-party IP confirmed clear.",
    ),
    licenses: bool | None = typer.Option(
        None, "--licenses/--no-licenses", help="G3: every cue is licensed."
    ),
) -> None:
    """Approve a pending gate; the run continues.

    A G3 (final cut) approval must carry the audience checklist: either
    `--checklist-file checklist.json` or the individual flags. In the final
    profile every item is mandatory and machine-filled values are refused.
    """
    _, _, clock, ids, repos = _connect()
    dag = _dag_for_gate(repos, gate_id)
    service = GateService(_, repos, clock, dag, ids)
    checklist: dict[str, object] | None = None
    gate_row = repos.gates.get(gate_id)
    is_g3 = gate_row is not None and gate_row.gate == "G3_FINAL_CUT"
    if checklist_file is not None and not is_g3:
        _fail("--checklist-file only applies to a G3_FINAL_CUT gate")
    if checklist_file is not None:
        checklist_path = Path(checklist_file)
        try:
            loaded = json.loads(checklist_path.read_text(encoding="utf-8"))
        except OSError as exc:
            _fail(f"cannot read {checklist_path}: {exc}")
        except ValueError as exc:
            _fail(f"{checklist_path} is not valid JSON: {exc}")
        if not isinstance(loaded, dict):
            _fail(f"{checklist_path} must contain a JSON object")
        checklist = {str(k): v for k, v in loaded.items()}
    flag_values_given = any(
        value is not None
        for value in (
            reviewed_full_render,
            made_for_kids,
            third_party_ip,
            licenses,
        )
    )
    flag_values: dict[str, object] = {
        "reviewed_full_render": reviewed_full_render,
        "audience_made_for_kids": made_for_kids,
        "third_party_ip_confirmed": third_party_ip,
        "licenses_confirmed": licenses,
    }
    if is_g3:
        merged = dict(checklist or {})
        for key, value in flag_values.items():
            if value is not None:
                merged[key] = value
        disclosure_given = "--no-ai-disclosure" in sys.argv or _flag_present(
            "--ai-disclosure"
        )
        if disclosure_given:
            merged["ai_disclosure"] = ai_disclosure
        profile = "final"
        if gate_row is not None:
            run_row = repos.pipeline.get_run(gate_row.run_id)
            profile = run_row.profile if run_row is not None else "final"
        if (
            profile == "final"
            and checklist_file is None
            and not any(value is not None for value in flag_values.values())
            and not disclosure_given
        ):
            _fail(
                "a final G3 approval needs the audience checklist: pass "
                "--checklist-file checklist.json or the individual flags"
            )
        if profile != "final" and checklist_file is None and not flag_values_given:
            # Sandbox/draft: omitted items are stored as `"auto"` markers, which
            # S13 and the final profile both refuse later.
            checklist = None
        else:
            checklist = dict(merged)
    try:
        service.approve(gate_id, note, checklist=checklist)
    except StudioError as exc:
        _fail(str(exc))
    _hint = " --production" if _is_text_gate(repos, gate_id) else ""
    console.print(f"gate {gate_id} approved; next: studio worker --until-idle{_hint}")


@gates_app.command("reject")
def gates_reject(
    gate_id: str = typer.Argument(...),
    shot: str | None = typer.Option(None, "--shot", help="Shot id to reject."),
    shots: str | None = typer.Option(
        None, "--shots", help="Comma-separated shot numbers, e.g. 3,7 (G2)."
    ),
    all_shots: bool = typer.Option(False, "--all", help="Reject every shot (shows cost)."),
    prompt: str | None = typer.Option(None, "--prompt", help="Edited prompt for the next attempt."),
    note: str | None = typer.Option(None, "--note"),
    reason: str | None = typer.Option(None, "--reason", help="Alias of --note."),
    yes: bool = typer.Option(False, "--yes", help="Confirm the paid regeneration."),
    deliverable: str | None = typer.Option(
        None, "--deliverable", help="G3: reject this deliverable's render (free rebuild)."
    ),
) -> None:
    """Reject output at a gate; the stage re-executes.

    G2 accepts shot numbers (`--shots 3,7`) or `--all`; both show the expected
    keyframe cost and require `--yes`, because only those shots are re-paid.

    G3 takes `--deliverable KEY --reason ...`: it re-runs the *free* assembly
    and QC for that deliverable, so the next G3 round binds to a new render sha.
    """
    config, db, clock, ids, repos = _connect()
    dag = _dag_for_gate(repos, gate_id)
    service = GateService(db, repos, clock, dag, ids)
    note = note or reason
    rejections = []
    if shot:
        rejections.append(ShotRejection(shot_id=shot, edited_prompt=prompt))
    if shots or all_shots:
        gate = repos.gates.get(gate_id)
        run = repos.pipeline.get_run(gate.run_id) if gate is not None else None
        if gate is None or run is None or not run.episode_id:
            _fail(f"gate {gate_id} has no resolvable run/episode")
        targets = _reject_shot_rows(repos, run.episode_id, shots, all_shots)
        if not targets:
            _fail("no shots matched")
        engine = Engine(
            db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids,
            llm_subset_only=False,
        )
        from studio.pipeline.panel import gate_panel, keyframe_reject_cost

        ctx = engine.context_for(run.id)
        cost = keyframe_reject_cost(ctx, [row.id for row in targets])
        console.print(
            f"rejecting {len(targets)} shot(s): "
            + ", ".join(str(row.shot_no) for row in targets)
        )
        if cost["available"]:
            with_retries = musd_to_usd(int(cost["with_retries_musd"] or 0))
            first_pass = musd_to_usd(int(cost["first_pass_musd"] or 0))
            console.print(
                f"expected keyframe cost: {with_retries:.4f} USD "
                f"(first pass {first_pass:.4f} USD)"
            )
        else:
            console.print("[yellow]cost not computable offline for every shot[/yellow]")
        if not yes:
            _fail("rejecting shots re-pays their keyframes; re-run with --yes")
        rejections.extend(
            ShotRejection(shot_id=row.id, edited_prompt=prompt) for row in targets
        )
        _ = gate_panel  # panel is what `gates show` renders from the same context
    if deliverable is not None:
        gate = repos.gates.get(gate_id)
        if gate is None or gate.gate != "G3_FINAL_CUT":
            _fail("--deliverable only applies to a G3_FINAL_CUT gate")
        run = repos.pipeline.get_run(gate.run_id)
        if run is None:
            _fail(f"gate {gate_id} has no run")
        resolved = _deliverable_by_key(repos, run, deliverable)
        if resolved is None:
            _fail(f"unknown deliverable {deliverable!r} for run {run.id}")
        deliverable = resolved
        if not note:
            _fail("a G3 rejection needs --reason so the rebuild has a record")
    if not rejections and deliverable is None:
        _fail(
            "nothing to reject: pass --shot <id>, --shots 3,7, --all or --deliverable KEY"
        )
    try:
        service.request_changes(gate_id, note, rejections, deliverable_id=deliverable)
    except StudioError as exc:
        _fail(str(exc))
    _hint = " --production" if _is_text_gate(repos, gate_id) else ""
    console.print(
        f"gate {gate_id} marked changes_requested; next: studio worker --until-idle{_hint}"
    )


def _reject_shot_rows(  # type: ignore[no-untyped-def]
    repos, episode_id: str, shots: str | None, all_shots: bool
) -> list[Any]:
    """Resolve `--shots 3,7` (shot numbers) or `--all` for this episode."""
    tracks = repos.content.list_tracks(episode_id)
    rows = [shot for track in tracks for shot in repos.content.shots_for_track(track.id)]
    if all_shots:
        return rows
    wanted: set[int] = set()
    for chunk in (shots or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            wanted.add(int(chunk))
        except ValueError:
            _fail(f"--shots expects shot numbers, got {chunk!r}")
    missing = wanted - {row.shot_no for row in rows}
    if missing:
        _fail(f"unknown shot number(s): {', '.join(str(n) for n in sorted(missing))}")
    return [row for row in rows if row.shot_no in wanted]


# ======================================================================
# bible proposals (S14)
# ======================================================================


def _episode_run(repos: StudioRepos, episode_id: str) -> str | None:
    """The most recent run for an episode (a proposal needs a run context)."""
    runs = [run for run in repos.pipeline.list_runs() if run.episode_id == episode_id]
    return runs[-1].id if runs else None


@bible_app.command("proposals")
def bible_proposals_list(
    episode: str | None = typer.Option(None, "--episode", help="Filter by episode id."),
    status: str | None = typer.Option(None, "--status", help="pending|accepted|rejected|stale"),
) -> None:
    """List bible proposals (never applies anything)."""
    _config, db, _clock, _ids, repos = _connect()
    migrate(db)
    if episode is not None:
        rows = repos.proposals.list_for_episode(episode, status=status)
    else:
        sql = "SELECT * FROM bible_proposals"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at, rowid"
        rows = [
            _proposal_row(row) for row in db.connect().execute(sql, params).fetchall()
        ]
    table = Table(title="bible proposals")
    for column in ("id", "episode", "base", "status", "facts", "created"):
        table.add_column(column)
    for row in rows:
        payload = json.loads(row.proposal_json or "{}")
        facts = sum(len(value) for value in payload.values() if isinstance(value, list))
        table.add_row(
            row.id,
            row.episode_id,
            str(row.base_bible_version),
            row.status,
            str(facts),
            row.created_at,
        )
    console.print(table)


def _proposal_row(row: Any) -> Any:
    from studio.domain.models import BibleProposalRow

    return BibleProposalRow.model_validate(dict(row))


@bible_app.command("show")
def bible_proposals_show(
    proposal_id: str = typer.Argument(..., help="Proposal id."),
) -> None:
    """Print one proposal, with its evidence, as stored."""
    _config, db, _clock, _ids, repos = _connect()
    migrate(db)
    row = repos.proposals.get(proposal_id)
    if row is None:
        _fail(f"unknown proposal {proposal_id}")
    console.print(
        f"[bold]proposal[/bold] {row.id}  status={row.status}  "
        f"base_bible_version={row.base_bible_version}  episode={row.episode_id}"
    )
    console.print(row.proposal_json)


@bible_app.command("accept")
def bible_proposals_accept(
    proposal_id: str = typer.Argument(..., help="Proposal id."),
    note: str = typer.Option("", "--note", help="Why it was accepted (audited)."),
) -> None:
    """Apply a pending proposal as a new bible version (refuses a stale base)."""
    from studio.bible.proposal import accept_proposal
    from studio.domain.errors import ProposalStale

    config, db, clock, ids, repos = _connect()
    migrate(db)
    row = repos.proposals.get(proposal_id)
    if row is None:
        _fail(f"unknown proposal {proposal_id}")
    run_id = _episode_run(repos, row.episode_id)
    if run_id is None:
        _fail(f"episode {row.episode_id} has no run; cannot build a context")
    ctx = _run_context(config, db, clock, ids, repos, run_id)
    try:
        result = accept_proposal(ctx, proposal_id, actor=f"operator:{note[:80]}")
    except ProposalStale as exc:
        _fail(str(exc))
    except StudioError as exc:
        _fail(str(exc))
    console.print(
        f"accepted {result['proposal_id']}: bible is now v{result['version']}"
        f" ({'new version' if result['created'] else 'unchanged content'})"
    )


@bible_app.command("reject")
def bible_proposals_reject(
    proposal_id: str = typer.Argument(..., help="Proposal id."),
    reason: str = typer.Option("", "--reason", help="Why it was rejected (audited)."),
) -> None:
    """Reject a pending proposal; the bible is untouched."""
    from studio.bible.proposal import reject_proposal

    config, db, clock, ids, repos = _connect()
    migrate(db)
    row = repos.proposals.get(proposal_id)
    if row is None:
        _fail(f"unknown proposal {proposal_id}")
    run_id = _episode_run(repos, row.episode_id)
    ctx = _run_context(config, db, clock, ids, repos, run_id or "")
    result = reject_proposal(ctx, proposal_id, reason=reason)
    console.print(f"rejected {result['proposal_id']}")


@bible_app.command("propose")
def bible_propose(
    episode: str = typer.Argument(..., help="Episode id."),
    regenerate: bool = typer.Option(
        False, "--regenerate", help="Spend on a fresh call instead of the cached one."
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirmed: this action spends money."),
) -> None:
    """Ask the model for one bible proposal (paid; never auto-applied)."""
    from studio.bible.proposal import estimate_cost, propose

    config, db, clock, ids, repos = _connect()
    migrate(db)
    if repos.content.get_episode(episode) is None:
        _fail(f"unknown episode {episode}")
    run_id = _episode_run(repos, episode)
    if run_id is None:
        _fail(f"episode {episode} has no run; start one before proposing")
    ctx = _run_context(config, db, clock, ids, repos, run_id)
    musd, detail = estimate_cost(ctx)
    console.print(f"bible proposal: {detail}")
    cached_note = "" if regenerate else " (a resume of the same nonce is a cache hit)"
    console.print(f"worst-case cost: ${musd_to_usd(musd):.4f}{cached_note}")
    if not yes:
        _fail("aborted (nothing spent) — re-run with --yes to proceed")
    result = _run_async(propose(ctx, regenerate=regenerate))
    console.print(
        f"proposal {result['proposal_id']} ({result['facts']} facts, "
        f"base v{result['base_bible_version']}) — pending operator review"
    )


def _run_context(
    config: AppConfig,
    db: Database,
    clock: SystemClock,
    ids: UlidIds,
    repos: StudioRepos,
    run_id: str,
) -> Any:
    """A RunContext for a CLI action that needs the gateway (never a stage run)."""
    engine = Engine(
        db=db, repos=repos, dag=_dag_for_run(repos, run_id), clock=clock,
        config=config, ids=ids, llm_subset_only=False,
    )
    return engine.context_for(run_id)


# ======================================================================
# publish marker + purge (S15)
# ======================================================================


@publish_app.command("mark")
def publish_mark(
    deliverable_id: str = typer.Argument(..., help="Deliverable id."),
    url: str = typer.Option(..., "--url", help="Public URL the deliverable was published to."),
    published_at: str | None = typer.Option(
        None, "--published-at", help="ISO-8601 timestamp (default: now)."
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirmed: this writes the published state."),
) -> None:
    """Record that a deliverable was published (needs a prior export)."""
    from studio.retention.publish import mark_published

    _config, db, clock, _ids, repos = _connect()
    migrate(db)
    if not yes:
        _fail("aborted (nothing written) — re-run with --yes to record the publication")
    try:
        result = mark_published(
            repos, deliverable_id=deliverable_id, url=url, published_at=published_at, clock=clock
        )
    except StudioError as exc:
        _fail(str(exc))
    console.print(
        f"published {result['deliverable_id']} at {result['published_at']} -> {result['url']}"
    )
    if result["episode_published"]:
        console.print(
            f"[green]episode {result['episode_id']} is now published[/green]: "
            "paid stages are closed, free stages (a re-export) still run"
        )


def _purge_plan_table(plan: Any) -> Table:
    table = Table(title=f"purge plan — episode {plan.episode_id}")
    table.add_column("class")
    table.add_column("count", justify="right")
    table.add_column("bytes", justify="right")
    table.add_column("rule")
    for row in plan.summary():
        table.add_row(
            str(row["class"]),
            str(row["count"]),
            f"{int(row['bytes']):,}",
            "; ".join(str(rule) for rule in row["rules"])[:120],
        )
    table.add_row("[bold]total[/bold]", str(len(plan.items)), f"{plan.total_bytes:,}", "")
    return table


@purge_app.command("plan")
def purge_plan_cmd(
    episode: str = typer.Option(..., "--episode", help="Episode id."),
) -> None:
    """Show what a purge would delete for one episode. Deletes nothing."""
    from studio.retention.plan import build_plan

    config, db, clock, _ids, repos = _connect()
    migrate(db)
    try:
        plan = build_plan(repos, db, episode_id=episode, settings=config.settings, clock=clock)
    except StudioError as exc:
        _fail(str(exc))
    console.print(_purge_plan_table(plan))
    reasons = ", ".join(
        f"{name}={count}" for name, count in sorted(plan.protected_reasons.items())
    )
    console.print(f"protected: {plan.protected} asset(s) ({reasons or 'none'})")


@purge_app.command("run")
def purge_run_cmd(
    episode: str = typer.Option(..., "--episode", help="Episode id."),
    yes: bool = typer.Option(False, "--yes", help="Confirmed: this deletes files."),
    allow_repay_purged: bool = typer.Option(
        False,
        "--allow-repay-purged",
        help="Allow a future provider call to re-pay for a purged asset (audited).",
    ),
) -> None:
    """Delete a published episode's purgeable assets (dry run unless --yes)."""
    from studio.retention.plan import build_plan
    from studio.retention.run import run_purge

    config, db, clock, _ids, repos = _connect()
    migrate(db)
    try:
        plan = build_plan(repos, db, episode_id=episode, settings=config.settings, clock=clock)
    except StudioError as exc:
        _fail(str(exc))
    console.print(_purge_plan_table(plan))
    if not yes:
        result = run_purge(
            repos, db, episode_id=episode, settings=config.settings, clock=clock, dry_run=True
        )
        console.print(
            f"dry run: {len(result.deleted)} file(s), {result.deleted_bytes:,} bytes "
            "would be deleted — re-run with --yes to delete"
        )
        return
    if allow_repay_purged:
        repos.audit.append(
            actor="operator",
            entity=f"episode:{episode}",
            event="s15_purge_allow_repay",
            payload={"note": "--allow-repay-purged acknowledged at purge time"},
        )
    try:
        result = run_purge(
            repos, db, episode_id=episode, settings=config.settings, clock=clock, dry_run=False
        )
    except StudioError as exc:
        _fail(str(exc))
    console.print(
        f"deleted {len(result.deleted)} file(s), {result.deleted_bytes:,} bytes "
        f"({', '.join(sorted({item.purge_class for item in result.deleted})) or 'nothing'})"
    )
    if result.newly_protected:
        console.print(
            f"[yellow]skipped {len(result.newly_protected)} asset(s) that became "
            "protected while the plan was reviewed[/yellow]"
        )
    if result.missing:
        console.print(f"already gone: {len(result.missing)} file(s) (resumable)")
    if not result.ok:
        console.print(
            f"[red]locked, skipped: {len(result.skipped_locked)} file(s)[/red] "
            "(a held handle; close the app and re-run)"
        )
        raise typer.Exit(code=1)


@purge_app.command("gc")
def purge_gc_cmd(
    yes: bool = typer.Option(False, "--yes", help="Confirmed: this deletes orphans."),
) -> None:
    """Remove CAS orphans, data/tmp scratch and stale .tmp-* export directories."""
    from studio.retention.run import run_gc

    config, db, clock, _ids, repos = _connect()
    migrate(db)
    result = run_gc(
        repos, db, settings=config.settings, clock=clock, dry_run=not yes
    )
    table = Table(title="purge gc" + (" (dry run)" if not yes else ""))
    table.add_column("class")
    table.add_column("candidates", justify="right")
    table.add_column("removed", justify="right")
    for key in result["candidates"]:
        table.add_row(key, str(result["candidates"][key]), str(result["removed"][key]))
    console.print(table)
    if not yes:
        console.print("dry run: nothing deleted — re-run with --yes to delete")


@app.command()
def export(
    run_id: str = typer.Argument(..., help="Run id."),
    deliverable: str | None = typer.Option(
        None, "--deliverable", help="Deliverable key (id, language or channel)."
    ),
    override_commercial_use: bool = typer.Option(
        False, "--override-commercial-use",
        help="Write the export despite unverified commercial use (audited).",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Why the override exists."),
) -> None:
    """Export every approved render of a run (free: no provider, no network).

    Final-profile runs need G1, G0, G2 and G3 all approved against the current
    snapshots; a draft/sandbox run writes under `export.draft_root` and records
    the same blockers as `export_flags` in the sidecar.
    """
    if override_commercial_use and not (reason or "").strip():
        _fail("--override-commercial-use requires --reason (it is audited)")
    config, db, clock, ids, repos = _connect()
    migrate(db)
    run = repos.pipeline.get_run(run_id)
    if run is None:
        _fail(f"unknown run {run_id}")
    target: str | None = None
    if deliverable is not None:
        target = _deliverable_by_key(repos, run, deliverable)
        if target is None:
            _fail(f"unknown deliverable {deliverable!r} for run {run_id}")
    imports = _export_imports()
    engine = Engine(
        db=db, repos=repos, dag=_dag_for_run(repos, run_id), clock=clock,
        config=config, ids=ids, llm_subset_only=False,
    )
    ctx = engine.context_for(run_id)
    handler = imports["ExportHandler"](
        override_commercial_use=override_commercial_use,
        override_reason=reason if override_commercial_use else None,
    )
    subjects = handler.subjects(ctx)
    if target is not None:
        subjects = [subject for subject in subjects if subject.id == target]
    if not subjects:
        _fail("no deliverable to export for this run")
    failures = 0
    table = Table(title=f"export — {run_id}")
    table.add_column("deliverable")
    table.add_column("profile")
    table.add_column("directory")
    table.add_column("flags")
    for subject in subjects:
        try:
            output = _run_async(handler.run(ctx, subject)).output
        except StudioError as exc:
            failures += 1
            console.print(f"[red]{subject.id}[/red]: {exc}")
            continue
        flags = output.get("export_flags") or []
        table.add_row(
            subject.id,
            str(output.get("profile")),
            str(output.get("dir_path")),
            ", ".join(str(flag.get("code")) for flag in flags) or "-",
        )
    console.print(table)
    if failures:
        raise typer.Exit(code=1)


def _export_imports() -> dict[str, Any]:
    """Lazy import so `--help` stays fast and cycles stay out of module import."""
    from studio.export.stage import ExportHandler

    return {"ExportHandler": ExportHandler}


@render_app.command("rebuild")
def render_rebuild(
    run_id: str = typer.Argument(..., help="Run id."),
    deliverable: str | None = typer.Option(
        None, "--deliverable", help="Deliverable key (id, language or channel)."
    ),
) -> None:
    """Invalidate S11 + S12 so the free assembly and render QC run again.

    Only the render side is invalidated: conformed clips, the audio master and
    every gate-input upstream of S11 stay intact, and no provider is called.
    """
    _, db, _clock, _ids, repos = _connect()
    migrate(db)
    run = repos.pipeline.get_run(run_id)
    if run is None:
        _fail(f"unknown run {run_id}")
    target: str | None = None
    if deliverable is not None:
        target = _deliverable_by_key(repos, run, deliverable)
        if target is None:
            _fail(f"unknown deliverable {deliverable!r} for run {run_id}")
    stage_run_ids = _render_rows(repos, run_id, target)
    if not stage_run_ids:
        _fail("no S11/S12 stage runs to invalidate")
    repos.pipeline.mark_stale(stage_run_ids)
    repos.pipeline.set_run_status(run_id, "running")
    repos.jobs.enqueue(
        kind="advance_run",
        payload={"run_id": run_id},
        dedupe_key=f"advance:{run_id}",
    )
    scope = target or "every deliverable"
    console.print(
        f"staled {len(stage_run_ids)} stage run(s) for {scope}; "
        "next: studio worker --until-idle (free)"
    )


@gates_app.command("show")
def gates_show(
    gate_id: str = typer.Argument(...),
    as_json: bool = typer.Option(False, "--json", help="Print the panel as JSON."),
) -> None:
    """Show the gate panel: estimates, spend, clip projection, caps, verdict."""
    from studio.pipeline.panel import gate_panel

    config, db, clock, ids, repos = _connect()
    gate = repos.gates.get(gate_id)
    if gate is None:
        _fail(f"unknown gate {gate_id}")
    dag = _dag_for_run(repos, gate.run_id)
    engine = Engine(
        db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids,
        llm_subset_only=False,
    )
    try:
        panel = gate_panel(engine.context_for(gate.run_id), gate_id)
    except StudioError as exc:
        _fail(str(exc))
    if as_json:
        console.print_json(json.dumps(panel, ensure_ascii=False, sort_keys=True))
        return
    verdict = str(panel["verdict"])
    colour = {"ok": "green", "over_remaining_cap": "red"}.get(verdict, "yellow")
    console.print(
        f"gate [bold]{gate_id}[/bold] · {gate.gate} on {gate.stage} · round {gate.round}"
        f" · status {gate.status} · verdict [{colour}]{verdict}[/{colour}]"
    )
    estimates = panel.get("estimates") or []
    if estimates:
        table = Table(title="S4 media estimate (USD, low / expected / high)")
        for column in ("deliverable", "tts", "sheets", "keyframes", "video"):
            table.add_column(column)
        for entry in estimates:
            buckets = entry["buckets"]
            table.add_row(
                str(entry["deliverable_id"])[:12],
                *[
                    "{:.4f} / {:.4f} / {:.4f}".format(
                        musd_to_usd(int(buckets[name]["low_musd"] or 0)),
                        musd_to_usd(int(buckets[name]["expected_musd"] or 0)),
                        musd_to_usd(int(buckets[name]["high_musd"] or 0)),
                    )
                    for name in ("tts", "sheets", "keyframes", "video")
                ],
            )
        console.print(table)
    elif not panel.get("estimates_available", False):
        console.print("[yellow]media estimate unavailable (unconfirmed model or price)[/yellow]")
    spend = panel.get("spend") or {}
    console.print(
        f"spent to date: {musd_to_usd(int(spend.get('total_musd') or 0)):.4f} USD"
    )
    for row in spend.get("by_model") or []:
        console.print(
            f"  {row['capability']:<10} {row['model_key']:<28} calls={row['calls']:<4}"
            f" spent={musd_to_usd(int(row['spent_musd'])):.4f}"
            f" est={musd_to_usd(int(row['est_musd'])):.4f}"
        )
    projection = panel.get("clip_projection") or {}
    console.print(
        "clip projection: "
        + (
            "not computable (locked durations or price missing)"
            if not projection.get("available")
            else f"first pass {musd_to_usd(int(projection['first_pass_musd'] or 0)):.4f} USD,"
            f" with retries {musd_to_usd(int(projection['with_retries_musd'] or 0)):.4f} USD"
        )
    )
    for scope, cap in (panel.get("caps") or {}).items():
        remaining = cap.get("remaining_musd")
        cap_text = (
            "none"
            if cap.get("cap_musd") is None
            else f"{musd_to_usd(int(cap['cap_musd'])):.4f}"
        )
        console.print(
            f"  {scope:<8} cap="
            + cap_text
            + " remaining="
            + ("n/a" if remaining is None else f"{musd_to_usd(int(remaining)):.4f}")
        )
    for entry in panel.get("drift") or []:
        flag = "[yellow]WARN[/yellow]" if entry["warn"] else "ok"
        console.print(
            f"  drift {entry['bucket']}: {entry['drift_pct']:+.1f}% vs S4 baseline ({flag})"
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
        from studio.pipeline.specs import production_specs

        return production_specs()
    return _demo_specs_light()


def _dag_for_run(repos: StudioRepos, run_id: str) -> Dag:
    """Pick the text DAG when the run contains text stages, else the demo DAG."""
    try:
        rows = repos.pipeline.list_stage_runs(run_id)
    except Exception:  # diagnostics only; never block the operator
        rows = []
    demo = _demo_specs_light()
    demo_names = {spec.name for spec in demo}
    if rows and all(row.stage in demo_names for row in rows):
        return Dag(demo)
    from studio.pipeline.specs import production_specs

    return Dag(production_specs())


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
        from studio.pipeline.specs import production_specs

        return Dag(production_specs())
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



# ==================================================================
# shot / media (Phase 4, §N)
# ==================================================================
# ==================================================================
# shot / media (Phase 4, §N)
# ==================================================================

shot_app = typer.Typer(help="Shot inspection and regeneration (Phase 4).")
media_app = typer.Typer(help="Media outputs: preview, QC, audition, smoke (Phase 4).")
app.add_typer(shot_app, name="shot")
app.add_typer(media_app, name="media")

_SHOT_MEDIA_STAGES = {"keyframe": "s8_keyframes", "clip": "s10_clips"}


@shot_app.command("list")
def shot_list(run_id: str = typer.Argument(..., help="Run id.")) -> None:
    """Per shot: attempt history, QC status/warnings, sanitize level, cost."""
    _, db, _clock, _ids, repos = _connect()
    run = repos.pipeline.get_run(run_id)
    if run is None:
        _fail(f"unknown run {run_id}")
    episode_id = run.episode_id
    if not episode_id:
        _fail(f"run {run_id} has no episode")
    tracks = {track.id: track for track in repos.content.list_tracks(episode_id)}
    table = Table(title=f"shots — run {run_id}")
    for column in (
        "shot", "aspect", "kind", "attempt", "qc", "sanitize", "cost_usd", "qc_warns", "prompt",
    ):
        table.add_column(column)
    rows = db.connect().execute(
        "SELECT shot_id, kind, attempt_no, qc_status, sanitize_level, cost_musd, qc_json "
        "FROM shot_attempts ORDER BY shot_id, kind, attempt_no"
    ).fetchall()
    attempts_by_shot: dict[str, list[Any]] = {}
    for row in rows:
        attempts_by_shot.setdefault(str(row["shot_id"]), []).append(row)
    shots = sorted(
        repos.content.list_shots(episode_id), key=lambda shot: (shot.track_id, shot.shot_no)
    )
    for shot in shots:
        track = tracks.get(shot.track_id)
        aspect = track.aspect if track is not None else "?"
        attempts = attempts_by_shot.get(shot.id, [])
        if not attempts:
            table.add_row(
                str(shot.shot_no), aspect, "-", "-", "-", "-", "-", "-", _truncate(shot.prompt, 40)
            )
            continue
        for attempt in attempts:
            try:
                qc = json.loads(attempt["qc_json"] or "[]")
            except json.JSONDecodeError:
                qc = []
            warns = sum(
                1 for check in qc if isinstance(check, dict) and check.get("status") == "warn"
            )
            table.add_row(
                str(shot.shot_no),
                aspect,
                str(attempt["kind"]),
                str(attempt["attempt_no"]),
                str(attempt["qc_status"]),
                str(attempt["sanitize_level"]),
                f"{musd_to_usd(int(attempt['cost_musd'])):.4f}",
                str(warns),
                _truncate(shot.prompt, 40),
            )
    console.print(table)


def _aspect_of(value: str) -> Any:
    from studio.domain.enums import Aspect

    try:
        return Aspect(value)
    except ValueError as exc:
        raise StudioError(f"unknown aspect {value!r}") from exc


def _worst_case_reserve(
    entry: Any, *, kind: str, aspect: Any, clip_duration_ms: int | None
) -> int | None:
    from decimal import Decimal

    from studio.config.registry import PerImage, PerMegapixel, PerSecond, PerVideo
    from studio.media.stages import _pixel_dims_for

    pricing = entry.pricing
    if kind == "keyframe":
        if isinstance(pricing, PerImage):
            return usd_to_musd(pricing.usd)
        if isinstance(pricing, PerVideo):
            return usd_to_musd(pricing.usd)
        if isinstance(pricing, PerMegapixel):
            dims = _pixel_dims_for(entry, aspect)
            if dims is None:
                return None
            width, height = dims
            return usd_to_musd(
                (Decimal(width) * Decimal(height) / Decimal(1_000_000)) * pricing.usd
            )
        return None
    locked_ms = int(clip_duration_ms or 5000)
    if isinstance(pricing, PerSecond):
        return usd_to_musd(Decimal(locked_ms) / Decimal(1000) * pricing.usd)
    if isinstance(pricing, PerVideo):
        return usd_to_musd(Decimal(1) * pricing.usd)
    return None


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


@shot_app.command("regenerate")
def shot_regenerate(
    run_id: str = typer.Argument(..., help="Run id."),
    shot_id: str = typer.Argument(..., help="Shot id."),
    stage: str = typer.Option(..., "--stage", help="keyframe | clip"),
    prompt_file: str | None = typer.Option(
        None, "--prompt-file", help="UTF-8 file with the edited prompt for the shot."
    ),
    reason: str | None = typer.Option(None, "--reason", help="Operator note for the audit log."),
    yes: bool = typer.Option(False, "--yes", help="Proceed (required to spend)."),
) -> None:
    """Regenerate one shot's keyframe or clip.

    Creates a fresh attempt with a new nonce (resetting the nonce-based
    regeneration counter), stales only that shot's keyframe/clip stage rows
    and their downstream dependents (never purging stored raw assets), shows
    the worst-case reserve cost first, and refuses without --yes. A
    completed/failed/halted run is reopened (same semantics as `gates
    request-changes`); the rest of the run stays as it was.
    """
    from studio.db.repos.pipeline import subject_from_stage_run
    from studio.domain.refs import subjects_related
    from studio.media.registry import MediaRole, media_model_entry, model_confirmation_problems
    from studio.pipeline.gates import build_shot_anchor

    stage = stage.strip().lower()
    if stage not in _SHOT_MEDIA_STAGES:
        _fail(f"--stage must be one of {sorted(_SHOT_MEDIA_STAGES)}")
    kind = "keyframe" if stage == "keyframe" else "clip"

    config, _db, _clock, ids, repos = _connect()
    run = repos.pipeline.get_run(run_id)
    if run is None:
        _fail(f"unknown run {run_id}")
    reopen_from: str | None = None
    if run.status in (
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.HALTED_BUDGET.value,
    ):
        reopen_from = run.status
    shot = repos.content.get_shot(shot_id)
    if shot is None:
        _fail(f"unknown shot {shot_id}")
    track = repos.content.get_track(shot.track_id)
    if track is None:
        _fail(f"shot {shot_id} references missing track {shot.track_id}")
    episode = repos.content.get_episode(track.episode_id)
    assert episode is not None
    if run.episode_id != episode.id:
        _fail(f"shot {shot_id} does not belong to run {run_id}'s episode")

    prompt_edit: str | None = None
    if prompt_file is not None:
        prompt_path = Path(prompt_file)
        if not prompt_path.is_file():
            _fail(f"prompt file not found: {prompt_path}")
        try:
            prompt_edit = prompt_path.read_bytes().decode("utf-8").strip()
        except UnicodeDecodeError:
            _fail(f"{prompt_path} is not UTF-8")
        if not prompt_edit:
            _fail(f"{prompt_path} contains no non-empty prompt")

    # Resolve the model + worst-case reserve cost before any spend.
    resolved_profile = config.registry.resolve_profile(run.profile)
    role: MediaRole = "keyframes" if kind == "keyframe" else "clips"
    try:
        model_key, entry = media_model_entry(config, resolved_profile, role)
    except StudioError as exc:
        _fail(str(exc))
    aspect_enum = _aspect_of(track.aspect)
    try:
        reserve_musd: int | None = _worst_case_reserve(
            entry, kind=kind, aspect=aspect_enum, clip_duration_ms=shot.clip_duration_ms
        )
    except StudioError as exc:
        _fail(f"cannot estimate reserve cost for {model_key!r}: {exc}")
    if reserve_musd is None:
        reserve_musd = 0
        console.print(
            "[yellow]worst case not computable offline for this model (unconfirmed "
            "price/dims); reserving nothing — the gateway still enforces the cap[/yellow]"
        )
    console.print(f"model: {model_key} ({entry.provider})")
    console.print(f"worst-case reserve for one {kind}: ${musd_to_usd(reserve_musd):.4f}")
    problems = model_confirmation_problems(entry, final=Profile(run.profile) is Profile.FINAL)
    if problems:
        listing = "; ".join(problems)
        console.print(
            f"[yellow]warning: model entry is not trusted with money: {listing} — "
            f"the paid stages will refuse this model until the operator attests it[/yellow]"
        )

    if not yes:
        _fail("aborted (nothing spent) — re-run with --yes to proceed")

    anchor = build_shot_anchor(repos, shot.id)
    current_attempt = repos.content.current_shot_attempt(shot.id, kind=kind)
    base_prompt = current_attempt.prompt_used if current_attempt is not None else shot.prompt
    new_prompt = prompt_edit if prompt_edit is not None else base_prompt

    attempt_no = repos.content.next_shot_attempt_no(shot.id)
    attempt = repos.content.create_shot_attempt(
        shot_id=shot.id,
        attempt_no=attempt_no,
        nonce=ids.new(),
        prompt_used=new_prompt,
        kind=kind,
    )

    dag = _dag_for_run(repos, run_id)
    seeds = {_SHOT_MEDIA_STAGES[kind]}
    stages_to_stale = set(seeds)
    for seed in seeds:
        stages_to_stale |= dag.downstream(seed)
    affected = [
        row.id
        for row in repos.pipeline.list_stage_runs(run_id)
        if row.stage in stages_to_stale
        and row.status in ("succeeded", "pending", "running", "stale", "failed")
        and subjects_related(anchor, subject_from_stage_run(row))
    ]
    # H5.4: a `failed` row (e.g. failed_policy) cannot be staled by mark_stale
    # (SQL guard) and would keep the stage/run failed forever. Reset it with a
    # cleared input hash so the fresh attempt is evaluated from scratch.
    failed_rows = [row_id for row_id in affected if _stage_row_is_failed(repos, row_id)]
    for row_id in failed_rows:
        repos.pipeline.set_stage_run(row_id, status="pending", clear_input_hash=True)
    staled = [row_id for row_id in affected if row_id not in set(failed_rows)]
    if staled:
        repos.pipeline.mark_stale(staled)

    if reopen_from is not None:
        repos.pipeline.set_run_status(run_id, RunStatus.RUNNING.value)
        assert episode is not None
        repos.content.set_episode_status(episode.id, RunStatus.RUNNING.value)

    payload: dict[str, Any] = {
        "run_id": run_id,
        "shot_id": shot.id,
        "track_id": track.id,
        "stage": kind,
        "attempt_id": attempt.id,
        "attempt_no": attempt_no,
        "model_key": model_key,
        "reserve_cost_musd": reserve_musd,
        "reason": reason,
        "prompt_edited": prompt_edit is not None,
        "staled_stage_runs": sorted(affected),
        "per_round_counters_reset": True,
        "reopened_from_status": reopen_from,
    }
    if prompt_edit is not None:
        payload["prompt_sha256_old"] = hashlib.sha256(base_prompt.encode("utf-8")).hexdigest()
        payload["prompt_sha256_new"] = hashlib.sha256(new_prompt.encode("utf-8")).hexdigest()
    repos.audit.append(
        actor="operator",
        entity=f"shot:{shot.id}",
        event="shot_regenerate_requested",
        payload=payload,
    )
    repos.jobs.enqueue(
        kind="advance_run",
        payload={"run_id": run_id},
        dedupe_key=f"advance:{run_id}",
    )
    console.print(
        f"shot {shot_id} {kind} regenerate: attempt {attempt_no} created "
        f"({len(affected)} stage run(s) staled)"
    )
    console.print("run `studio worker --until-idle` to regenerate")


def _stage_row_is_failed(repos: Any, stage_run_id: str) -> bool:
    row = repos.pipeline.get_stage_run(stage_run_id)
    return row is not None and row.status == "failed"


def _cas(config: AppConfig, db: Database, clock: SystemClock) -> Any:
    from studio.core.cas import ContentStore

    return ContentStore(Path(config.settings.paths.cas), db, clock)


def _escape(value: Any) -> str:
    import html

    return html.escape(str(value), quote=True)


def _copy_for_preview(outdir: Path, cas, sha: str | None) -> str | None:  # type: ignore[no-untyped-def]
    """Copy a CAS asset into <outdir>/assets/ and return the relative POSIX link."""
    if not sha:
        return None
    import shutil

    try:
        source = cas.resolve(sha)
    except Exception:
        return None
    assets_dir = outdir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix or ".bin"
    target = assets_dir / f"{sha[:16]}{suffix}"
    if not target.exists():
        shutil.copy2(source, target)
    return target.relative_to(outdir).as_posix()


_QC_STYLE = {"pass": "#2e7d32", "warn": "#b45309", "fail": "#b91c1c"}


def _render_qc_rows(qc: list[dict[str, Any]]) -> str:
    if not qc:
        return '<p class="muted">no QC recorded</p>'
    rows = []
    for check in qc:
        if not isinstance(check, dict):
            continue
        name = _escape(check.get("name", "?"))
        status = _escape(check.get("status", "?"))
        value = _escape(check.get("value"))
        threshold = _escape(check.get("threshold"))
        color = _QC_STYLE.get(str(check.get("status")), "#555")
        rows.append(
            f'<tr><td>{name}</td>'
            f'<td style="color:{color};font-weight:600">{status}</td>'
            f"<td>{value}</td><td>{threshold}</td></tr>"
        )
    return (
        '<table class="qc"><thead><tr><th>check</th><th>status</th>'
        "<th>value</th><th>threshold</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _preview_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ar" dir="auto">
<head>
<meta charset="utf-8">
<title>{_escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 2rem; }}
h1, h2 {{ color: #f8fafc; }}
.card {{ background: #1e293b; border-radius: 12px; padding: 1rem 1.25rem; margin: 1rem 0; }}
.card img, .card video {{ max-width: 320px; border-radius: 8px; border: 1px solid #334155; }}
table {{ border-collapse: collapse; margin: 0.5rem 0; }}
td, th {{ border: 1px solid #334155; padding: 0.25rem 0.6rem; font-size: 0.85rem; }}
.muted {{ color: #94a3b8; }}
code {{ background: #0b1220; padding: 0.1rem 0.3rem; border-radius: 4px; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _render_animatic(  # type: ignore[no-untyped-def]
    db, repos, cas, outdir: Path, run_id: str, config, clock, ids
) -> str:
    """§G5: <video> per deliverable, a shot table and the frozen G2 panel.

    Every string is HTML-escaped and narration text carries dir="auto".
    """
    from studio.pipeline.panel import gate_panel

    rows = [
        row
        for row in repos.pipeline.list_stage_runs(run_id, stage="s9_animatic")
        if row.status == "succeeded" and row.output_json
    ]
    if not rows:
        return '<p class="muted">no animatic yet: run S9 (studio worker --until-idle)</p>'
    engine = Engine(
        db=db, repos=repos, dag=_dag_for_run(repos, run_id), clock=clock,
        config=config, ids=ids, llm_subset_only=False,
    )
    ctx = engine.context_for(run_id)
    gate = repos.gates.latest(run_id, "s9_animatic")
    panel_html = ""
    if gate is not None:
        try:
            panel = gate_panel(ctx, gate.id)
        except StudioError:
            panel = None
        if panel is not None:
            panel_html = (
                '<h2>gate panel</h2><table><tr><th>verdict</th><td dir="auto">'
                f"{_escape(str(panel['verdict']))}</td></tr>"
                "<tr><th>spent (USD)</th><td>"
                f"{musd_to_usd(int(panel['spend']['total_musd'])):.4f}</td></tr>"
                f"<tr><th>clip projection</th><td dir=\"auto\">"
                f"{_escape(json.dumps(panel['clip_projection'], ensure_ascii=False))}</td></tr>"
                "</table>"
            )
    parts: list[str] = [panel_html]
    for row in rows:
        output = json.loads(row.output_json or "{}")
        sha = str(output.get("animatic_sha256") or "")
        if not sha:
            continue
        source = cas.resolve(sha)
        target = outdir / f"animatic-{sha[:12]}.mp4"
        if not target.exists():
            shutil.copy2(source, target)
        frames = output.get("frames")
        shots = output.get("shots") or []
        parts.append(
            f"<h2>deliverable <code>{_escape(str(output.get('deliverable_id')))}</code>"
            f" · {frames} frames · canvas {output.get('canvas')}</h2>"
            f'<video controls preload="metadata" width="360" '
            f'src="{_escape(target.name)}"></video>'
            '<table id="shots"><tr><th>#</th><th>shot</th><th>start</th>'
            "<th>frames</th><th>narration</th></tr>"
        )
        for shot in shots:
            narration = " ".join(
                str(line.get("text") or line.get("line_id") or "")
                for line in shot.get("narration") or []
            )
            parts.append(
                f'<tr data-start="{_escape(str(shot.get("start_ms")))}">'
                f'<td>{_escape(str(shot.get("index")))}</td>'
                f'<td><code>{_escape(str(shot.get("shot_id")))}</code></td>'
                f'<td>{_escape(str(shot.get("start_ms")))} ms</td>'
                f'<td>{_escape(str(shot.get("end_frame", 0)))}</td>'
                f'<td dir="auto">{_escape(narration)}</td></tr>'
            )
        parts.append("</table>")
    parts.append(
        "<script>"
        "document.querySelectorAll('video').forEach(function(v){"
        "v.addEventListener('timeupdate',function(){"
        "var t=v.currentTime*1000;var rows=document.querySelectorAll('#shots tr[data-start]');"
        "rows.forEach(function(r){r.classList.toggle('current',"
        "parseFloat(r.dataset.start)<=t && t<parseFloat(r.dataset.start)+4000);});"
        "});});"
        "</script>"
    )
    return "\n".join(parts)

def _render_render_preview(  # type: ignore[no-untyped-def]
    repos, cas, outdir: Path, run_id: str
) -> str:
    """§J: <video> per deliverable plus the measured S12 QC table."""
    rows = [
        row
        for row in repos.pipeline.list_stage_runs(run_id, stage="s12_render_qc")
        if row.status == "succeeded" and row.output_json
    ]
    if not rows:
        return '<p class="muted">no render QC yet: run S12 (studio worker --until-idle)</p>'
    parts: list[str] = []
    for row in rows:
        output = json.loads(row.output_json or "{}")
        link = _copy_for_preview(outdir, cas, output.get("render_sha256"))
        qc_link = _copy_for_preview(outdir, cas, output.get("qc_report_sha256"))
        preset = output.get("preset") or {}
        parts.append(
            f"<h2>deliverable <code>{_escape(str(output.get('deliverable_id')))}</code>"
            f" · {output.get('frames')} frames · {output.get('fps')} fps · "
            f"{_escape(str(preset.get('width')))}x{_escape(str(preset.get('height')))}"
            " · QC <strong style=\"color:"
            f"{_QC_STYLE.get(str(output.get('qc_status')), '#333')}\">"
            f"{_escape(str(output.get('qc_status')))}</strong></h2>"
        )
        if link:
            parts.append(
                '<video controls preload="metadata" width="360" '
                f'src="{_escape(link)}"></video>'
            )
        parts.append(
            "<table><tr><th>check</th><th>status</th></tr>"
            + "".join(
                f"<tr><td>{_escape(str(check.get('name')))}</td><td style=\"color:"
                f"{_QC_STYLE.get(str(check.get('status')), '#333')}\">"
                f"{_escape(str(check.get('status')))}</td></tr>"
                for check in output.get("checks") or []
                if isinstance(check, dict)
            )
            + "</table>"
        )
        if qc_link:
            parts.append(
                f'<p class="muted">QC report: <a href="{_escape(qc_link)}">'
                f"{_escape(str(output.get('qc_report_sha256'))[:16])}</a></p>"
            )
    return "\n".join(parts)


@media_app.command("preview")
def media_preview(
    run_id: str = typer.Argument(..., help="Run id."),
    stage: str | None = typer.Option(
        None, "--stage", help="sheets | keyframes | clips (default: all three)"
    ),
) -> None:
    """Write data/previews/<run_id>/<stage>.html (stdlib-only static page)."""
    stages = [stage.strip().lower()] if stage else ["sheets", "keyframes", "clips"]
    for item in stages:
        if item not in ("sheets", "keyframes", "clips", "animatic", "render"):
            _fail(
                "--stage must be sheets|keyframes|clips|animatic|render, "
                f"got {item!r}"
            )

    config, db, clock, _ids, repos = _connect()
    migrate(db)
    run = repos.pipeline.get_run(run_id)
    if run is None:
        _fail(f"unknown run {run_id}")
    episode_id = run.episode_id
    if not episode_id:
        _fail(f"run {run_id} has no episode")
    episode = repos.content.get_episode(episode_id)
    assert episode is not None
    tracks = {track.id: track for track in repos.content.list_tracks(episode_id)}
    shots = {
        shot.id: shot
        for track in tracks.values()
        for shot in repos.content.shots_for_track(track.id)
    }
    cas = _cas(config, db, clock)
    outdir = Path(config.settings.paths.home) / "data" / "previews" / run_id
    outdir.mkdir(parents=True, exist_ok=True)

    for item in stages:
        body_parts: list[str] = [f"<h1>media preview — {_escape(item)}</h1>"]
        body_parts.append(
            f'<p class="muted">run <code>{_escape(run_id)}</code> · '
            f"episode <code>{_escape(episode_id)}</code></p>"
        )
        sheet_events = list(
            repos.audit.list(event="character_sheet_generated", limit=1000)
        ) + list(repos.audit.list(event="character_sheet_attempt", limit=1000))
        if item == "sheets":
            body_parts.append(
                _render_sheets(repos, sheet_events, cas, outdir, episode.series_id)
            )
        elif item == "keyframes":
            body_parts.append(_render_attempts(db, cas, outdir, shots, tracks, "keyframe"))
        elif item == "animatic":
            body_parts.append(_render_animatic(db, repos, cas, outdir, run_id, config, clock, _ids))
        elif item == "render":
            body_parts.append(_render_render_preview(repos, cas, outdir, run_id))
        else:
            body_parts.append(_render_attempts(db, cas, outdir, shots, tracks, "clip"))
        target = outdir / f"{item}.html"
        target.write_text(_preview_page(f"media preview — {item}", "\n".join(body_parts)), "utf-8")
        console.print(f"preview written to {target}")


def _render_sheets(  # type: ignore[no-untyped-def]
    repos, event_rows, cas, outdir: Path, series_id: str
) -> str:
    sheets = repos.sheets.list_for_series(series_id)
    by_sheet: dict[str, dict[str, Any]] = {}
    attempt_history: dict[tuple[str, str], list[str]] = {}
    for event in event_rows:
        try:
            payload = json.loads(event.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if event.event == "character_sheet_generated":
            sheet_id = payload.get("sheet_id")
            if isinstance(sheet_id, str):
                by_sheet[sheet_id] = payload
        elif event.event == "character_sheet_attempt":
            key = (str(payload.get("character_id")), str(payload.get("role")))
            cost = payload.get("cost_musd")
            cost_text = f"${musd_to_usd(int(cost)):.4f}" if isinstance(cost, int | float) else "-"
            history_item = (
                f"<li>nonce <code>{_escape(str(payload.get('nonce', ''))[:12])}</code> "
                f"· qc <strong>{_escape(payload.get('qc_status', '?'))}</strong> "
                f"· sanitize {_escape(payload.get('sanitize_level', '-'))} "
                f"· {cost_text}</li>"
            )
            attempt_history.setdefault(key, []).append(history_item)
    if not sheets:
        return '<p class="muted">no sheets yet (run S8a or approve G0 first)</p>'
    parts: list[str] = []
    for sheet in sheets:
        payload = by_sheet.get(sheet.id, {})
        qc_raw = payload.get("qc")
        qc = [c for c in qc_raw if isinstance(c, dict)] if isinstance(qc_raw, list) else []
        cost = payload.get("cost_musd")
        cost_text = f"${musd_to_usd(int(cost)):.4f}" if isinstance(cost, int | float) else "-"
        link = _copy_for_preview(outdir, cas, sheet.asset_id)
        img = (
            f'<img src="{_escape(link)}" alt="sheet {_escape(sheet.character_id)}">'
            if link
            else ""
        )
        warns = sum(
            1 for c in qc if isinstance(c, dict) and c.get("status") == "warn"
        )
        history = "".join(attempt_history.get((sheet.character_id, sheet.role), []))
        prompt_spec = payload.get("prompt_spec_json")
        prompt_text = ""
        if isinstance(prompt_spec, str):
            try:
                parsed = json.loads(prompt_spec)
                prompt_text = str(parsed.get("scene_text") or parsed.get("full_prompt") or "")
            except json.JSONDecodeError:
                prompt_text = prompt_spec[:200]
        parts.append(
            f'<div class="card"><h2>{_escape(sheet.character_id)} · '
            f'{_escape(sheet.role)} · v{sheet.version} '
            f'<span class="muted">({_escape(sheet.status)})</span></h2>\n{img}\n'
            f"<p>{_escape(prompt_text)}</p>\n"
            f"<p>spec <code>{_escape(sheet.spec_hash[:16])}</code> · "
            f"nonce <code>{_escape(sheet.nonce[:12])}</code> · cost {_escape(cost_text)} · "
            f"sanitize {_escape(payload.get('sanitize_level', '-'))} · warnings {warns}</p>\n"
            f'<p class="muted">attempt history</p><ul>{history}</ul>\n'
            f"{_render_qc_rows(qc)}</div>"
        )
    return "\n".join(parts)


def _render_attempts(  # type: ignore[no-untyped-def]
    db, cas, outdir: Path, shots: dict[str, Any], tracks: dict[str, Any], kind: str
) -> str:
    rows = db.connect().execute(
        "SELECT * FROM shot_attempts WHERE kind = ? ORDER BY shot_id, attempt_no",
        (kind,),
    ).fetchall()
    parts: list[str] = []
    by_shot: dict[str, list[Any]] = {}
    for row in rows:
        shot_id = str(row["shot_id"])
        if shot_id not in shots:
            continue
        by_shot.setdefault(shot_id, []).append(row)
    if not by_shot:
        return f'<p class="muted">no {kind} attempts yet</p>'
    ordered = sorted(
        by_shot.items(),
        key=lambda item: (
            (shots[item[0]].track_id, shots[item[0]].shot_no)
            if item[0] in shots
            else ("", 0)
        ),
    )
    by_shot = dict(ordered)
    for shot_id, attempts in by_shot.items():
        shot = shots.get(shot_id)
        shot_no = shot.shot_no if shot is not None else "?"
        track = tracks.get(shot.track_id) if shot is not None else None
        aspect = track.aspect if track is not None else "?"
        prompt = shot.prompt if shot is not None else ""
        history: list[str] = []
        for attempt in attempts:
            attempt = dict(attempt)
            try:
                qc = json.loads(attempt["qc_json"] or "[]")
            except json.JSONDecodeError:
                qc = []
            warns = sum(
                1 for c in qc if isinstance(c, dict) and c.get("status") == "warn"
            )
            cost = f"${musd_to_usd(int(attempt['cost_musd'])):.4f}"
            nonce_html = _escape(str(attempt["nonce"])[:12])
            history.append(
                f"<li>attempt {attempt['attempt_no']} · nonce <code>{nonce_html}</code> "
                f"· qc <strong>{_escape(attempt['qc_status'])}</strong> "
                f"· sanitize {attempt['sanitize_level']} "
                f"· {cost} · warnings {warns}</li>"
            )
        current = attempts[-1]
        asset_sha = current["conformed_asset_id"] or current["asset_id"]
        try:
            qc = json.loads(current["qc_json"] or "[]")
        except json.JSONDecodeError:
            qc = []
        link = _copy_for_preview(outdir, cas, asset_sha)
        tag = "video" if kind == "clip" else "img"
        media = (
            f'<{tag} src="{_escape(link)}" controls></{tag}>'
            if tag == "video" and link
            else (f'<{tag} src="{_escape(link)}"></{tag}>' if link else "")
        )
        parts.append(
            f'<div class="card"><h2>shot {shot_no} · {_escape(aspect)}</h2>\n'
            f"<p>{_escape(prompt)}</p>\n{media}\n"
            f'<p class="muted">attempt history</p><ul>{"".join(history)}</ul>\n'
            f"{_render_qc_rows(qc)}</div>"
        )
    return "\n".join(parts)



@media_app.command("qc")
def media_qc(
    reference: str = typer.Argument(..., help="Attempt id, asset sha, or file path."),
    aspect: str = typer.Option("9:16", "--aspect", help="Aspect to check (default 9:16)."),
    kind: str | None = typer.Option(
        None, "--kind", help="image | video (aliases: keyframe | clip; default: infer)"
    ),
    expected_frames: int | None = typer.Option(None, "--expected-frames"),
    keyframe: str | None = typer.Option(None, "--keyframe", help="Keyframe path for SSIM (clips)."),
) -> None:
    """Run the visual QC on an asset/attempt and print each check."""
    from pathlib import Path as _Path

    from studio.qc.visual import qc_image, qc_summary, qc_video

    config, db, clock, ids, repos = _connect()
    migrate(db)
    _ = ids

    path: Path | None = None
    resolved_kind = kind
    resolved_aspect = aspect
    resolved_frames = expected_frames
    resolved_keyframe = Path(keyframe) if keyframe else None

    candidate = _Path(reference)
    if candidate.is_file():
        path = candidate
        if resolved_kind is None:
            resolved_kind = "image" if candidate.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".webp", ".bmp"
            } else "video"
    else:
        attempt = repos.content.get_shot_attempt(reference)
        if attempt is not None:
            resolved_kind = resolved_kind or attempt.kind
            sha = attempt.conformed_asset_id or attempt.asset_id
            if sha is None:
                _fail(f"attempt {reference} has no stored asset yet")
            cas = _cas(config, db, clock)
            path = cas.resolve(sha)
            shot = repos.content.get_shot(attempt.shot_id)
            if shot is not None:
                track = repos.content.get_track(shot.track_id)
                if track is not None:
                    resolved_aspect = track.aspect
        else:
            asset = repos.assets.get(reference)
            if asset is not None:
                cas = _cas(config, db, clock)
                path = cas.resolve(asset.sha256)
                if resolved_kind is None:
                    resolved_kind = "image" if (asset.kind or "") == "image" else "video"
            else:
                _fail(f"not a file, attempt id, or asset sha: {reference!r}")

    assert path is not None
    resolved_kind = _qc_kind(resolved_kind)
    if resolved_kind is None:
        _fail(f"could not infer media kind for {reference!r}; pass --kind image|video")

    attempt_row = (
        repos.content.get_shot_attempt(reference)
        if not Path(reference).is_file()
        else None
    )
    if resolved_kind == "image":
        checks = _run_async(
            qc_image(path, aspect=resolved_aspect, settings=config.settings)
        )
    else:
        if resolved_frames is None and attempt_row is not None:
            from studio.media.conform import clip_frames_for

            shot = repos.content.get_shot(attempt_row.shot_id)
            locked = getattr(shot, "clip_duration_ms", None) if shot is not None else None
            if locked:
                resolved_frames = clip_frames_for(int(locked), int(config.settings.render.fps))
        checks = _run_async(
            qc_video(
                path,
                aspect=resolved_aspect,
                settings=config.settings,
                keyframe_path=resolved_keyframe,
                expected_frames=resolved_frames,
            )
        )
    table = Table(title=f"visual QC — {path.name}")
    for column in ("check", "status", "value", "threshold", "retryable"):
        table.add_column(column)
    for check in checks:
        style = {"pass": "green", "warn": "yellow", "fail": "red"}.get(check.status, "white")
        table.add_row(
            check.name,
            f"[{style}]{check.status}[/{style}]",
            str(check.value),
            str(check.threshold),
            "yes" if check.retryable else "no",
        )
    console.print(table)
    summary = qc_summary(checks)
    n_fail = sum(1 for c in checks if c.status == "fail")
    n_warn = sum(1 for c in checks if c.status == "warn")
    console.print(f"overall: {summary['status']} ({n_fail} fail(s), {n_warn} warning(s))")
    if any(check.status == "fail" for check in checks):
        raise typer.Exit(code=1)



_QC_KIND_ALIASES = {"keyframe": "image", "image": "image", "clip": "video", "video": "video"}


def _qc_kind(value: str | None) -> str | None:
    if value is None:
        return None
    return _QC_KIND_ALIASES.get(value.strip().lower())


def _audition_entries(config: AppConfig, keys: list[str], capability: str) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    for key in keys:
        entry = config.registry.models.get(key)
        if entry is None:
            _fail(f"unknown model_key {key!r} in model_registry.yaml")
        if entry.capability != capability:
            _fail(f"{key!r} has capability {entry.capability!r}, expected {capability!r}")
        entries.append((key, entry))
    return entries


def _audition_estimate(
    entry: Any,
    *,
    kind: str,
    aspect: Any,
    duration_s: float | None,
    refs: list[Path],
) -> int:
    """Worst-case per-call cost in micro-USD; refuses when not computable."""
    from decimal import Decimal

    from studio.config.registry import PerImage, PerMegapixel, PerSecond, PerVideo
    from studio.media.stages import _pixel_dims_for

    pricing = entry.pricing
    if kind == "image":
        if isinstance(pricing, PerImage):
            return usd_to_musd(pricing.usd)
        if isinstance(pricing, PerVideo):
            return usd_to_musd(pricing.usd)
        if isinstance(pricing, PerMegapixel):
            dims = _pixel_dims_for(entry, aspect)
            if dims is None:
                raise StudioError(
                    f"per_megapixel entry {entry.model_ref!r} has no confirmed "
                    f"width/height for aspect {aspect.value}"
                )
            width, height = dims
            return usd_to_musd(
                (Decimal(width) * Decimal(height) / Decimal(1_000_000)) * pricing.usd
            )
        raise StudioError(f"unsupported pricing unit {pricing.unit!r} for an image model")
    assert duration_s is not None
    if isinstance(pricing, PerSecond):
        return usd_to_musd(Decimal(str(duration_s)) * pricing.usd)
    if isinstance(pricing, PerVideo):
        return usd_to_musd(pricing.usd)
    raise StudioError(f"unsupported pricing unit {pricing.unit!r} for a video model")


def _store_reference(cas: Any, path: Path) -> Any:
    suffix = path.suffix.lower().lstrip(".") or "bin"
    return cas.put_file(path, kind="image", ext=suffix, retention="intermediate", move=False)


@media_app.command("audition")
def media_audition(
    kind: str = typer.Argument(..., help="image | clip"),
    model_keys: str = typer.Option(..., "--model-keys", help="Comma-separated registry keys."),
    prompts_file: str | None = typer.Option(
        None, "--prompts-file", help="UTF-8 prompts, one per line (image)."
    ),
    refs: Annotated[list[str] | None, typer.Option(
        "--ref", help="Reference image paths (image audition)."
    )] = None,
    image: str | None = typer.Option(None, "--image", help="Source image path (clip audition)."),
    prompt_text: str | None = typer.Option(None, "--prompt-text", help="Motion prompt (clip)."),
    duration: float = typer.Option(5.0, "--duration", help="Clip duration in seconds (clip)."),
    aspect: str = typer.Option("9:16", "--aspect", help="Aspect ratio."),
    cap_usd: float = typer.Option(1.00, "--cap-usd", min=0.0, help="Hard cap on total spend."),
    yes: bool = typer.Option(False, "--yes", help="Proceed (required to spend)."),
) -> None:
    """Audition image or clip models before committing a series.

    episode_id is NULL on purpose (month/global budgets apply). The worst-case
    total must fit under --cap-usd before ANY call is made; a running-total
    guard re-checks before every subsequent call. Deterministic nonces make
    re-running the same audition free (cache hits). Image auditions with no
    --ref are pure text-to-image; with refs they are reference-guided.
    """
    import shutil
    from datetime import UTC, datetime

    from studio.core.cas import ContentStore
    from studio.core.gateway import CallContext, ProviderGateway
    from studio.domain.enums import Aspect
    from studio.domain.refs import AssetRef
    from studio.providers.base import MediaRequest

    kind = kind.strip().lower()
    if kind not in ("image", "clip"):
        _fail("kind must be image|clip")
    keys = [k.strip() for k in model_keys.split(",") if k.strip()]
    if not keys:
        _fail("--model-keys must name at least one registry key")
    try:
        aspect_enum = Aspect(aspect.strip())
    except ValueError:
        _fail(f"unknown aspect {aspect!r}")

    config, db, clock, ids, repos = _connect()
    del repos
    migrate(db)

    cap_musd = usd_to_musd(str(cap_usd))
    plan: list[tuple[str, Any, str]] = []  # (key, entry, prompt)

    ref_paths = [Path(ref) for ref in (refs or [])]
    image_path: Path | None = None

    if kind == "image":
        if prompts_file is None:
            _fail("--prompts-file is required for image auditions")
        prompts_path = Path(prompts_file)
        if not prompts_path.is_file():
            _fail(f"prompts file not found: {prompts_path}")
        try:
            prompts = [
                line.strip()
                for line in prompts_path.read_bytes().decode("utf-8").splitlines()
                if line.strip()
            ]
        except UnicodeDecodeError:
            _fail(f"{prompts_path} is not UTF-8")
        if not prompts:
            _fail(f"{prompts_path} contains no non-empty prompts")
        if image is not None:
            _fail("--image only applies to clip auditions")
        for ref_path in ref_paths:
            if not ref_path.is_file():
                _fail(f"reference image not found: {ref_path}")
        entries = _audition_entries(config, keys, "image_reference_generate")
        for key, entry in entries:
            if ref_paths and entry.supports_refs is False:
                _fail(f"{key!r} does not support reference inputs")
            if not ref_paths and entry.supports_refs is not False:
                pass  # pure T2I is fine even for ref-capable models
        plan = [(key, entry, prompt) for prompt in prompts for key, entry in entries]
    else:
        if image is None:
            _fail("--image is required for clip auditions")
        if prompt_text is None or not prompt_text.strip():
            _fail("--prompt-text is required for clip auditions")
        if ref_paths:
            _fail("--ref only applies to image auditions; clip auditions take --image")
        image_path = Path(image)
        if not image_path.is_file():
            _fail(f"image not found: {image_path}")
        entries = _audition_entries(config, keys, "image_to_video")
        plan = [(key, entry, prompt_text.strip()) for key, entry in entries]

    try:
        estimates = [
            _audition_estimate(
                entry, kind=kind, aspect=aspect_enum,
                duration_s=duration if kind == "clip" else None,
                refs=ref_paths,
            )
            for _key, entry, _prompt in plan
        ]
    except StudioError as exc:
        _fail(str(exc))
    worst_case = sum(estimates)
    console.print(
        f"audition plan: {len(plan)} call(s) from {len(keys)} model(s) "
        f"worst-case ${musd_to_usd(worst_case):.4f} total (cap ${cap_usd:.4f})"
    )
    for _key, entry, _prompt in plan:
        problems = []
        if entry.needs_confirmation:
            problems.append("needs_confirmation")
        if entry.pricing_verified_on is None and not entry.simulated:
            problems.append("price never verified")
        if problems:
            console.print(f"[yellow]warning: {_key} is unverified ({', '.join(problems)})[/yellow]")
    if worst_case > cap_musd:
        _fail(
            f"worst-case audition cost exceeds --cap-usd "
            f"({musd_to_usd(worst_case):.4f} > {cap_usd:.4f}); raise the cap or narrow the matrix"
        )
    if not yes:
        _fail("aborted (nothing spent) — re-run with --yes to proceed")

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
        / "audition_media"
        / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    outdir.mkdir(parents=True, exist_ok=True)

    input_assets: list[AssetRef] = []
    for ref_path in ref_paths:
        input_assets.append(_store_reference(cas, ref_path))
    if image_path is not None:
        input_assets.append(_store_reference(cas, image_path))

    report_lines = [
        f"media audition ({kind})",
        f"models: {', '.join(keys)}",
        f"aspect: {aspect_enum.value}",
        f"cap_usd: {cap_usd}",
        "",
    ]
    total_spent = 0
    failures: list[str] = []
    for (key, _entry, prompt), est in zip(plan, estimates, strict=True):
        if total_spent + est > cap_musd:
            failures.append(f"{key}: skipped — running total would exceed the cap")
            continue
        nonce_material = (
            f"{key}|{kind}|{aspect_enum.value}|{duration}|{prompt}|"
            + ",".join(r.name for r in ref_paths)
            + (f"|{image_path.name}" if image_path is not None else "")
            + "|seed=0"
        ).encode("utf-8")
        nonce = hashlib.sha256(nonce_material).hexdigest()[:16]
        if kind == "image":
            request = MediaRequest(
                capability="image_reference_generate",
                params={"prompt": prompt},
                input_assets=input_assets,
                aspect=aspect_enum,
                seed=0,
            )
            call = gateway.image
        else:
            request = MediaRequest(
                capability="image_to_video",
                params={"prompt": prompt},
                input_assets=input_assets,
                aspect=aspect_enum,
                duration_s=duration,
                seed=0,
            )
            call = gateway.video
        ctx = CallContext(
            episode_id=None,
            stage_run_id=None,
            nonce=nonce,
            purpose="media_audition",
            gate_exempt="media_audition",
        )
        try:
            result = _run_async(call(key, request, ctx))
        except Exception as exc:
            failures.append(f"{key}:{prompt[:24]}: {type(exc).__name__}: {exc}")
            continue
        ref = result.assets[0]
        source_path = cas.resolve(ref.sha256)
        target = outdir / f"{key.replace('.', '-')}-{nonce}{source_path.suffix or '.bin'}"
        shutil.copy2(source_path, target)
        total_spent += int(result.cost_musd)
        expected_frames = None
        if kind == "clip":
            from studio.media.conform import clip_frames_for

            expected_frames = clip_frames_for(
                int(duration * 1000), int(config.settings.render.fps)
            )
        qc_note = _audition_qc(
            config,
            target,
            kind,
            aspect_enum,
            keyframe=image_path if kind == "clip" else None,
            expected_frames=expected_frames,
            expected_fps=int(config.settings.render.fps) if kind == "clip" else None,
        )
        report_lines.append(
            f"{target.name}\tmodel={key}\tprompt={prompt[:80]}\t"
            f"cost_usd={musd_to_usd(int(result.cost_musd)):.4f}\t{qc_note}"
        )
        console.print(f"  {target.name}  ${musd_to_usd(int(result.cost_musd)):.4f}  {qc_note}")

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


def _audition_qc(
    config: AppConfig,
    target: Path,
    kind: str,
    aspect: Any,
    *,
    keyframe: Path | None = None,
    expected_frames: int | None = None,
    expected_fps: int | None = None,
) -> str:
    from studio.qc.visual import qc_image, qc_video

    try:
        if kind == "image":
            checks = _run_async(qc_image(target, aspect=aspect, settings=config.settings))
        else:
            checks = _run_async(
                qc_video(
                    target,
                    aspect=aspect,
                    settings=config.settings,
                    keyframe_path=keyframe,
                    expected_frames=expected_frames,
                    expected_fps=expected_fps,
                )
            )
    except Exception as exc:  # QC is informational in the audition report
        return f"qc={type(exc).__name__}"
    fails = sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in checks if c.status == "warn")
    if kind != "clip":
        return f"qc_fail={fails} qc_warn={warns}"
    by_name = {check.name: check for check in checks}
    parts = []
    for name, label in (("frame_count", "frames"), ("fps", "fps"), ("first_frame_ssim", "ssim")):
        check = by_name.get(name)
        if check is not None:
            parts.append(f"{label}={check.value}")
    parts.append(f"qc_fail={fails} qc_warn={warns}")
    return " ".join(parts)


@media_app.command("smoke")
def media_smoke(
    cap_usd: float = typer.Option(3.00, "--cap-usd", help="Hard cap for the smoke run."),
    yes: bool = typer.Option(False, "--yes", help="Proceed (required to spend)."),
) -> None:
    """Opt-in live smoke (§Q): 1 character, 2 shots, 5 s each, 9:16, draft profile.

    Runs S8a → S8 → S10 through the real handlers against live fal in a
    scratch STUDIO_HOME (a copy of this home's configs), with gates
    auto-approved (draft profile policy). Writes the preview, per-call
    costs, QC values and conform reports to data/previews/media_smoke/<ts>/
    and prints the remote request ids for dashboard reconciliation.
    Requires FAL_KEY; refuses when any needed registry entry is unconfirmed
    or unpriced. Never usable in CI.
    """
    if not os.environ.get("FAL_KEY", "").strip():
        _fail("FAL_KEY is not set — export it before running the live smoke")

    from studio.domain.enums import Aspect
    from studio.media.registry import media_model_entry, model_confirmation_problems
    from studio.media.stages import _estimate_clip_cost, _estimate_image_cost

    config, _db, _clock, _ids, _repos = _connect()
    migrate(_db)
    try:
        resolved = config.registry.resolve_profile("draft")
    except StudioError as exc:
        problems: list[str] = []
        for key, entry in sorted(config.registry.models.items()):
            if entry.capability not in ("image_reference_generate", "image_to_video"):
                continue
            problems.extend(
                f"{key}: {problem}" for problem in model_confirmation_problems(entry)
            )
        if problems:
            listing = "\n- ".join(problems)
            _fail(f"refusing the live smoke:\n- {exc}\n- {listing}")
        _fail(f"draft profile does not resolve: {exc}")

    named: dict[str, Any] = {}
    needs: list[str] = []
    for role in ("sheets", "keyframes", "clips"):
        try:
            key, entry = media_model_entry(config, resolved, role)
        except StudioError as exc:
            _fail(f"draft {role} model does not resolve: {exc}")
        named[role] = (key, entry)
        needs.extend(f"{role} ({key}): {problem}" for problem in model_confirmation_problems(entry))
    if needs:
        listing = "\n- ".join(needs)
        _fail(
            "refusing the live smoke — needed registry entries are not confirmed/priced:\n"
            f"- {listing}\n"
            "confirm them in model_registry.yaml (the operator owns that attestation)"
        )

    shot_ms = 5000
    estimates: dict[str, int] = {}
    for role, (key, entry) in named.items():
        if role == "clips":
            est = _estimate_clip_cost(entry, clip_duration_ms=shot_ms)
        else:
            est = _estimate_image_cost(entry, aspect=Aspect.TALL)
        if est is None:
            _fail(f"worst-case cost not computable offline for {key!r}; refusing to spend")
        estimates[role] = est
    worst = estimates["sheets"] + 2 * estimates["keyframes"] + 2 * estimates["clips"]
    cap_musd = usd_to_musd(str(cap_usd))
    console.print(
        f"smoke worst case: sheet ${musd_to_usd(estimates['sheets']):.4f} + "
        f"2 keyframes ${musd_to_usd(2 * estimates['keyframes']):.4f} + "
        f"2 clips ${musd_to_usd(2 * estimates['clips']):.4f} = "
        f"[bold]${musd_to_usd(worst):.4f}[/bold] (cap ${cap_usd:.4f})"
    )
    if worst > cap_musd:
        _fail(f"worst-case smoke cost exceeds --cap-usd ({musd_to_usd(worst):.4f} > {cap_usd:.4f})")
    if not yes:
        _fail("aborted (nothing spent) — re-run with --yes to proceed")

    rc = _run_media_smoke(
        config, named=named, cap_usd=cap_usd, worst_case_musd=worst, estimates=estimates
    )
    raise typer.Exit(code=rc)


def _run_media_smoke(
    config: AppConfig,
    *,
    named: dict[str, Any],
    cap_usd: float,
    worst_case_musd: int,
    estimates: dict[str, int],
) -> int:
    import json as _json
    import shutil
    import tempfile
    from datetime import UTC, datetime

    import yaml

    from studio.config.loader import load_all
    from studio.core.clock import SystemClock
    from studio.core.ids import UlidIds
    from studio.db import build_repos, migrate
    from studio.db.connection import Database
    from studio.domain.series_bible import BibleContent, CharacterBible
    from studio.pipeline.dag import Dag
    from studio.pipeline.engine import Engine
    from studio.pipeline.specs import media_specs
    from studio.pipeline.worker import Worker

    real_home = Path(_home())
    src_configs = Path(config.settings.paths.home) / "configs"
    if not src_configs.is_dir():
        _fail(f"configs not found at {src_configs}")
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    scratch = Path(tempfile.mkdtemp(prefix="studio-media-smoke-"))
    shutil.copytree(src_configs, scratch / "configs")
    # Pin the scratch run to a single language and aspect.
    settings_path = scratch / "configs" / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    data["languages"] = [Language.AR.value]
    data["aspects"] = ["9:16"]
    settings_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    previous_home = os.environ.get("STUDIO_HOME")
    os.environ["STUDIO_HOME"] = str(scratch)
    try:
        sconfig = load_all(scratch)
        sdb_path = Path(sconfig.settings.paths.db)
        sdb_path.parent.mkdir(parents=True, exist_ok=True)
        sdb = Database(sdb_path)
        migrate(sdb)
        sclock = SystemClock()
        sids = UlidIds()
        srepos = build_repos(sdb, sclock, sids)
        scas = _cas(sconfig, sdb, sclock)

        short = sids.new()[:8].lower()
        series = srepos.content.create_series(
            name="Studio Media Smoke", slug=f"studio-media-smoke-{short}"
        )
        srepos.content.create_character(
            series_id=series.id, role="lead", name="Smoke Hero", voices_json="{}"
        )
        _bible, _created = srepos.bible.create_version(
            series.id,
            BibleContent(
                title="Studio Media Smoke",
                premise="A lone traveler crosses a neon desert at dusk.",
                characters=[
                    CharacterBible(
                        name="Smoke Hero",
                        description="A weathered traveler in a sun-bleached coat.",
                        visual_tokens=["sun-bleached coat", "neon dusk", "weathered face"],
                    )
                ],
                style_tokens=["cinematic", "film grain"],
                tone=["quiet", "awe"],
            ),
            created_by="s1",
        )
        episode = srepos.content.create_episode(
            series_id=series.id,
            title="Media Smoke",
            concept="A lone traveler crosses a neon desert at dusk.",
            languages_json=f'["{Language.AR.value}"]',
            aspects_json='["9:16"]',
        )
        track = srepos.content.create_track(episode_id=episode.id, aspect="9:16")
        prompts = [
            "A lone traveler in a sun-bleached coat walks through a "
            "neon-lit desert at dusk, cinematic wide shot, anamorphic lens flare.",
            "The traveler stops before a half-buried neon sign glowing "
            "turquoise, close profile, wind moving through the coat, cinematic.",
        ]
        shot_ids: list[str] = []
        for index, prompt in enumerate(prompts, start=1):
            shot = srepos.content.create_shot(
                track_id=track.id, shot_no=index, prompt=prompt, min_duration_ms=5000
            )
            srepos.content.set_shot_timing(
                shot.id,
                clip_duration_ms=5000,
                speed_factor=1.0,
                hold_frames=0,
                timing_status="locked",
            )
            shot_ids.append(shot.id)

        snapshot = {
            "profile": "draft",
            "media_models": {role: key for role, (key, _entry) in named.items()},
            "smoke": True,
        }
        run_row = srepos.pipeline.create_run(
            episode_id=episode.id,
            profile="draft",
            auto_approve=True,
            config_snapshot_json=_json.dumps(snapshot, sort_keys=True),
        )
        srepos.jobs.enqueue(
            kind="advance_run",
            payload={"run_id": run_row.id},
            dedupe_key=f"advance:{run_row.id}",
        )
        dag = Dag(media_specs(after=()))
        engine = Engine(
            db=sdb, repos=srepos, dag=dag, clock=sclock, config=sconfig, ids=sids,
            llm_subset_only=False,
        )
        worker = Worker(sdb, engine, srepos, sconfig, sclock)
        console.print(f"scratch home: {scratch}")
        jobs = _run_async(worker.run(until_idle=True))
        console.print(f"jobs executed: {jobs}")

        failures = srepos.pipeline.list_stage_runs(run_row.id)
        bad = [row for row in failures if row.status != "succeeded"]
        _write_smoke_artifacts(
            real_home=real_home,
            scratch_db=sdb,
            scratch_repos=srepos,
            scas=scas,
            series_id=series.id,
            episode_id=episode.id,
            run_id=run_row.id,
            worst_case_musd=worst_case_musd,
            ts=ts,
        )
        if bad:
            for row in bad:
                console.print(f"[red]stage {row.stage}: {row.status}[/red]")
            console.print(f"scratch home kept at {scratch}")
            return 1
        console.print("smoke completed; scratch home kept at " + str(scratch))
        return 0
    finally:
        if previous_home is None:
            os.environ.pop("STUDIO_HOME", None)
        else:
            os.environ["STUDIO_HOME"] = previous_home


def _write_smoke_artifacts(
    *,
    real_home: Path,
    scratch_db: Database,
    scratch_repos: StudioRepos,
    scas: Any,
    series_id: str,
    episode_id: str,
    run_id: str,
    worst_case_musd: int,
    ts: str,
) -> None:
    import json as _json

    outdir = real_home / "data" / "previews" / "media_smoke" / ts
    outdir.mkdir(parents=True, exist_ok=True)

    episode = scratch_repos.content.get_episode(episode_id)
    assert episode is not None
    tracks = {track.id: track for track in scratch_repos.content.list_tracks(episode_id)}
    shots = {
        shot.id: shot
        for track in tracks.values()
        for shot in scratch_repos.content.shots_for_track(track.id)
    }
    body = ["<h1>media smoke</h1>"]
    body.append(
        f'<p class="muted">scratch run <code>{_escape(run_id)}</code> · '
        f'episode <code>{_escape(episode_id)}</code></p>'
    )
    sheet_events = list(scratch_repos.audit.list(event="character_sheet_generated", limit=100))
    sheet_events += list(scratch_repos.audit.list(event="character_sheet_attempt", limit=100))
    body.append(_render_sheets(scratch_repos, sheet_events, scas, outdir, series_id))
    for kind in ("keyframe", "clip"):
        body.append(_render_attempts(scratch_db, scas, outdir, shots, tracks, kind))
    (outdir / "preview.html").write_text(
        _preview_page("media smoke", "\n".join(body)), encoding="utf-8"
    )

    calls = scratch_db.connect().execute(
        "SELECT capability, model_key, purpose, status, est_cost_musd, actual_cost_musd, "
        "remote_request_id FROM provider_calls ORDER BY created_at, id"
    ).fetchall()
    total_actual = sum(int(row["actual_cost_musd"] or 0) for row in calls)
    lines = [
        "media smoke report",
        f"run_id: {run_id}",
        f"episode_id: {episode_id}",
        f"worst_case_usd: {musd_to_usd(worst_case_musd):.4f}",
        f"actual_cost_usd: {musd_to_usd(total_actual):.4f}",
        "",
        "remote request ids (reconcile against the fal dashboard):",
    ]
    for row in calls:
        rid = row["remote_request_id"] or "(none)"
        cost = musd_to_usd(int(row["actual_cost_musd"] or row["est_cost_musd"] or 0))
        lines.append(f"  {row['capability']}/{row['model_key']} {rid} cost_usd={cost}")
    lines.append("")
    lines.append("ledger vs expected:")
    ledger_rows = scratch_db.connect().execute(
        "SELECT entry_type, COALESCE(SUM(amount_musd), 0) AS total FROM cost_ledger "
        "GROUP BY entry_type"
    ).fetchall()
    for row in ledger_rows:
        lines.append(f"  {row['entry_type']}: {musd_to_usd(int(row['total'])):.4f}")
    lines.append(f"  expected worst case: {musd_to_usd(worst_case_musd):.4f}")
    lines.append("")
    lines.append("QC values (from attempt bookkeeping, incl. SSIM / freeze ratios):")
    attempts = scratch_db.connect().execute(
        "SELECT shot_id, kind, attempt_no, qc_status, qc_json, cost_musd FROM shot_attempts "
        "ORDER BY shot_id, kind, attempt_no"
    ).fetchall()
    for row in attempts:
        lines.append(
            f"  shot={row['shot_id']} kind={row['kind']} attempt={row['attempt_no']} "
            f"qc={row['qc_status']} cost_usd={musd_to_usd(int(row['cost_musd'])):.4f}"
        )
        try:
            checks = _json.loads(row["qc_json"] or "[]")
        except _json.JSONDecodeError:
            checks = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            lines.append(
                f"    - {check.get('name')}: {check.get('status')} value={check.get('value')} "
                f"threshold={check.get('threshold')}"
            )
    lines.append("")
    lines.append("conform reports (from s10 stage output):")
    outputs = scratch_db.connect().execute(
        "SELECT subject_id, output_json FROM stage_runs WHERE stage = 's10_clips' "
        "ORDER BY created_at"
    ).fetchall()
    for row in outputs:
        try:
            payload = _json.loads(row["output_json"] or "{}")
        except _json.JSONDecodeError:
            payload = {}
        conform = payload.get("conform", {})
        lines.append(f"  subject={row['subject_id']}")
        lines.append(_json.dumps(conform, indent=2, sort_keys=True))
    (outdir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    console.print(f"smoke artifacts written to {outdir}")
    console.print("request ids:")
    for row in calls:
        if row["remote_request_id"]:
            console.print(f"  {row['capability']}: {row['remote_request_id']}")
    console.print(
        f"ledger vs expected: actual ${musd_to_usd(total_actual):.4f} "
        f"vs worst case ${musd_to_usd(worst_case_musd):.4f}"
    )


# ==================================================================
# encode
# ==================================================================


@encode_app.command("probe")
def encode_probe(
    use_cache: bool = typer.Option(True, "--cache/--no-cache", help="Reuse the probe cache."),
) -> None:
    """Probe NVENC with a real encode and print the resolved encoder.

    Presence in `ffmpeg -encoders` is never trusted: the probe encodes 24 frames
    at every export preset geometry. `encode.encoder: nvenc` fails closed,
    `auto` falls back to x264 and records the scrubbed reason.
    """
    from studio.video.encoders import SubprocessProbeRunner, driver_string, resolve_encoder
    from studio.video.ffmpeg import ffmpeg_version

    config, *_ = _connect()
    geometries = sorted({(p.width, p.height) for p in config.presets.channels.values()})
    cache_file = config.settings.encode.probe.cache_file
    cache_path = Path(cache_file) if Path(cache_file).is_absolute() else _home() / cache_file
    version = ".".join(str(part) for part in _run_async(ffmpeg_version()))
    try:
        resolution = _run_async(
            resolve_encoder(
                config.settings,
                SubprocessProbeRunner(),
                geometries=geometries,
                cache_path=cache_path if use_cache else None,
                ffmpeg_version=version,
                driver=driver_string(),
            )
        )
    except StudioError as exc:
        _fail(str(exc))
    console.print(f"encoder: [bold]{resolution.profile.name}[/bold]")
    console.print(f"reason:  {resolution.reason}")
    if resolution.fallback_reason:
        console.print(f"fallback: {resolution.fallback_reason}")
    console.print(f"profile_hash: {resolution.profile.profile_hash}")
    console.print(f"args: {' '.join(resolution.profile.args)}")
    console.print(
        f"cache: {'hit' if resolution.cache_hit else 'miss'} at {cache_path}"
        if use_cache
        else "cache: disabled"
    )
    console.print(f"geometries: {', '.join(f'{w}x{h}' for w, h in geometries)}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":
    main()
