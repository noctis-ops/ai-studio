"""Media pipeline stages (Phase 4 sections H-L).

DAG (wired by `specs.py` / `demo_specs()`):

    s4p_media_preflight (EPISODE, free, fail closed)
        → s5a_tts_preflight → … → s7b_audio_mix
        → s8a_character_sheets (subjects: missing (character, role); Gate G0)
        → s8_keyframes       (SHOT, paid; Gate G2)
        → s10p_clip_preflight (EPISODE, free, after G2)
        → s10_clips          (SHOT, paid; locked duration; conform)

Shared design decisions (recorded in PHASE4_REPORT.md):

- **Crash resume repays nothing.** Shot attempts are created before the first
  paid call; the attempt's nonce is persisted in one statement with the
  prompt spec before every submit. After a crash the handler rebuilds the
  cache key from the persisted attempt, so the gateway either resumes the
  in-flight call (adoption) or hits the provider cache. A regeneration
  changes the nonce (``base.qN``); a resume keeps it.
- **Sanitizer state survives crashes.** ``prompt_spec_json`` and
  ``refusal_log_json`` are written before each submit, so a crash after a
  refusal resumes at the sanitized prompt; the sanitizer LLM rewrite itself
  is memoized by the gateway (nonce ``san{level}``), so no extra paid call
  happens either way. Sanitizer rewrites count toward
  ``media.max_paid_attempts_per_shot``.
- **Circuit breaker.** ``media.circuit_breaker_consecutive_failures`` failed
  shots in one run for the stage (or a single non-retryable QC failure)
  fails the stage with :class:`MediaCircuitOpen` plus an audit row, so a
  broken endpoint cannot burn money one shot at a time.
- **No visual LLM anywhere.** All QC is ffmpeg/ffprobe (``qc/visual.py``).
- Sheets are keyed on the *pinned bible character name* (the same identity
  S3's continuity gate validates and shot plans reference), not on the
  ``characters`` table — production episodes have bible characters but no
  ``characters`` rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from studio.config.registry import ModelEntry
from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.core.clock import format_ts
from studio.core.gateway import CallContext
from studio.domain.enums import Aspect, BudgetScope, Language, Profile, Scope
from studio.domain.errors import (
    BudgetExceeded,
    ContentPolicyError,
    FailedPolicy,
    MediaAttemptExhausted,
    MediaCircuitOpen,
    MediaConfigError,
    MediaJobFailedError,
    PermanentProviderError,
    PreflightError,
    StudioError,
    VisualQcError,
)
from studio.domain.money import musd_to_usd, usd_to_musd
from studio.domain.refs import AssetRef, SubjectRef
from studio.media.conform import clip_frames_for, conform_clip
from studio.media.prompts import (
    PromptSpec,
    compose_keyframe_prompt,
    compose_motion_prompt,
    compose_motion_prompt_spec,
    compose_sheet_prompt,
    prompt_version_sha,
    seed_for,
)
from studio.media.refs import prepare_refs
from studio.media.registry import (
    aspect_value,
    max_refs,
    media_model_entry,
    media_spec,
    model_confirmation_problems,
    supported_aspects,
    supported_durations_s,
)
from studio.media.sanitizer import sanitize_scene_text
from studio.pipeline.estimate_media import media_estimate, media_estimate_hash_parts
from studio.pipeline.stage import StageOutput
from studio.providers.base import MediaRequest
from studio.qc.visual import QcCheck, qc_image, qc_video

if TYPE_CHECKING:
    from studio.domain.models import (
        EpisodeRow,
        ShotAttemptRow,
        ShotRow,
        TrackRow,
    )
    from studio.pipeline.context import RunContext

logger = logging.getLogger(__name__)

_NONCE_RETRY_MARKER = ".q"
_S4_STAGE = "metadata_safety"
_SHOT_PLAN_STAGE = "shot_plan"
_KEYFRAME_MODEL_ROLE = "keyframes"
_SHEET_MODEL_ROLE = "sheets"
_CLIP_MODEL_ROLE = "clips"


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _episode(ctx: RunContext) -> EpisodeRow | None:
    if not ctx.episode_id:
        return None
    return ctx.repos.content.get_episode(ctx.episode_id)


def _episode_subject(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    return [
        SubjectRef(
            scope=Scope.EPISODE,
            id=episode.id,
            lineage=((Scope.SERIES, episode.series_id),),
        )
    ]


def _track_shots(ctx: RunContext) -> list[ShotRow]:
    episode = _episode(ctx)
    if episode is None:
        return []
    return sorted(
        ctx.repos.content.list_shots(episode.id),
        key=lambda shot: (shot.track_id, shot.shot_no),
    )


def _shot_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    subjects: list[SubjectRef] = []
    for shot in _track_shots(ctx):
        subjects.append(
            SubjectRef(
                scope=Scope.SHOT,
                id=shot.id,
                lineage=(
                    (Scope.SERIES, episode.series_id),
                    (Scope.EPISODE, episode.id),
                    (Scope.TRACK, shot.track_id),
                ),
            )
        )
    return subjects


def _stage_output(
    ctx: RunContext, stage: str, subject_id: str
) -> dict[str, Any] | None:
    for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage=stage):
        if row.subject_id == subject_id and row.status == "succeeded" and row.output_json:
            try:
                data: dict[str, Any] = json.loads(row.output_json)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _pinned_bible(ctx: RunContext, episode_id: str):  # type: ignore[no-untyped-def]
    from studio.domain.series_bible import SeriesBible

    episode = ctx.repos.content.get_episode(episode_id)
    if episode is None:
        return None
    s1_output = _stage_output(ctx, "bible_upsert", episode_id)
    version: int | None = None
    if isinstance(s1_output, dict):
        raw = s1_output.get("version")
        if isinstance(raw, int):
            version = raw
    bible: SeriesBible | None = None
    if version is not None:
        bible = ctx.repos.bible.get(episode.series_id, version)
    if bible is None:
        bible = ctx.repos.bible.latest(episode.series_id)
    return bible


def _style_tokens(bible: Any) -> list[str]:
    return list(bible.style_tokens) if bible is not None else []


def _shot_plan(ctx: RunContext, track_id: str) -> dict[int, dict[str, Any]]:
    output = _stage_output(ctx, _SHOT_PLAN_STAGE, track_id)
    shots = output.get("shots", []) if isinstance(output, dict) else []
    plan: dict[int, dict[str, Any]] = {}
    for item in shots if isinstance(shots, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            no = int(item.get("no") or 0)
        except (TypeError, ValueError):
            continue
        if no >= 1:
            plan[no] = item
    return plan


def _shot_characters(ctx: RunContext, track_id: str) -> dict[int, list[str]]:
    plan = _shot_plan(ctx, track_id)
    characters: dict[int, list[str]] = {}
    for no, item in plan.items():
        raw = item.get("characters")
        names = [str(name) for name in raw if str(name).strip()] if isinstance(raw, list) else []
        characters[no] = names
    return characters


def _shot_camera(ctx: RunContext, track_id: str) -> dict[int, str]:
    plan = _shot_plan(ctx, track_id)
    camera: dict[int, str] = {}
    for no, item in plan.items():
        raw = item.get("camera")
        camera[no] = str(raw).strip() if isinstance(raw, str) else ""
    return camera


def _bible_character(bible: Any, name: str):  # type: ignore[no-untyped-def]
    if bible is None:
        return None
    for character in bible.characters:
        if character.name == name:
            return character
    return None


def _character_visual_spec(bible: Any, name: str) -> str:
    character = _bible_character(bible, name)
    if character is None:
        return ""
    tokens = ", ".join(token.strip() for token in character.visual_tokens if token.strip())
    description = character.description.strip()
    if tokens and description:
        return f"{tokens}. {description}"
    return tokens or description


def _model_entry(
    ctx: RunContext, role: Literal["sheets", "keyframes", "clips"]
) -> tuple[str, ModelEntry]:
    key, entry = media_model_entry(ctx.config, ctx.resolved_profile, role)
    return key, entry


def _ref_clause(entry: ModelEntry) -> str | None:
    ref_clause = media_spec(entry).prompt_templates.get("ref_clause")
    return ref_clause if isinstance(ref_clause, str) and ref_clause.strip() else None


def _media_model_problems(
    ctx: RunContext, *, final: bool, need_refs: bool
) -> list[str]:
    """Every media-registry problem, listed at once (s4p/s10p preflights)."""
    settings = ctx.config.settings
    problems: list[str] = []
    for role in ("sheets", "keyframes", "clips"):
        try:
            key, entry = _model_entry(ctx, role)
        except MediaConfigError as exc:
            problems.append(f"{role}: {exc}")
            continue
        except StudioError as exc:
            problems.append(f"{role}: {exc}")
            continue
        if entry.provider == "fal":
            import os

            if not os.environ.get(settings.fal.key_env):
                problems.append(
                    f"{role} model {key!r} requires {settings.fal.key_env} to be set"
                )
        for problem in model_confirmation_problems(entry, final=final):
            problems.append(f"{role} model {key!r}: {problem}")
        if role == "clips":
            # supported_durations_s raises on an empty contract; the preflight
            # must list the problem, not crash the whole check.
            try:
                clips_durations = supported_durations_s(entry)
            except MediaConfigError:
                clips_durations = ()
            if not clips_durations:
                problems.append(f"clips model {key!r} declares no supported durations")
        if need_refs and role == "keyframes":
            if entry.supports_refs is not True:
                problems.append(
                    f"keyframes model {key!r} is not marked reference-capable "
                    f"(supports_refs={entry.supports_refs})"
                )
            if max_refs(entry) < 1:
                problems.append(f"keyframes model {key!r} declares max_refs < 1")
    return problems


def _aspect_problems(
    ctx: RunContext, entries: dict[str, tuple[str, ModelEntry]]
) -> list[str]:
    problems: list[str] = []
    track_aspects = {aspect for aspect in ctx.aspects}
    for role, (key, entry) in entries.items():
        try:
            supported = supported_aspects(entry)
        except MediaConfigError as exc:
            problems.append(f"{role}: {exc}")
            continue
        supported_values = {aspect.value for aspect in supported}
        for aspect in track_aspects:
            if aspect.value not in supported_values:
                problems.append(
                    f"{role} model {key!r} does not support track aspect {aspect.value}"
                )
            else:
                try:
                    aspect_value(entry, aspect)
                except MediaConfigError as exc:
                    problems.append(f"{role}: {exc}")
    return problems


def _budget_row_cap(
    ctx: RunContext, scope: BudgetScope, ref: str, default_usd: float | None
) -> int | None:
    row = ctx.repos.budgets.get(scope.value, ref)
    if row is not None:
        return row.cap_musd
    if default_usd is None:
        return None
    return usd_to_musd(str(default_usd))


def _remaining_musd(
    ctx: RunContext, scope: BudgetScope, ref: str, cap_musd: int | None
) -> int | None:
    if cap_musd is None:
        return None
    held = ctx.repos.budgets.held_musd(scope.value, ref)
    return cap_musd - held


def _worst_case_pause(
    ctx: RunContext,
    episode: EpisodeRow,
    *,
    worst_case_musd: int,
    label: str,
) -> None:
    """Whole-episode affordability guard; raises before any spend."""
    settings = ctx.config.settings
    month_ref = ctx.clock.month_tag()
    caps: list[tuple[BudgetScope, str, int | None]] = [
        (
            BudgetScope.EPISODE,
            episode.id,
            _budget_row_cap(
                ctx,
                BudgetScope.EPISODE,
                episode.id,
                float(settings.budgets.default_episode_cap_usd),
            ),
        ),
        (
            BudgetScope.MONTH,
            month_ref,
            _budget_row_cap(
                ctx,
                BudgetScope.MONTH,
                month_ref,
                float(settings.budgets.monthly_cap_usd),
            ),
        ),
    ]
    global_cap = settings.budgets.global_cap_usd
    if global_cap is not None:
        caps.append(
            (
                BudgetScope.GLOBAL,
                "global",
                _budget_row_cap(ctx, BudgetScope.GLOBAL, "global", float(global_cap)),
            )
        )
    for scope, ref, cap in caps:
        if cap is None:
            continue
        remaining = _remaining_musd(ctx, scope, ref, cap)
        if remaining is not None and worst_case_musd > remaining:
            raise BudgetExceeded(
                f"{label} worst-case cost ({musd_to_usd(worst_case_musd)} USD) exceeds "
                f"the remaining {scope.value} budget (cap {musd_to_usd(cap)} USD, "
                f"available {musd_to_usd(remaining)} USD); refusing before any spend",
                scope=scope.value,
                scope_ref=ref,
                cap_musd=cap,
                held_musd=cap - remaining,
                requested_musd=worst_case_musd,
            )


def _episode_estimate_totals(ctx: RunContext, episode: EpisodeRow) -> dict[str, Any] | None:
    """H2 estimate summed over deliverables (sheets counted once).

    Returns None when any estimate is unavailable — the model-confirmation
    problems already fail closed in that case; never emit a $0 total.
    """
    deliverables = ctx.repos.content.list_deliverables(episode.id)
    if not deliverables:
        return None
    expected = 0
    high = 0
    sheets_expected = 0
    sheets_high = 0
    estimates: list[dict[str, Any]] = []
    for deliverable in deliverables:
        estimate = media_estimate(ctx, deliverable.id)
        if not estimate.available:
            return None
        expected += (
            estimate.tts["expected_musd"]
            + estimate.keyframes["expected_musd"]
            + estimate.video["expected_musd"]
        )
        high += (
            estimate.tts["high_musd"]
            + estimate.keyframes["high_musd"]
            + estimate.video["high_musd"]
        )
        sheets_expected = max(sheets_expected, estimate.sheets["expected_musd"])
        sheets_high = max(sheets_high, estimate.sheets["high_musd"])
        estimates.append(estimate.as_dict())
    return {
        "expected_musd": expected + sheets_expected,
        "high_musd": high + sheets_high,
        "sheets_expected_musd": sheets_expected,
        "sheets_high_musd": sheets_high,
        "per_deliverable": estimates,
    }


def _s4_baseline_musd(ctx: RunContext, deliverable_ids: list[str]) -> int | None:
    baselines: list[int] = []
    for deliverable_id in deliverable_ids:
        output = _stage_output(ctx, _S4_STAGE, deliverable_id)
        if not isinstance(output, dict):
            continue
        for key in ("projected_video_cost_musd", "gate1_video_estimate_musd"):
            value = output.get(key)
            if isinstance(value, int) and value >= 0:
                baselines.append(value)
                break
    return max(baselines) if baselines else None


def _estimate_drift(
    ctx: RunContext, *, projected_musd: int
) -> dict[str, Any] | None:
    """Same shape as S6's drift check (§F/§I). Returns the drift dict when the
    S4 baseline exists and is exceeded; emits the `estimate_drift` audit."""
    episode = _episode(ctx)
    if episode is None:
        return None
    deliverables = [
        d.id for d in ctx.repos.content.list_deliverables(episode.id)
        if Language(d.language) in ctx.languages
    ]
    baseline = _s4_baseline_musd(ctx, deliverables)
    if baseline is None:
        return None
    import math

    warn_pct = float(ctx.config.settings.timing.estimate_drift_warn_pct)
    if baseline == 0:
        exceeded = projected_musd > 0
        drift_pct: float | None = None if projected_musd == 0 else math.inf
    else:
        drift_pct = (projected_musd - baseline) * 100.0 / baseline
        exceeded = drift_pct > warn_pct
    if not exceeded:
        return None
    ctx.repos.audit.append(
        actor="system",
        entity=f"episode:{episode.id}",
        event="estimate_drift",
        payload={
            "episode_id": episode.id,
            "track_id": None,
            "baseline_musd": baseline,
            "projected_musd": projected_musd,
            "drift_pct": drift_pct,
            "estimate_drift_warn_pct": warn_pct,
        },
    )
    return {
        "baseline_musd": baseline,
        "projected_musd": projected_musd,
        "drift_pct": drift_pct,
    }


def _clip_cost_musd(entry: ModelEntry, clip_duration_ms: int, shots: int) -> int:
    """Exact first-pass cost from S6's locked durations (§I output key)."""
    from decimal import Decimal

    from studio.config.registry import PerSecond, PerVideo

    if isinstance(entry.pricing, PerSecond):
        return usd_to_musd(Decimal(str(clip_duration_ms / 1000.0)) * entry.pricing.usd)
    if isinstance(entry.pricing, PerVideo):
        return usd_to_musd(Decimal(shots) * entry.pricing.usd)
    raise MediaConfigError(
        f"clip cost projection requires per_second or per_video pricing; "
        f"got {entry.pricing.unit!r}"
    )


