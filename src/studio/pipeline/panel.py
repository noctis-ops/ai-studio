"""Gate panel for a presentation-time review (§G4), never stage output.

Every number here is *mutable*: spend, remaining caps, drift against the S4
baseline. That is exactly why the panel is computed when a human looks at it
and frozen into ``approval_gates.presented_json`` at gate creation, and never
enters S9's stage output or the gate snapshot hash.

Verdicts:
``ok`` — the remaining episode/month caps cover the clip projection.
``over_remaining_cap`` — S10p would halt before spending.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from studio.audio.timeline import build_timeline
from studio.domain.enums import BudgetScope, Language
from studio.domain.errors import StudioError
from studio.pipeline.estimate_media import media_estimate
from studio.pipeline.stages_render import deliverable_subjects

if TYPE_CHECKING:
    from studio.db.connection import Database
    from studio.pipeline.context import RunContext

_BUCKETS = ("tts", "sheets", "keyframes", "video")


def _spend_by(db: Database, episode_id: str | None) -> dict[str, Any]:
    """Actual spend to date, by capability and by (capability, model)."""
    if episode_id is None:
        return {"by_capability": {}, "by_model": [], "total_musd": 0}
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT capability, model_key, COUNT(*) AS calls,
               COALESCE(SUM(actual_cost_musd), 0) AS spent_musd,
               COALESCE(SUM(est_cost_musd), 0) AS est_musd
        FROM provider_calls
        WHERE episode_id = ?
        GROUP BY capability, model_key
        ORDER BY capability, model_key
        """,
        (episode_id,),
    ).fetchall()
    by_capability: dict[str, int] = {}
    by_model: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        spent = int(row["spent_musd"])
        total += spent
        by_capability[str(row["capability"])] = (
            by_capability.get(str(row["capability"]), 0) + spent
        )
        by_model.append(
            {
                "capability": row["capability"],
                "model_key": row["model_key"],
                "calls": int(row["calls"]),
                "spent_musd": spent,
                "est_musd": int(row["est_musd"]),
            }
        )
    return {"by_capability": by_capability, "by_model": by_model, "total_musd": total}


def _caps(ctx: RunContext, episode_id: str) -> dict[str, Any]:
    from studio.domain.money import usd_to_musd

    settings = ctx.config.settings
    month_ref = ctx.clock.month_tag()
    out: dict[str, Any] = {}
    for scope, ref, default_usd in (
        (BudgetScope.EPISODE, episode_id, float(settings.budgets.default_episode_cap_usd)),
        (BudgetScope.MONTH, month_ref, float(settings.budgets.monthly_cap_usd)),
    ):
        row = ctx.repos.budgets.get(scope.value, ref)
        cap = row.cap_musd if row is not None else usd_to_musd(str(default_usd))
        held = ctx.repos.budgets.held_musd(scope.value, ref)
        out[scope.value] = {
            "scope_ref": ref,
            "cap_musd": cap,
            "held_musd": held,
            "remaining_musd": None if cap is None else max(cap - held, 0),
        }
    return out


def _clip_projection(ctx: RunContext, episode_id: str) -> dict[str, Any]:
    from studio.media.keyframes import _project_clip_cost

    first_pass = _project_clip_cost(ctx, episode_id)
    with_retries = _project_clip_cost(ctx, episode_id, with_retries=True)
    return {
        "first_pass_musd": first_pass,
        "with_retries_musd": with_retries,
        "available": first_pass is not None,
    }


def _estimates(ctx: RunContext, episode_id: str) -> tuple[list[dict[str, Any]], bool]:
    episode = ctx.repos.content.get_episode(episode_id)
    if episode is None:
        return [], False
    per_deliverable: list[dict[str, Any]] = []
    available = True
    for deliverable in ctx.repos.content.list_deliverables(episode.id):
        if Language(deliverable.language) not in ctx.languages:
            continue
        estimate = media_estimate(ctx, deliverable.id)
        if not estimate.available:
            available = False
        per_deliverable.append({"deliverable_id": deliverable.id, **estimate.as_dict()})
    return per_deliverable, available


def _drift(
    per_deliverable: list[dict[str, Any]], projection: dict[str, Any]
) -> list[dict[str, Any]]:
    """Per-bucket drift against the S4 baseline, with a warn flag (§G4)."""
    baseline_video = 0
    for entry in per_deliverable:
        video = entry.get("video") or {}
        if isinstance(video.get("expected_musd"), int):
            baseline_video += int(video["expected_musd"])
    projected = projection.get("first_pass_musd")
    if not isinstance(projected, int) or baseline_video <= 0:
        return []
    pct = (projected - baseline_video) * 100.0 / baseline_video
    warn_pct = 25.0
    return [
        {
            "bucket": "video",
            "baseline_musd": baseline_video,
            "projected_musd": projected,
            "drift_pct": round(pct, 2),
            "warn": pct > warn_pct,
        }
    ]


