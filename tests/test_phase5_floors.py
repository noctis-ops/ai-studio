"""Phase 5 floors: error paths that keep `panel.py` and `stages_render.py` above
their 88 % per-module floors.

These are the "operator asks for something that is not there" branches: an
unknown gate id, a gate whose run lost its episode, an assembly preflight with
several independent problems, and a render QC asked for a deliverable S11 never
finished. All are free and fail closed.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from media_env import approve_parked, build_env, drain, run_to_g2, stage_rows

from studio.domain.enums import Scope
from studio.domain.errors import PreflightError, StudioError
from studio.domain.refs import SubjectRef
from studio.pipeline.panel import (
    _estimates,
    _spend_by,
    _timeline_summary,
    gate_panel,
    keyframe_reject_cost,
)
from studio.pipeline.qc_render import probe_streams  # noqa: F401  (import path check)
from studio.pipeline.stages_render import (
    AssembleHandler,
    RenderQcHandler,
    assemble_plan,
)


def test_panel_unknown_gate_and_empty_episode(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="floor-panel")
        ctx = env.worker.engine.context_for(env.run_row.id)
        with pytest.raises(StudioError, match="unknown gate"):
            gate_panel(ctx, "no-such-gate")
        # No episode → no spend series, no estimates, no timeline rows.
        assert _spend_by(env.db, None)["total_musd"] == 0
        assert _estimates(ctx, "no-episode") == ([], False)
        assert _timeline_summary(ctx, "no-episode") == []
        # A shot that does not exist has no reject cost either.
        cost = keyframe_reject_cost(ctx, ["no-such-shot"])
        assert cost["available"] is False or cost["with_retries_musd"] == 0

    asyncio.run(scenario())


def test_assemble_plan_collects_independent_problems(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="floor-asm")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        ctx = env.worker.engine.context_for(env.run_row.id)
        shots = env.repos.content.shots_for_track(env.seeded.track_ids[0])
        # One shot loses its conformed clip; another loses its duration.
        attempt = env.repos.content.current_shot_attempt(shots[0].id, kind="clip")
        assert attempt is not None
        env.repos.content.update_shot_attempt(attempt.id, conformed_asset_id=None)
        env.db.connect().execute(
            "UPDATE shots SET clip_duration_ms = NULL WHERE id = ?", (shots[1].id,)
        )

        async def resolve(_settings, _geometries):
            return object()

        with pytest.raises(PreflightError, match="clip_duration_ms"):
            await assemble_plan(
                ctx,
                env.seeded.deliverable_ids[0],
                resolve_encoder=resolve,
                free_space=lambda _path: 10**12,
            )

        # With the timeline rebuilt, the missing conformed clip is collected.
        env.db.connect().execute(
            "UPDATE shots SET clip_duration_ms = 5000 WHERE id = ?", (shots[1].id,)
        )
        plan = await assemble_plan(
            ctx,
            env.seeded.deliverable_ids[0],
            resolve_encoder=resolve,
            free_space=lambda _path: 10**12,
        )
        assert any("no accepted conformed clip" in problem for problem in plan.problems)
        assert len(plan.problems) >= 1

    asyncio.run(scenario())


def test_render_qc_without_an_assembly_is_a_preflight_error(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="floor-qc")
        await drain(env)  # parks at G1: no S11 output exists yet
        ctx = env.worker.engine.context_for(env.run_row.id)
        handler = RenderQcHandler()
        subject = SubjectRef(scope=Scope.DELIVERABLE, id=env.seeded.deliverable_ids[0])
        # The input hash degrades to an error key instead of raising...
        assert handler.input_hash(ctx, subject)
        # ...and run() refuses with a preflight error that names the stage.
        with pytest.raises(PreflightError, match="s11_assemble"):
            await handler.run(ctx, subject)

    asyncio.run(scenario())


def test_render_qc_missing_render_asset_is_a_preflight_error(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="floor-qc2")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        rows = [r for r in stage_rows(env, "s11_assemble") if r["status"] == "succeeded"]
        assert rows
        run_row = env.repos.pipeline.list_stage_runs(env.run_row.id, stage="s11_assemble")
        target = next(row for row in run_row if row.output_json)
        payload = json.loads(target.output_json or "{}")
        payload["render_sha256"] = "f" * 64
        env.repos.pipeline.set_stage_run(
            target.id, status="succeeded", output_json=json.dumps(payload)
        )
        ctx = env.worker.engine.context_for(env.run_row.id)
        handler = RenderQcHandler()
        subject = SubjectRef(scope=Scope.DELIVERABLE, id=payload["deliverable_id"])
        with pytest.raises(PreflightError, match="missing from the CAS"):
            await handler.run(ctx, subject)

    asyncio.run(scenario())


def test_assemble_plan_free_space_is_injectable(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="floor-asm2")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        ctx = env.worker.engine.context_for(env.run_row.id)

        async def resolve(_settings, _geometries):
            return object()

        plan = await assemble_plan(
            ctx,
            env.seeded.deliverable_ids[0],
            resolve_encoder=resolve,
            free_space=lambda _path: 0,
        )
        assert any("InsufficientDisk" in problem for problem in plan.problems)
        # The plan still knows what it *would* have built.
        assert plan.total_frames > 0 and plan.shots
        assert AssembleHandler().subjects(ctx)

    asyncio.run(scenario())