def _base_nonce(nonce: str) -> str:
    head, sep, tail = nonce.rpartition(_NONCE_RETRY_MARKER)
    if sep and head and tail.isdigit():
        return head
    return nonce


def _shot_plan_camera(ctx: RunContext, track_id: str, shot_no: int) -> str:
    return _shot_camera(ctx, track_id).get(shot_no, "")


@dataclass
class _PreSubmitState:
    nonce: str
    sanitize_level: int = 0
    refusal_log: list[dict[str, Any]] = field(default_factory=list)
    scene_override: str | None = None


@dataclass
class _GenerationOutcome:
    asset: AssetRef
    conformed_asset: AssetRef | None
    provider_call_id: str | None
    cost_musd: int
    checks: list[QcCheck]
    sanitize_level: int
    refusal_log: list[dict[str, Any]]
    nonce: str
    paid_calls: int
    report: dict[str, Any]


QcOutcome = tuple[list[QcCheck], AssetRef | None, dict[str, Any]]
QcRunner = Callable[[Path], Awaitable[QcOutcome]]
PersistHook = Callable[[dict[str, Any]], None]


def _prompt_spec_payload(state: _PreSubmitState, spec: PromptSpec) -> dict[str, Any]:
    return {
        "nonce": state.nonce,
        "prompt_spec_json": _json(spec.as_dict()),
        "sanitize_level": state.sanitize_level,
        "refusal_log_json": _json(state.refusal_log),
    }


