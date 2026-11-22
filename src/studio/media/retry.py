"""Shared media attempt/retry policy (paid-call crash safety).

Split from ``studio.media.stages`` in Phase 5 (H5.8) as a pure move.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from studio.core.cas import ContentStore
from studio.core.clock import format_ts
from studio.core.gateway import CallContext
from studio.domain.errors import (
    ContentPolicyError,
    FailedPolicy,
    MediaAttemptExhausted,
    MediaCircuitOpen,
    VisualQcError,
)
from studio.domain.refs import AssetRef
from studio.media.common import _json
from studio.media.prompts import (
    PromptSpec,
    seed_for,
)
from studio.media.sanitizer import sanitize_scene_text
from studio.providers.base import MediaRequest
from studio.qc.visual import QcCheck

if TYPE_CHECKING:
    from studio.domain.models import ShotAttemptRow, ShotRow
    from studio.pipeline.context import RunContext



_NONCE_RETRY_MARKER = ".q"


def _base_nonce(nonce: str) -> str:
    head, sep, tail = nonce.rpartition(_NONCE_RETRY_MARKER)
    if sep and head and tail.isdigit():
        return head
    return nonce


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