def _timeline_summary(ctx: RunContext, episode_id: str) -> list[dict[str, Any]]:
    episode = ctx.repos.content.get_episode(episode_id)
    if episode is None:
        return []
    out: list[dict[str, Any]] = []
    for deliverable in ctx.repos.content.list_deliverables(episode.id):
        if Language(deliverable.language) not in ctx.languages:
            continue
        try:
            timeline = build_timeline(ctx.repos.content, deliverable.id, ctx.config.settings)
        except StudioError as exc:
            out.append({"deliverable_id": deliverable.id, "error": str(exc)})
            continue
        out.append(
            {
                "deliverable_id": deliverable.id,
                "shots": len(timeline.shots),
                "total_frames": timeline.total_frames,
                "total_samples": timeline.total_samples,
                "timeline_hash": timeline.timeline_hash,
            }
        )
    return out


def gate_panel(ctx: RunContext, gate_id: str) -> dict[str, Any]:
    """The presentation-time panel for one gate row."""
    gate = ctx.repos.gates.get(gate_id)
    if gate is None:
        raise StudioError(f"unknown gate: {gate_id}")
    run = ctx.repos.pipeline.get_run(gate.run_id)
    episode_id = run.episode_id if run is not None else ctx.episode_id
    if episode_id is None:
        raise StudioError(f"gate {gate_id} has no episode")
    return _panel_payload(
        ctx,
        episode_id=episode_id,
        header={
            "gate_id": gate.id,
            "run_id": gate.run_id,
            "gate": gate.gate,
            "stage": gate.stage,
            "round": gate.round,
            "status": gate.status,
        },
    )


def panel_for_new_gate(
    ctx: RunContext, *, run_id: str, gate: str, stage: str, round_no: int
) -> dict[str, Any]:
    """Panel computed *before* the gate row exists (frozen at creation, §G4)."""
    run = ctx.repos.pipeline.get_run(run_id)
    episode_id = run.episode_id if run is not None else ctx.episode_id
    if episode_id is None:
        raise StudioError(f"run {run_id} has no episode")
    return _panel_payload(
        ctx,
        episode_id=episode_id,
        header={
            "gate_id": None,
            "run_id": run_id,
            "gate": gate,
            "stage": stage,
            "round": round_no,
            "status": "pending",
        },
    )


def _panel_payload(
    ctx: RunContext, *, episode_id: str, header: dict[str, Any]
) -> dict[str, Any]:
    per_deliverable, estimates_available = _estimates(ctx, episode_id)
    projection = _clip_projection(ctx, episode_id)
    caps = _caps(ctx, episode_id)
    spend = _spend_by(ctx.db, episode_id)

    remaining = [
        value["remaining_musd"]
        for value in caps.values()
        if value.get("remaining_musd") is not None
    ]
    projected = projection.get("with_retries_musd") or projection.get("first_pass_musd")
    if not isinstance(projected, int) or not remaining:
        verdict = "unknown"
    elif any(projected > int(left) for left in remaining):
        verdict = "over_remaining_cap"
    else:
        verdict = "ok"

    return {
        **header,
        "episode_id": episode_id,
        "estimates_available": estimates_available,
        "estimates": [
            {
                "deliverable_id": entry["deliverable_id"],
                "buckets": {
                    bucket: {
                        key: entry.get(bucket, {}).get(key)
                        for key in ("low_musd", "expected_musd", "high_musd")
                    }
                    for bucket in _BUCKETS
                },
                "available": entry.get("available"),
            }
            for entry in per_deliverable
        ],
        "spend": spend,
        "clip_projection": projection,
        "caps": caps,
        "drift": _drift(per_deliverable, projection),
        "timeline": _timeline_summary(ctx, episode_id),
        "verdict": verdict,
    }


def keyframe_reject_cost(ctx: RunContext, shot_ids: list[str]) -> dict[str, Any]:
    """What re-running the keyframes of these shots is expected to cost (§G3).

    Shown before `gates reject --shots ... --yes`; the real guard is still the
    per-call gateway reservation and the episode/month cap in the handler.
    """
    from studio.domain.enums import Aspect
    from studio.media.common import _model_entry
    from studio.media.sheets import _estimate_image_cost

    settings = ctx.config.settings
    key, entry = _model_entry(ctx, "keyframes")
    retry_rate = float(settings.media.assumed_retry_rate.get("image", 0.25))
    per_shot: list[dict[str, Any]] = []
    total = 0
    computable = True
    for shot_id in shot_ids:
        shot = ctx.repos.content.get_shot(shot_id)
        if shot is None:
            continue
        track = ctx.repos.content.get_track(shot.track_id)
        if track is None:
            continue
        cost = _estimate_image_cost(entry, aspect=Aspect(track.aspect))
        if cost is None:
            computable = False
            continue
        total += cost
        per_shot.append({"shot_id": shot.id, "shot_no": shot.shot_no, "cost_musd": cost})
    import math

    return {
        "model_key": key,
        "shots": per_shot,
        "available": computable,
        "first_pass_musd": total if computable else None,
        "with_retries_musd": math.ceil(total * (1 + retry_rate)) if computable else None,
    }


def presented_json(ctx: RunContext, gate_id: str) -> str:
    return json.dumps(gate_panel(ctx, gate_id), ensure_ascii=False, sort_keys=True)


def subjects_for_run(ctx: RunContext) -> int:
    """Small helper kept for the CLI's `gates show` header."""
    return len(deliverable_subjects(ctx))