def _qc_status(checks: list[QcCheck]) -> str:
    statuses = [check.status for check in checks]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


async def _generate_with_retries(
    ctx: RunContext,
    *,
    episode_id: str,
    subject_key: str,
    purpose: str,
    kind: Literal["image", "video"],
    model_key: str,
    make_prompt: Callable[[str | None], PromptSpec],
    make_request: Callable[[PromptSpec, int, str], MediaRequest],
    base_nonce: str,
    max_paid_attempts: int,
    auto_retries: int,
    qc_run: QcRunner,
    persist: PersistHook | None,
    resume_state: _PreSubmitState | None = None,
    accumulate_cost: int = 0,
) -> _GenerationOutcome:
    """One shot/sheet/clip generation with sanitizer + QC retry policy.

    ``resume_state`` is non-None when the persisted attempt already shows a
    sanitized prompt (crash after a refusal); generation resumes there with
    zero extra sanitizer calls.
    """
    settings = ctx.config.settings
    cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
    state = resume_state or _PreSubmitState(nonce=base_nonce)
    attempts_used = 0
    regenerations = 0
    cost_total = accumulate_cost
    subject_note = f"{purpose} {subject_key}"
    while True:
        if attempts_used >= max_paid_attempts:
            raise MediaAttemptExhausted(
                f"{subject_note}: exhausted media.max_paid_attempts_per_shot "
                f"({max_paid_attempts})"
            )
        spec = make_prompt(state.scene_override)
        if persist is not None:
            persist({**_prompt_spec_payload(state, spec), "qc_status": "pending"})
        request = make_request(
            spec, seed_for(episode_id, subject_key, state.nonce), state.nonce
        )
        call_ctx = CallContext(
            episode_id=episode_id,
            stage_run_id=ctx.stage_run_id,
            nonce=state.nonce,
            purpose=purpose,
            gate_cleared=ctx.gate_cleared,
        )
        try:
            if kind == "image":
                result = await ctx.gateway.image(model_key, request, call_ctx)
            else:
                result = await ctx.gateway.video(model_key, request, call_ctx)
        except ContentPolicyError as exc:
            attempts_used += 1
            log_entry: dict[str, Any] = {
                "level": state.sanitize_level + 1,
                "original": spec.scene_text,
                "error": str(exc),
                "billed": bool(exc.billed),
                "at": format_ts(ctx.clock.now()),
            }
            if state.sanitize_level >= int(settings.sanitizer.max_levels):
                state.refusal_log.append(log_entry)
                if persist is not None:
                    persist(
                        {
                            **_prompt_spec_payload(state, spec),
                            "qc_status": "failed_policy",
                            "policy_blocked": 1,
                            "refusal_log_json": _json(state.refusal_log),
                            "cost_musd": cost_total,
                        }
                    )
                raise FailedPolicy(
                    f"{subject_note}: policy refusal after {state.sanitize_level} "
                    f"sanitize level(s); parked for manual prompt edit "
                    f"(studio shot regenerate --prompt-file)"
                ) from exc
            rewrite = await sanitize_scene_text(
                gateway=ctx.gateway,
                model_key=settings.sanitizer.model_key,
                scene_text=spec.scene_text,
                level=state.sanitize_level + 1,
                episode_id=episode_id,
                stage_run_id=ctx.stage_run_id,
                max_chars=int(settings.sanitizer.max_scene_chars),
            )
            log_entry["rewritten"] = rewrite
            state.refusal_log.append(log_entry)
            if rewrite is None:
                if persist is not None:
                    persist(
                        {
                            **_prompt_spec_payload(state, spec),
                            "qc_status": "failed_policy",
                            "policy_blocked": 1,
                            "refusal_log_json": _json(state.refusal_log),
                            "cost_musd": cost_total,
                        }
                    )
                raise FailedPolicy(
                    f"{subject_note}: sanitizer level {state.sanitize_level + 1} "
                    f"produced no valid rewrite"
                ) from exc
            state.sanitize_level += 1
            state.scene_override = rewrite
            spec = spec.with_scene(rewrite)
            if persist is not None:
                persist(
                    {
                        **_prompt_spec_payload(state, spec),
                        "qc_status": "pending",
                        "refusal_log_json": _json(state.refusal_log),
                    }
                )
            continue
        attempts_used += 1
        cost_total += int(result.cost_musd)
        asset = result.assets[0]
        path = cas.resolve(asset.sha256)
        checks, conformed, report = await qc_run(path)
        status = _qc_status(checks)
        if persist is not None:
            payload = {
                **_prompt_spec_payload(state, spec),
                "qc_status": status if status != "fail" else "fail",
                "qc_json": _json([c.as_dict() for c in checks]),
                "asset_id": asset.sha256,
                "provider_call_id": result.provider_call_id,
                "cost_musd": cost_total,
            }
            if conformed is not None:
                payload["conformed_asset_id"] = conformed.sha256
            persist(payload)
        if status != "fail":
            return _GenerationOutcome(
                asset=asset,
                conformed_asset=conformed,
                provider_call_id=result.provider_call_id,
                cost_musd=cost_total,
                checks=checks,
                sanitize_level=state.sanitize_level,
                refusal_log=list(state.refusal_log),
                nonce=state.nonce,
                paid_calls=attempts_used,
                report=report,
            )
        retryable_fail = any(c.status == "fail" and c.retryable for c in checks)
        failing = [c.name for c in checks if c.status == "fail"]
        if not retryable_fail:
            raise MediaCircuitOpen(
                f"{subject_note}: non-retryable QC failure ({', '.join(failing)})"
            )
        if regenerations >= auto_retries:
            raise VisualQcError(
                f"{subject_note}: QC failed after {auto_retries} regeneration(s) "
                f"({', '.join(failing)})"
            )
        regenerations += 1
        state.nonce = f"{base_nonce}{_NONCE_RETRY_MARKER}{regenerations}"


def _resume_state(attempt: ShotAttemptRow | None) -> _PreSubmitState | None:
    if attempt is None or int(attempt.sanitize_level) <= 0:
        return None
    scene_override: str | None = None
    if attempt.prompt_spec_json:
        try:
            payload = json.loads(attempt.prompt_spec_json)
            raw = payload.get("scene_text") if isinstance(payload, dict) else None
            scene_override = str(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            scene_override = None
    try:
        refusal_log = json.loads(attempt.refusal_log_json or "[]")
    except json.JSONDecodeError:
        refusal_log = []
    return _PreSubmitState(
        nonce=attempt.nonce,
        sanitize_level=int(attempt.sanitize_level),
        refusal_log=refusal_log if isinstance(refusal_log, list) else [],
        scene_override=scene_override,
    )


def _attempt_persist(ctx: RunContext, attempt_id: str):  # type: ignore[no-untyped-def]
    def persist(payload: dict[str, Any]) -> None:
        clean = {k: v for k, v in payload.items() if k in _ATTEMPT_COLUMNS}
        if clean:
            ctx.repos.content.update_shot_attempt(attempt_id, **clean)

    return persist


_ATTEMPT_COLUMNS = frozenset(
    {
        "nonce",
        "prompt_spec_json",
        "sanitize_level",
        "refusal_log_json",
        "qc_status",
        "qc_json",
        "asset_id",
        "conformed_asset_id",
        "cost_musd",
        "provider_call_id",
        "policy_blocked",
    }
)


def _create_attempt(
    ctx: RunContext,
    *,
    shot: ShotRow,
    kind: str,
    prompt: str,
) -> ShotAttemptRow:
    attempt_no = ctx.repos.content.next_shot_attempt_no(shot.id)
    return ctx.repos.content.create_shot_attempt(
        shot_id=shot.id,
        attempt_no=attempt_no,
        nonce=ctx.ids.new(),
        prompt_used=prompt,
        kind=kind,
    )


def _ensure_attempt(
    ctx: RunContext, *, shot: ShotRow, kind: str, prompt: str
) -> ShotAttemptRow:
    attempt = ctx.repos.content.current_shot_attempt(shot.id, kind=kind)
    if attempt is not None:
        return attempt
    return _create_attempt(ctx, shot=shot, kind=kind, prompt=prompt)


def _approved_sheet_assets(
    ctx: RunContext, series_id: str, character_id: str, roles: list[str]
) -> dict[str, AssetRef]:
    assets: dict[str, AssetRef] = {}
    for role in roles:
        row = ctx.repos.sheets.approved(series_id, character_id, role)
        if row is None:
            continue
        asset_row = ctx.repos.assets.get(row.asset_id)
        if asset_row is None:
            continue
        assets[role] = AssetRef(
            sha256=asset_row.sha256,
            rel_path=asset_row.rel_path,
            kind=asset_row.kind,
            mime=asset_row.mime,
            size_bytes=asset_row.size_bytes,
            duration_ms=asset_row.duration_ms,
            width=asset_row.width,
            height=asset_row.height,
        )
    return assets


def _ref_assets_for_shot(
    ctx: RunContext,
    *,
    series_id: str,
    characters: list[str],
    entry: ModelEntry,
) -> tuple[list[tuple[str, str, AssetRef]], bool]:
    """Approved sheet refs for the shot's characters, up to max_refs.

    Returns ((character_id, role, asset) tuples in prominence order,
    refs_truncated). Prominence order = order of first appearance in the
    shot plan; ref_full first, then extra configured roles per character.
    """
    roles = [role for role in ctx.config.settings.character_sheets.roles]
    ordered: list[tuple[str, str, AssetRef]] = []
    for character_id in characters:
        assets = _approved_sheet_assets(ctx, series_id, character_id, roles)
        for role in roles:
            asset = assets.get(role)
            if asset is not None:
                ordered.append((character_id, role, asset))
    limit = max_refs(entry)
    if limit <= 0:
        return [], bool(ordered)
    overflow = ctx.config.settings.media.refs_overflow
    if len(ordered) <= limit:
        return ordered, False
    if overflow == "fail":
        raise MediaConfigError(
            f"shot references {len(ordered)} sheet assets but the model declares "
            f"max_refs={limit} and media.refs_overflow=fail"
        )
    logger.warning(
        "shot references %d sheet assets; keeping the first %d (refs_overflow=primary_only)",
        len(ordered),
        limit,
    )
    return ordered[:limit], True


async def _prepare_input_assets(
    ctx: RunContext, refs: list[AssetRef]
) -> tuple[list[AssetRef], list[Any]]:
    settings = ctx.config.settings
    if not refs:
        return [], []
    cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
    prepared = await prepare_refs(cas, refs, settings)
    input_assets = [item.ref for item in prepared]
    return input_assets, list(prepared)


def _image_request(
    ctx: RunContext,
    *,
    entry: ModelEntry,
    spec: PromptSpec,
    negative: str | None,
    input_assets: list[AssetRef],
    prepared: list[Any],
    aspect: Aspect,
    seed: int,
    nonce: str,
    purpose: str,
) -> MediaRequest:
    params: dict[str, Any] = {"prompt": spec.full_prompt}
    if negative is not None and "negative" in media_spec(entry).param_map:
        params["negative"] = negative
    if input_assets:
        use_data_uri = media_spec(entry).input_transport == "data_uri"

        def wire_ref(prepared_item: Any) -> str:
            return prepared_item.data_uri if use_data_uri else str(prepared_item.local_path)

        if "refs" in media_spec(entry).param_map:
            params["refs"] = [wire_ref(p) for p in prepared]
        elif "image" in media_spec(entry).param_map:
            params["image"] = wire_ref(prepared[0])
    if input_assets:
        total = sum(len(p.data_uri.encode("utf-8")) for p in prepared)
        if media_spec(entry).input_transport == "data_uri" and (
            total > int(ctx.config.settings.media.max_inline_bytes)
        ):
            raise MediaConfigError(
                f"prepared refs exceed media.max_inline_bytes "
                f"({total} > {ctx.config.settings.media.max_inline_bytes}); "
                f"use the upload transport or fewer refs"
            )
    return MediaRequest(
        capability="image_reference_generate",
        params=params,
        input_assets=input_assets,
        aspect=aspect,
        duration_s=None,
        seed=seed,
        prompt_version_sha=spec.prompt_version_sha,
    )


def _video_request(
    ctx: RunContext,
    *,
    entry: ModelEntry,
    prompt: str,
    keyframe: AssetRef,
    keyframe_prepared: Any,
    aspect: Aspect,
    duration_s: float,
    seed: int,
    prompt_sha: str,
) -> MediaRequest:
    params: dict[str, Any] = {"prompt": prompt}
    transport = media_spec(entry).input_transport
    if "image" in media_spec(entry).param_map:
        if transport == "data_uri":
            params["image"] = keyframe_prepared.data_uri
            total = len(keyframe_prepared.data_uri.encode("utf-8"))
            if total > int(ctx.config.settings.media.max_inline_bytes):
                raise MediaConfigError(
                    f"keyframe data URI exceeds media.max_inline_bytes ({total} bytes)"
                )
        else:
            params["image"] = str(keyframe_prepared.local_path)
    if "negative" in media_spec(entry).param_map:
        from studio.media.prompts import load_templates as _load_templates

        params["negative"] = _load_templates(
            Path(ctx.config.settings.paths.home)
        )["negative"].strip()
    return MediaRequest(
        capability="image_to_video",
        params=params,
        input_assets=[keyframe],
        aspect=aspect,
        duration_s=duration_s,
        seed=seed,
        prompt_version_sha=prompt_sha,
    )





# ==================================================================
# S4p — media preflight (EPISODE scope; free; fail closed)
# ==================================================================


class MediaPreflightHandler:
    """Collects every media problem at once, then halts before any spend."""

    _STAGE = "s4p_media_preflight"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _episode_subject(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        settings = ctx.config.settings
        return _sha(
            {
                "stage": self._STAGE,
                "episode_id": subject.id,
                "profile": ctx.resolved_profile.name,
                "languages": sorted(lang.value for lang in ctx.languages),
                "aspects": sorted(aspect.value for aspect in ctx.aspects),
                "media_estimate": media_estimate_hash_parts(ctx),
                "sanitizer": {
                    "max_levels": settings.sanitizer.max_levels,
                    "model_key": settings.sanitizer.model_key,
                    "max_scene_chars": settings.sanitizer.max_scene_chars,
                },
                "media": {
                    "max_paid_attempts_per_shot": settings.media.max_paid_attempts_per_shot,
                    "max_inline_bytes": settings.media.max_inline_bytes,
                    "refs_overflow": settings.media.refs_overflow,
                    "ref_max_side_px": settings.media.ref_max_side_px,
                    "chain_mode": settings.media.chain_mode,
                },
                "character_sheets": {
                    "roles": list(settings.character_sheets.roles),
                    "aspect": settings.character_sheets.aspect,
                },
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        problems: list[str] = []
        final = Profile(ctx.resolved_profile.name) is Profile.FINAL

        # ---- registry: every model resolves, confirmed, prices verified
        entries: dict[str, tuple[str, ModelEntry]] = {}
        for role in ("sheets", "keyframes", "clips"):
            try:
                entries[role] = _model_entry(ctx, role)
            except Exception as exc:  # reported by _media_model_problems below
                logger.debug("media model for role %s unresolved: %s", role, exc)
        bible = _pinned_bible(ctx, episode.id)
        characters = [c.name for c in bible.characters] if bible is not None else []
        problems.extend(_media_model_problems(ctx, final=final, need_refs=bool(characters)))
        problems.extend(_aspect_problems(ctx, entries))
        if settings.media.chain_mode != "off":
            problems.append(
                f"media.chain_mode is {settings.media.chain_mode!r}; "
                f"only 'off' is supported before Phase 5"
            )
        # ---- bible character names must be unique (sheet identity)
        if characters:
            seen: set[str] = set()
            duplicates_set: set[str] = set()
            for name in characters:
                if name in seen:
                    duplicates_set.add(name)
                seen.add(name)
            if duplicates_set:
                problems.append(
                    f"series bible lists duplicate character names: "
                    f"{', '.join(sorted(duplicates_set))}"
                )

        if problems:
            raise PreflightError(
                "media preflight failed before any spend:\n- " + "\n- ".join(sorted(problems))
            )

        # ---- affordability: halt when the expected total exceeds the
        # remaining episode or monthly budget; warn when only high does.
        totals = _episode_estimate_totals(ctx, episode)
        affordability: dict[str, Any] | None = None
        if totals is None:
            ctx.repos.audit.append(
                actor="system",
                entity=f"episode:{episode.id}",
                event="media_affordability_skipped",
                payload={"episode_id": episode.id,
                         "reason": "media estimate unavailable (see preflight model checks)"},
            )
        else:
            from studio.domain.enums import GLOBAL_SCOPE_REF

            month_ref = ctx.clock.month_tag()
            caps: list[tuple[BudgetScope, str, int | None]] = [
                (
                    BudgetScope.EPISODE,
                    episode.id,
                    _budget_row_cap(
                        ctx, BudgetScope.EPISODE, episode.id,
                        float(settings.budgets.default_episode_cap_usd),
                    ),
                ),
                (
                    BudgetScope.MONTH,
                    month_ref,
                    _budget_row_cap(
                        ctx, BudgetScope.MONTH, month_ref,
                        float(settings.budgets.monthly_cap_usd),
                    ),
                ),
            ]
            if settings.budgets.global_cap_usd is not None:
                caps.append(
                    (
                        BudgetScope.GLOBAL,
                        GLOBAL_SCOPE_REF,
                        _budget_row_cap(
                            ctx, BudgetScope.GLOBAL, GLOBAL_SCOPE_REF,
                            float(settings.budgets.global_cap_usd),
                        ),
                    )
                )
            for scope, ref, cap in caps:
                if cap is None:
                    continue
                remaining = _remaining_musd(ctx, scope, ref, cap)
                if remaining is None:
                    continue
                if totals["expected_musd"] > remaining:
                    raise BudgetExceeded(
                        f"expected media spend ({musd_to_usd(totals['expected_musd'])} USD) "
                        f"exceeds the remaining {scope.value} budget "
                        f"(cap {musd_to_usd(cap)} USD, available {musd_to_usd(remaining)} USD); "
                        f"refusing before any spend",
                        scope=scope.value,
                        scope_ref=ref,
                        cap_musd=cap,
                        held_musd=cap - remaining,
                        requested_musd=int(totals["expected_musd"]),
                    )
                if totals["high_musd"] > remaining:
                    ctx.repos.audit.append(
                        actor="system",
                        entity=f"episode:{episode.id}",
                        event="media_estimate_high_exceeds_budget",
                        payload={
                            "episode_id": episode.id,
                            "scope": scope.value,
                            "scope_ref": ref,
                            "cap_musd": cap,
                            "remaining_musd": remaining,
                            "expected_musd": totals["expected_musd"],
                            "high_musd": totals["high_musd"],
                        },
                    )
            affordability = {
                "expected_musd": totals["expected_musd"],
                "high_musd": totals["high_musd"],
                "sheets_expected_musd": totals["sheets_expected_musd"],
                "headroom_by_scope": {
                    scope.value: (
                        _remaining_musd(ctx, scope, ref, cap)
                         if cap is not None else None
                    )
                    for scope, ref, cap in caps
                },
            }

        # ---- per-deliverable estimate additive keys (for the operator)
        estimate_out: dict[str, Any] | None = None
        for deliverable in ctx.repos.content.list_deliverables(episode.id):
            if estimate_out is None:
                estimate_out = media_estimate(ctx, deliverable.id).additive_output()
        return StageOutput(
            output={
                "episode_id": episode.id,
                "languages": sorted(lang.value for lang in ctx.languages),
                "aspects": sorted(aspect.value for aspect in ctx.aspects),
                "characters": characters,
                "models": {role: key for role, (key, _e) in sorted(entries.items())},
                "estimate": estimate_out,
                "affordability": affordability,
            }
        )


# ==================================================================
# S10p — clip preflight (EPISODE scope; free; runs after G2)
# ==================================================================


class ClipPreflightHandler:
    """Rechecks model readiness and halts if the exact first-pass clip cost
    exceeds the remaining budget. Retries are protected per call by the
    gateway reservation."""

    _STAGE = "s10p_clip_preflight"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _episode_subject(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        shots = _track_shots(ctx)
        clips_key = None
        durations: list[int] = []
        price_unit = None
        try:
            clips_key, clips_entry = _model_entry(ctx, "clips")
            durations = list(supported_durations_s(clips_entry))
            price_unit = clips_entry.pricing.unit
        except Exception as exc:
            logger.debug("clips model unresolved during preflight hash: %s", exc)
        settings = ctx.config.settings
        return _sha(
            {
                "stage": self._STAGE,
                "episode_id": subject.id,
                "profile": ctx.resolved_profile.name,
                "video_model_key": clips_key,
                "price_unit": price_unit,
                "supported_durations_s": durations,
                "locked": [
                    {"shot_id": shot.id, "clip_duration_ms": shot.clip_duration_ms}
                    for shot in shots
                ],
                "fps": settings.render.fps,
                "media_estimate": media_estimate_hash_parts(ctx),
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        final = Profile(ctx.resolved_profile.name) is Profile.FINAL
        problems = _media_model_problems(ctx, final=final, need_refs=False)

        shots = _track_shots(ctx)
        try:
            clips_key, clips_entry = _model_entry(ctx, "clips")
        except Exception as exc:
            problems.append(f"clips: {exc}")
            clips_key, clips_entry = None, None

        supported_ms: list[int] = []
        if clips_entry is not None:
            try:
                supported_ms = [d * 1000 for d in supported_durations_s(clips_entry)]
            except MediaConfigError as exc:
                problems.append(f"clips: {exc}")
        for shot in shots:
            if shot.clip_duration_ms is None or shot.timing_status != "locked":
                problems.append(
                    f"shot {shot.shot_no} ({shot.id}) has no locked duration; run S6 first"
                )
                continue
            if supported_ms and int(shot.clip_duration_ms) not in supported_ms:
                problems.append(
                    f"shot {shot.shot_no} ({shot.id}) locked duration "
                    f"{shot.clip_duration_ms} ms is not in the clips model's "
                    f"supported durations {sorted(supported_ms)}; re-run S6"
                )
        if problems:
            raise PreflightError(
                "clip preflight failed; no video call will be made:\n- "
                + "\n- ".join(sorted(problems))
            )
        assert clips_entry is not None and clips_key is not None

        # ---- exact first-pass worst case (locked durations only)
        from decimal import Decimal

        from studio.config.registry import PerSecond, PerVideo

        total = 0
        seconds = 0.0
        for shot in shots:
            locked_ms = int(shot.clip_duration_ms or 0)
            seconds += locked_ms / 1000.0
            if isinstance(clips_entry.pricing, PerSecond):
                total += usd_to_musd(Decimal(locked_ms) / Decimal(1000) * clips_entry.pricing.usd)
            elif isinstance(clips_entry.pricing, PerVideo):
                total += usd_to_musd(Decimal(1) * clips_entry.pricing.usd)
            # zero-priced simulated models (sandbox) reserve nothing
        _worst_case_pause(
            ctx,
            episode,
            worst_case_musd=total,
            label="first-pass clip generation",
        )
        ctx.repos.audit.append(
            actor="system",
            entity=f"episode:{episode.id}",
            event="clip_preflight_passed",
            payload={
                "episode_id": episode.id,
                "shots": len(shots),
                "video_model_key": clips_key,
                "first_pass_worst_case_musd": total,
                "locked_seconds": seconds,
            },
        )
        return StageOutput(
            output={
                "episode_id": episode.id,
                "shots": len(shots),
                "video_model_key": clips_key,
                "first_pass_worst_case_musd": total,
                "locked_seconds": seconds,
            }
        )


# ==================================================================
# S8a — character sheets (subject: one missing (character, role); Gate G0)
# ==================================================================


def _pixel_dims_for(entry: ModelEntry, aspect: Aspect) -> tuple[int, int] | None:
    raw: dict[str, Any] = {}
    raw.update(media_spec(entry).fixed_params)
    raw.update(media_spec(entry).aspect_extra_params.get(aspect.value, {}))
    width, height = raw.get("width"), raw.get("height")
    if width is None or height is None:
        return None
    if "<CONFIRM" in str(width) or "<CONFIRM" in str(height):
        return None
    try:
        return int(width), int(height)
    except (TypeError, ValueError):
        return None


def _estimate_image_cost(entry: ModelEntry, *, aspect: Aspect) -> int | None:
    """Per-image worst case in micro-USD; None when not computable offline."""
    from decimal import Decimal

    from studio.config.registry import PerImage, PerMegapixel, PerVideo

    pricing = entry.pricing
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


def _estimate_clip_cost(entry: ModelEntry, *, clip_duration_ms: int) -> int | None:
    """Per-clip worst case in micro-USD (locked duration)."""
    from decimal import Decimal

    from studio.config.registry import PerSecond, PerVideo

    pricing = entry.pricing
    if isinstance(pricing, PerSecond):
        return usd_to_musd(Decimal(clip_duration_ms) / Decimal(1000) * pricing.usd)
    if isinstance(pricing, PerVideo):
        return usd_to_musd(Decimal(1) * pricing.usd)
    return None


def _media_failure_audit(
    ctx: RunContext,
    *,
    stage: str,
    episode_id: str | None,
    subject_id: str,
    error: Exception,
) -> None:
    ctx.repos.audit.append(
        actor="system",
        entity=f"stage:{stage}",
        event="media_attempt_failed",
        payload={
            "stage": stage,
            "episode_id": episode_id,
            "subject_id": subject_id,
            "error": str(error),
            "error_type": type(error).__name__,
        },
    )


def _sheet_spec_hash(
    bible: Any, character_name: str, role: str, style_tokens: list[str]
) -> str:
    character = _bible_character(bible, character_name)
    payload = {
        "name": character_name,
        "description": character.description if character is not None else "",
        "visual_tokens": list(character.visual_tokens) if character is not None else [],
        "style_tokens": list(style_tokens),
        "role": role,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _sheet_precheck_subjects(ctx: RunContext) -> list[tuple[str, str]]:
    """Missing (character_id, role) pairs only. Zero calls, zero gate when
    everything is already approved."""
    episode = _episode(ctx)
    if episode is None:
        return []
    bible = _pinned_bible(ctx, episode.id)
    if bible is None:
        return []
    roles = list(ctx.config.settings.character_sheets.roles)
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []
    for character in bible.characters:
        name = character.name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        for role in roles:
            approved = ctx.repos.sheets.approved(episode.series_id, name, role)
            if approved is None:
                missing.append((name, role))
    return missing


class CharacterSheetHandler:
    _STAGE = "s8a_character_sheets"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        episode = _episode(ctx)
        if episode is None:
            return []
        missing = _sheet_precheck_subjects(ctx)
        if not missing:
            approved = ctx.repos.sheets.approved_for_series(episode.series_id)
            if approved:
                ctx.repos.audit.append(
                    actor="system",
                    entity=f"series:{episode.series_id}",
                    event="character_sheets_all_approved",
                    payload={
                        "series_id": episode.series_id,
                        "approved": [f"{row.character_id}:{row.role}" for row in approved],
                    },
                )
            return []
        return [
            SubjectRef(
                scope=Scope.SERIES,
                id=f"{character_id}::{role}",
                lineage=(
                    (Scope.SERIES, episode.series_id),
                    (Scope.EPISODE, episode.id),
                ),
            )
            for character_id, role in missing
        ]

    def _parts(self, subject: SubjectRef) -> tuple[str, str]:
        character_id, sep, role = subject.id.partition("::")
        if not sep or not character_id or not role:
            raise StudioError(f"malformed sheet subject: {subject.id!r}")
        return character_id, role

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        character_id, role = self._parts(subject)
        bible = _pinned_bible(ctx, episode.id)
        settings = ctx.config.settings
        roles = list(settings.character_sheets.roles)
        is_primary = bool(roles) and role == roles[0]
        if is_primary:
            model_key, _entry = _model_entry(ctx, "sheets")
        else:
            model_key, _entry = _model_entry(ctx, "keyframes")
        home = Path(settings.paths.home)
        return _sha(
            {
                "stage": self._STAGE,
                "series_id": episode.series_id,
                "character": character_id,
                "character_spec": {
                    "description": (
                        _bible_character(bible, character_id).description
                        if _bible_character(bible, character_id) is not None
                        else ""
                    ),
                    "visual_tokens": (
                        list(_bible_character(bible, character_id).visual_tokens)
                        if _bible_character(bible, character_id) is not None
                        else []
                    ),
                },
                "role": role,
                "is_primary_role": is_primary,
                "style_tokens": _style_tokens(bible),
                "aspect": settings.character_sheets.aspect,
                "model_key": model_key,
                "prompt_version_sha": prompt_version_sha(home),
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        character_id, role = self._parts(subject)
        bible = _pinned_bible(ctx, episode.id)
        if bible is None:
            raise StudioError(
                "s8a_character_sheets requires the series bible; run the text stages first"
            )
        if _bible_character(bible, character_id) is None:
            raise PermanentProviderError(
                f"character {character_id!r} is not in the pinned series bible"
            )
        roles = list(settings.character_sheets.roles)
        is_primary = bool(roles) and role == roles[0]
        style_tokens = _style_tokens(bible)
        visual_spec = _character_visual_spec(bible, character_id)
        home = Path(settings.paths.home)

        # ---- ref conditioning for later roles (ref_full's sha)
        ref_assets: list[AssetRef] = []
        if not is_primary and roles:
            primary = ctx.repos.sheets.approved(episode.series_id, character_id, roles[0])
            if primary is None:
                raise PreflightError(
                    f"cannot generate {role!r} for {character_id!r}: no approved "
                    f"{roles[0]!r} sheet exists yet"
                )
            asset_row = ctx.repos.assets.get(primary.asset_id)
            if asset_row is None:
                raise PermanentProviderError(
                    f"approved {roles[0]} sheet {primary.id} lost its asset {primary.asset_id}"
                )
            ref_assets.append(
                AssetRef(
                    sha256=asset_row.sha256,
                    rel_path=asset_row.rel_path,
                    kind=asset_row.kind,
                    mime=asset_row.mime,
                    size_bytes=asset_row.size_bytes,
                    duration_ms=asset_row.duration_ms,
                    width=asset_row.width,
                    height=asset_row.height,
                )
            )

        if is_primary:
            model_key, entry = _model_entry(ctx, "sheets")
        else:
            model_key, entry = _model_entry(ctx, "keyframes")
        ref_clause_template = _ref_clause(entry)

        input_assets: list[AssetRef] = []
        prepared: list[Any] = []
        if ref_assets:
            input_assets, prepared = await _prepare_input_assets(ctx, ref_assets)

        # ---- budget: one sheet, worst case with the assumed retry rate
        worst = _estimate_image_cost(entry, aspect=Aspect(settings.character_sheets.aspect))
        if worst is not None:
            uplift = float(settings.media.assumed_retry_rate.get("image", 0.25))
            worst = int(worst * (1 + uplift)) + 1
            _worst_case_pause(
                ctx,
                episode,
                worst_case_musd=worst,
                label=f"character sheet {character_id}/{role}",
            )

        def make_prompt(scene_override: str | None) -> PromptSpec:
            spec = compose_sheet_prompt(
                character_name=character_id,
                visual_spec=visual_spec,
                role=role,
                style_tokens=style_tokens,
                ref_clause_template=ref_clause_template if ref_assets else None,
                ref_clauses=[(character_id, 1)] if ref_assets else None,
                home=home,
            )
            if scene_override is not None:
                spec = spec.with_scene(scene_override)
            return spec

        def make_request(spec: PromptSpec, seed: int, nonce: str) -> MediaRequest:
            return _image_request(
                ctx,
                entry=entry,
                spec=spec,
                negative=spec.negative if "negative" in media_spec(entry).param_map else None,
                input_assets=input_assets,
                prepared=prepared,
                aspect=Aspect(settings.character_sheets.aspect),
                seed=seed,
                nonce=nonce,
                purpose="character_sheet",
            )

        async def qc_run(path: Path) -> QcOutcome:
            checks = await qc_image(
                path, aspect=Aspect(settings.character_sheets.aspect), settings=settings
            )
            return checks, None, {}

        def persist_audit(payload: dict[str, Any]) -> None:
            ctx.repos.audit.append(
                actor="system",
                entity=f"sheet:{character_id}:{role}",
                event="character_sheet_attempt",
                payload={
                    "episode_id": episode.id,
                    "character_id": character_id,
                    "role": role,
                    "stage_run_id": ctx.stage_run_id,
                    **payload,
                },
            )

        subject_key = f"sheet:{character_id}:{role}"
        base_nonce = ctx.ids.new()
        try:
            outcome = await _generate_with_retries(
                ctx,
                episode_id=episode.id,
                subject_key=subject_key,
                purpose="character_sheet",
                kind="image",
                model_key=model_key,
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce=base_nonce,
                max_paid_attempts=int(settings.media.max_paid_attempts_per_shot),
                auto_retries=int(settings.media.qc.image.auto_retries),
                qc_run=qc_run,
                persist=persist_audit,
            )
        except (FailedPolicy, VisualQcError, MediaCircuitOpen, MediaJobFailedError) as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
            raise

        version = ctx.repos.sheets.latest_version(character_id, role) + 1
        spec_hash = _sheet_spec_hash(bible, character_id, role, style_tokens)
        sheet = ctx.repos.sheets.create(
            series_id=episode.series_id,
            character_id=character_id,
            role=role,
            version=version,
            asset_id=outcome.asset.sha256,
            spec_hash=spec_hash,
            nonce=outcome.nonce,
            status="candidate",
        )
        ctx.repos.audit.append(
            actor="system",
            entity=f"sheet:{sheet.id}",
            event="character_sheet_generated",
            payload={
                "episode_id": episode.id,
                "series_id": episode.series_id,
                "character_id": character_id,
                "role": role,
                "sheet_id": sheet.id,
                "version": version,
                "asset_sha256": outcome.asset.sha256,
                "spec_hash": spec_hash,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc": [c.as_dict() for c in outcome.checks],
            },
        )
        return StageOutput(
            output={
                "series_id": episode.series_id,
                "character_id": character_id,
                "role": role,
                "sheet_id": sheet.id,
                "version": version,
                "asset_sha256": outcome.asset.sha256,
                "spec_hash": spec_hash,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc_status": _qc_status(outcome.checks),
                "model_key": model_key,
            }
        )


# ==================================================================
# S8 — keyframes (SHOT scope; paid; Gate G2)
# ==================================================================


class KeyframeHandler:
    _STAGE = "s8_keyframes"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _shot_subjects(ctx)

    def _attempt(self, ctx: RunContext, shot: ShotRow) -> ShotAttemptRow | None:
        return ctx.repos.content.current_shot_attempt(shot.id, kind="keyframe")

    def _approved_refs(
        self, ctx: RunContext, shot: ShotRow, track: TrackRow
    ) -> tuple[list[tuple[str, str, AssetRef]], bool, list[str]]:
        episode = _episode(ctx)
        assert episode is not None
        plan = _shot_plan(ctx, track.id)
        characters = plan.get(shot.shot_no, {}).get("characters", [])
        names = [str(name) for name in characters] if isinstance(characters, list) else []
        _key, entry = _model_entry(ctx, "keyframes")
        refs, truncated = _ref_assets_for_shot(
            ctx, series_id=episode.series_id, characters=names, entry=entry
        )
        return refs, truncated, names

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        shot = ctx.repos.content.get_shot(subject.id)
        assert shot is not None
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        episode = _episode(ctx)
        assert episode is not None
        attempt = self._attempt(ctx, shot)
        refs, _truncated, _names = self._approved_refs(ctx, shot, track)
        bible = _pinned_bible(ctx, episode.id)
        settings = ctx.config.settings
        home = Path(settings.paths.home)
        model_key, _entry = _model_entry(ctx, "keyframes")
        return _sha(
            {
                "stage": self._STAGE,
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no if attempt else 0,
                "nonce": attempt.nonce if attempt else None,
                "prompt": attempt.prompt_used if attempt else shot.prompt,
                "model_key": model_key,
                "aspect": track.aspect,
                "refs": [ref.sha256 for _n, _r, ref in refs],
                "style_tokens": _style_tokens(bible),
                "prompt_version_sha": prompt_version_sha(home),
                "sanitizer_model_key": settings.sanitizer.model_key,
                "ref_max_side_px": settings.media.ref_max_side_px,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        shot = ctx.repos.content.get_shot(subject.id)
        if shot is None:
            raise PermanentProviderError(f"shot not found: {subject.id}")
        track = ctx.repos.content.get_track(shot.track_id)
        if track is None:
            raise PermanentProviderError(f"track not found: {shot.track_id}")
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        home = Path(settings.paths.home)
        aspect = Aspect(track.aspect)
        bible = _pinned_bible(ctx, episode.id)
        style_tokens = _style_tokens(bible)
        plan = _shot_plan(ctx, track.id)
        plan_entry = plan.get(shot.shot_no, {})
        names = [str(n) for n in plan_entry.get("characters", [])] if isinstance(
            plan_entry.get("characters"), list
        ) else []

        model_key, entry = _model_entry(ctx, "keyframes")
        ref_specs, refs_truncated = _ref_assets_for_shot(
            ctx, series_id=episode.series_id, characters=names, entry=entry
        )
        ordered_refs = [ref for _n, _r, ref in ref_specs]
        ref_clauses = [
            (name, index + 1) for index, (name, _role, _ref) in enumerate(ref_specs)
        ]
        input_assets, prepared = await _prepare_input_assets(ctx, ordered_refs)

        # ---- worst-case budget for this shot before any spend
        worst = _estimate_image_cost(entry, aspect=aspect)
        if worst is not None:
            worst = int(
                worst * (1 + float(settings.media.assumed_retry_rate.get("image", 0.25)))
            ) + 1
            _worst_case_pause(
                ctx,
                episode,
                worst_case_musd=worst,
                label=f"keyframe for shot {shot.shot_no}",
            )

        existing_attempt = self._attempt(ctx, shot)
        # A human edit at G2 lives on the (kind=keyframe) attempt's
        # prompt_used; it wins over the shot's original prompt.
        scene_source = (
            existing_attempt.prompt_used
            if existing_attempt is not None and existing_attempt.prompt_used
            else shot.prompt
        )
        characters = [
            (name, _character_visual_spec(bible, name)) for name in names
        ]
        ref_clause_template = _ref_clause(entry)

        def make_prompt(scene_override: str | None) -> PromptSpec:
            spec = compose_keyframe_prompt(
                style_tokens=style_tokens,
                characters=characters,
                shot_visual=scene_source,
                ref_clause_template=ref_clause_template,
                ref_clauses=ref_clauses or None,
                home=home,
            )
            if scene_override is not None:
                spec = spec.with_scene(scene_override)
            return spec

        def make_request(spec: PromptSpec, seed: int, nonce: str) -> MediaRequest:
            return _image_request(
                ctx,
                entry=entry,
                spec=spec,
                negative=spec.negative if "negative" in media_spec(entry).param_map else None,
                input_assets=input_assets,
                prepared=prepared,
                aspect=aspect,
                seed=seed,
                nonce=nonce,
                purpose="keyframe",
            )

        async def qc_run(path: Path) -> QcOutcome:
            checks = await qc_image(path, aspect=aspect, settings=settings)
            return checks, None, {}

        attempt = _ensure_attempt(ctx, shot=shot, kind="keyframe", prompt=shot.prompt)
        persist = _attempt_persist(ctx, attempt.id)
        resume_state = _resume_state(attempt)
        base_nonce = _base_nonce(attempt.nonce) if resume_state is not None else attempt.nonce
        try:
            outcome = await _generate_with_retries(
                ctx,
                episode_id=episode.id,
                subject_key=shot.id,
                purpose="keyframe",
                kind="image",
                model_key=model_key,
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce=base_nonce,
                max_paid_attempts=int(settings.media.max_paid_attempts_per_shot),
                auto_retries=int(settings.media.qc.image.auto_retries),
                qc_run=qc_run,
                persist=persist,
                resume_state=resume_state,
                accumulate_cost=int(attempt.cost_musd),
            )
        except (FailedPolicy, VisualQcError) as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
            raise
        except MediaCircuitOpen as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
            ctx.repos.audit.append(
                actor="system",
                entity=f"run:{ctx.run_id}",
                event="media_circuit_open",
                payload={
                    "stage": self._STAGE,
                    "episode_id": episode.id,
                    "subject_id": subject.id,
                    "error": str(exc),
                },
            )
            raise

        warnings: list[str] = []
        if refs_truncated:
            warnings.append("refs_truncated")
        warnings.extend(f"qc_warn:{c.name}" for c in outcome.checks if c.status == "warn")
        if outcome.sanitize_level:
            warnings.append(f"sanitized:{outcome.sanitize_level}")

        # ---- clip cost projection from S6's locked durations (episode-wide)
        projected_first_pass = _project_clip_cost(ctx, episode.id)
        projected_with_retries = _project_clip_cost(
            ctx, episode.id, with_retries=True
        )
        drift = _estimate_drift(ctx, projected_musd=projected_first_pass or 0)

        return StageOutput(
            output={
                "shot_id": shot.id,
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "kind": "keyframe",
                "asset_sha256": outcome.asset.sha256,
                "refs": [ref.sha256 for ref in ordered_refs],
                "refs_truncated": refs_truncated,
                "warnings": warnings,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc_status": _qc_status(outcome.checks),
                "qc": [c.as_dict() for c in outcome.checks],
                "model_key": model_key,
                "projected_clip_cost_musd_first_pass": projected_first_pass,
                "projected_clip_cost_musd_with_retries": projected_with_retries,
                **({"estimate_drift": drift} if drift else {}),
            }
        )


def _project_clip_cost(
    ctx: RunContext, episode_id: str, *, with_retries: bool = False
) -> int | None:
    """Σ over all shots of the episode: locked clip_duration_ms x clips price.

    Returns None when any locked duration is missing or the pricing unit is
    not representable — never a guessed number.
    """
    from decimal import Decimal

    from studio.config.registry import PerSecond, PerVideo

    episode = ctx.repos.content.get_episode(episode_id)
    if episode is None:
        return None
    shots = ctx.repos.content.list_shots(episode_id)
    try:
        _key, entry = _model_entry(ctx, "clips")
    except Exception:
        return None
    if isinstance(entry.pricing, PerVideo):
        per = usd_to_musd(entry.pricing.usd)
        total = sum(per for shot in shots if shot.clip_duration_ms is not None)
    elif isinstance(entry.pricing, PerSecond):
        total = 0
        seconds_total = Decimal(0)
        for shot in shots:
            if shot.clip_duration_ms is None:
                return None
            seconds_total += Decimal(shot.clip_duration_ms) / Decimal(1000)
        total = usd_to_musd(seconds_total * entry.pricing.usd)
    else:
        return None
    if with_retries:
        rate = float(ctx.config.settings.media.assumed_retry_rate.get("video", 0.30))
        import math

        total = math.ceil(total * (1.0 + rate))
    return int(total)


# ==================================================================
# S10 — clips (SHOT scope; paid; locked duration; conform is free)
# ==================================================================


class ClipGenHandler:
    _STAGE = "s10_clips"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _shot_subjects(ctx)

    def _attempt(self, ctx: RunContext, shot: ShotRow) -> ShotAttemptRow | None:
        return ctx.repos.content.current_shot_attempt(shot.id, kind="clip")

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        shot = ctx.repos.content.get_shot(subject.id)
        assert shot is not None
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        episode = _episode(ctx)
        assert episode is not None
        attempt = self._attempt(ctx, shot)
        keyframe = ctx.repos.content.current_shot_attempt(shot.id, kind="keyframe")
        bible = _pinned_bible(ctx, episode.id)
        settings = ctx.config.settings
        home = Path(settings.paths.home)
        model_key, _entry = _model_entry(ctx, "clips")
        return _sha(
            {
                "stage": self._STAGE,
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no if attempt else 0,
                "nonce": attempt.nonce if attempt else None,
                "model_key": model_key,
                "aspect": track.aspect,
                "keyframe_sha": keyframe.asset_id if keyframe else None,
                "clip_duration_ms": shot.clip_duration_ms,
                "fps": int(settings.render.fps),
                "style_tokens": _style_tokens(bible),
                "prompt_version_sha": prompt_version_sha(home),
                "sanitizer_model_key": settings.sanitizer.model_key,
                "conform": {
                    "crf": settings.media.clip.conform_crf,
                    "duration_tolerance_ms": settings.media.clip.duration_tolerance_ms,
                    "strip_audio": settings.media.clip.strip_audio,
                },
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        shot = ctx.repos.content.get_shot(subject.id)
        if shot is None:
            raise PermanentProviderError(f"shot not found: {subject.id}")
        track = ctx.repos.content.get_track(shot.track_id)
        if track is None:
            raise PermanentProviderError(f"track not found: {shot.track_id}")
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        home = Path(settings.paths.home)
        aspect = Aspect(track.aspect)
        fps = int(settings.render.fps)
        locked_ms = int(shot.clip_duration_ms or 0)
        if locked_ms <= 0:
            raise PreflightError(
                f"shot {shot.shot_no} ({shot.id}) has no locked duration; run S6 first"
            )
        clip_frames = clip_frames_for(locked_ms, fps)
        model_key, entry = _model_entry(ctx, "clips")
        duration_s = locked_ms / 1000.0

        # ---- the accepted keyframe (G2) conditions the clip
        keyframe_attempt = ctx.repos.content.current_shot_attempt(shot.id, kind="keyframe")
        if keyframe_attempt is None or not keyframe_attempt.asset_id:
            raise PreflightError(
                f"shot {shot.shot_no} ({shot.id}) has no keyframe attempt; run the "
                f"keyframe stage first"
            )
        keyframe_asset_row = ctx.repos.assets.get(keyframe_attempt.asset_id)
        if keyframe_asset_row is None:
            raise PermanentProviderError(
                f"keyframe asset {keyframe_attempt.asset_id} is missing from the CAS index"
            )
        keyframe_ref = AssetRef(
            sha256=keyframe_asset_row.sha256,
            rel_path=keyframe_asset_row.rel_path,
            kind=keyframe_asset_row.kind,
            mime=keyframe_asset_row.mime,
            size_bytes=keyframe_asset_row.size_bytes,
            duration_ms=keyframe_asset_row.duration_ms,
            width=keyframe_asset_row.width,
            height=keyframe_asset_row.height,
        )

        cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
        prepared = await prepare_refs(cas, [keyframe_ref], settings)
        keyframe_prepared = prepared[0]

        # ---- worst-case budget for this shot before any spend
        worst = _estimate_clip_cost(entry, clip_duration_ms=locked_ms)
        if worst is not None:
            worst = int(
                worst * (1 + float(settings.media.assumed_retry_rate.get("video", 0.30)))
            ) + 1
            _worst_case_pause(
                ctx,
                episode,
                worst_case_musd=worst,
                label=f"clip for shot {shot.shot_no}",
            )

        plan = _shot_plan(ctx, track.id)
        camera = str(plan.get(shot.shot_no, {}).get("camera", "") or "")
        motion_source = keyframe_attempt.prompt_used or shot.prompt
        motion = compose_motion_prompt(
            shot_visual=motion_source,
            camera=camera,
            home=home,
        )
        def make_prompt(scene_override: str | None) -> PromptSpec:
            if scene_override is not None:
                base = compose_motion_prompt_spec(motion=motion, home=home)
                return base.with_scene(scene_override)
            return compose_motion_prompt_spec(motion=motion, home=home)

        def make_request(spec: PromptSpec, seed: int, nonce: str) -> MediaRequest:
            return _video_request(
                ctx,
                entry=entry,
                prompt=spec.full_prompt,
                keyframe=keyframe_ref,
                keyframe_prepared=keyframe_prepared,
                aspect=aspect,
                duration_s=duration_s,
                seed=seed,
                prompt_sha=spec.prompt_version_sha,
            )

        async def qc_run(path: Path) -> QcOutcome:
            # Keep the raw clip (retention keep_n_days) so re-mixing never repays.
            ext = path.suffix.lstrip(".") or "mp4"
            purge_after = format_ts(
                ctx.clock.now()
                + timedelta(days=int(settings.retention.keep_raw_clips_days))
            )
            raw_ref: AssetRef | None = None
            try:
                raw_ref = cas.put_file(
                    path,
                    kind="video",
                    ext=ext,
                    retention="keep_n_days",
                    purge_after=purge_after,
                    move=False,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("could not upgrade raw clip retention: %s", exc)
            if raw_ref is not None:
                raw_sha = raw_ref.sha256
            else:  # pragma: no cover - defensive
                raw_sha = await asyncio.to_thread(
                    lambda: hashlib.sha256(path.read_bytes()).hexdigest()
                )
            work_dir = Path(settings.paths.projects) / str(episode.id) / "work"
            try:
                conform = await conform_clip(
                    path,
                    clip_duration_ms=locked_ms,
                    settings=settings,
                    work_dir=work_dir,
                )
            except VisualQcError as exc:
                return (
                    [
                        QcCheck(
                            name="conform",
                            status="fail",
                            retryable=True,
                            value=str(exc),
                            threshold="conform within tolerance",
                        )
                    ],
                    None,
                    {"conform_error": str(exc), "raw_sha256": raw_sha},
                )
            if conform.already_compliant:
                if raw_ref is not None:
                    conformed_asset = raw_ref
                else:
                    row = ctx.repos.assets.get(raw_sha)
                    if row is None:
                        raise PermanentProviderError(
                            f"raw clip {raw_sha} is not in the CAS asset index"
                        )
                    conformed_asset = AssetRef(
                        sha256=row.sha256,
                        rel_path=row.rel_path,
                        kind=row.kind,
                        mime=row.mime,
                        size_bytes=row.size_bytes,
                        duration_ms=row.duration_ms,
                        width=row.width,
                        height=row.height,
                    )
            else:
                conformed_asset = cas.put_file(
                    conform.path,
                    kind="video",
                    ext="mp4",
                    retention="intermediate",
                    move=True,
                )
                conform.path.unlink(missing_ok=True)
            conformed_path = cas.resolve(conformed_asset.sha256)
            keyframe_path = cas.resolve(keyframe_ref.sha256)
            checks = await qc_video(
                conformed_path,
                aspect=aspect,
                settings=settings,
                keyframe_path=keyframe_path if keyframe_path.exists() else None,
                expected_frames=clip_frames,
                expected_fps=fps,
            )
            report = {
                **conform.report,
                "conformed_sha256": conformed_asset.sha256,
                "already_compliant": conform.already_compliant,
            }
            return checks, conformed_asset, report

        attempt = _ensure_attempt(ctx, shot=shot, kind="clip", prompt=shot.prompt)
        persist = _attempt_persist(ctx, attempt.id)
        resume_state = _resume_state(attempt)
        base_nonce = (
            _base_nonce(attempt.nonce) if resume_state is not None else attempt.nonce
        )
        try:
            outcome = await _generate_with_retries(
                ctx,
                episode_id=episode.id,
                subject_key=shot.id,
                purpose="clip",
                kind="video",
                model_key=model_key,
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce=base_nonce,
                max_paid_attempts=int(settings.media.max_paid_attempts_per_shot),
                auto_retries=int(settings.media.qc.video.auto_retries),
                qc_run=qc_run,
                persist=persist,
                resume_state=resume_state,
                accumulate_cost=int(attempt.cost_musd),
            )
        except (FailedPolicy, VisualQcError, MediaCircuitOpen, MediaJobFailedError) as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
            if isinstance(exc, MediaCircuitOpen):
                ctx.repos.audit.append(
                    actor="system",
                    entity=f"run:{ctx.run_id}",
                    event="media_circuit_open",
                    payload={
                        "stage": self._STAGE,
                        "episode_id": episode.id,
                        "subject_id": subject.id,
                        "error": str(exc),
                    },
                )
            raise

        warnings: list[str] = []
        warnings.extend(f"qc_warn:{c.name}" for c in outcome.checks if c.status == "warn")
        if outcome.sanitize_level:
            warnings.append(f"sanitized:{outcome.sanitize_level}")

        # clip_duration_ms / clip_frames are NEVER regenerated by this stage.
        report = dict(outcome.report)
        return StageOutput(
            output={
                "shot_id": shot.id,
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "kind": "clip",
                "asset_sha256": outcome.asset.sha256,
                "conformed_asset_id": (
                    outcome.conformed_asset.sha256 if outcome.conformed_asset else None
                ),
                "clip_duration_ms": locked_ms,
                "clip_frames": clip_frames,
                "fps": fps,
                "conform": report,
                "warnings": warnings,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc_status": _qc_status(outcome.checks),
                "qc": [c.as_dict() for c in outcome.checks],
                "model_key": model_key,
            }
        )
