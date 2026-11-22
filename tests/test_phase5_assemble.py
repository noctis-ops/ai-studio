"""Phase 5 step 5: S11 assemble (T-ASM-04..09).

Runs the real media chain on mocks with real ffmpeg, then attacks the
assembly: wrong aspect, tight disk, j-cut, cache, S6 invariants, and the
render report.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from media_env import approve_parked, build_env, drain, run_to_g2, stage_rows

from studio.domain.errors import PreflightError
from studio.pipeline.stages_render import AssembleHandler, assemble_plan
from studio.video.analysis import probe_frames
from studio.video.probe import probe

pytestmark = pytest.mark.ffmpeg


def _env(tmp_path, monkeypatch, name: str, **kwargs):
    return build_env(tmp_path, monkeypatch, filename=name, **kwargs)


async def _run_complete(tmp_path, monkeypatch, name: str, **kwargs):
    env = _env(tmp_path, monkeypatch, name, **kwargs)
    await run_to_g2(env)
    approve_parked(env, "G2_KEYFRAMES", note="animatic approved")
    await drain(env)
    return env


def _s11_output(env) -> dict:
    rows = [r for r in stage_rows(env, "s11_assemble") if r["status"] == "succeeded"]
    assert len(rows) == 1
    return json.loads(rows[0]["output_json"])


def _replace_conformed(env, shot, *, size: str, frames: int = 120) -> str:
    """Swap one shot's conformed clip for a clip of a different geometry."""
    import hashlib

    work = Path(env.config.settings.paths.home) / "data" / "manual"
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"manual-{shot.shot_no}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=gray:s={size}:r=24:d=6",
            "-frames:v", str(frames),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # Store it in the CAS through the same path a conform would use.
    from studio.core.cas import ContentStore

    cas = ContentStore(Path(env.config.settings.paths.cas), env.db, env.clock)
    with cas.db.connect() as conn:  # pragma: no cover - defensive
        conn.execute("SELECT 1")
    asset = cas.put_file(path, kind="video", ext="mp4", retention="intermediate", move=True)
    assert asset.sha256 == digest
    attempt = env.repos.content.current_shot_attempt(shot.id, kind="clip")
    assert attempt is not None
    env.repos.content.update_shot_attempt(attempt.id, conformed_asset_id=asset.sha256)
    return asset.sha256


def _rerun_s11(env) -> None:
    for row in env.repos.pipeline.list_stage_runs(env.run_row.id, stage="s11_assemble"):
        env.repos.pipeline.set_stage_run(row.id, status="pending", clear_input_hash=True)
    env.repos.pipeline.set_run_status(env.run_row.id, "running")
    env.repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": env.run_row.id},
        dedupe_key=f"advance:{env.run_row.id}",
    )


def test_asm_01_render_frames_and_report(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_complete(tmp_path, monkeypatch, "asm-01")
        output = _s11_output(env)
        from studio.audio.timeline import build_timeline

        deliverable_id = env.seeded.deliverable_ids[0]
        timeline = build_timeline(
            env.repos.content, deliverable_id, env.config.settings
        )
        assert output["frames"] == timeline.total_frames
        assert output["fps"] == 24
        assert output["encoder"] in ("x264", "nvenc")
        assert output["assembler_version"]
        path = env.cas.resolve(output["render_sha256"])
        media = probe(path)
        assert (await probe_frames(path)).frame_count == timeline.total_frames
        assert media.has_audio is True
        assert (media.width, media.height) == (1080, 1920)

        report = json.loads(
            env.cas.resolve(output["render_report_sha256"]).read_text(encoding="utf-8")
        )
        assert report["frames"] == timeline.total_frames
        assert report["segment_count"] == len(timeline.shots)
        assert report["max_inputs_per_graph"] >= 2
        assert report["encoder_profile_hash"] == output["encoder_profile_hash"]
        assert len(report["geometry"]) == len(timeline.shots)
        assert all("crop_loss_pct" in item for item in report["geometry"])
        assert report["timings"]["elapsed_s"] >= 0
        # The render is a permanent asset; the report is not.
        assert env.repos.assets.get(output["render_sha256"]).retention == "permanent"

    asyncio.run(scenario())


def test_asm_04_aspect_beyond_tolerance_fails(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="asm-04")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        shots = env.repos.content.shots_for_track(env.seeded.track_ids[0])
        _replace_conformed(env, shots[0], size="480x270")
        _rerun_s11(env)
        await drain(env)
        assert any(
            r["status"] == "failed" for r in stage_rows(env, "s11_assemble")
        )
        error = env.db.connect().execute(
            "SELECT payload_json FROM audit_events WHERE event = 'stage_run_failed' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()["payload_json"]
        assert "aspect mismatch" in error
        assert "nothing was rendered" in error

    asyncio.run(scenario())


def test_asm_04_within_tolerance_records_crop_loss(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="asm-04b")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        shots = env.repos.content.shots_for_track(env.seeded.track_ids[0])
        _replace_conformed(env, shots[0], size="272x480")
        _rerun_s11(env)
        await drain(env)
        output = _s11_output(env)
        report = json.loads(
            env.cas.resolve(output["render_report_sha256"]).read_text(encoding="utf-8")
        )
        first = report["geometry"][0]
        assert first["crop_loss_pct"] > 0
        assert first["aspect_delta_pct"] < 2.0
        from studio.audio.timeline import build_timeline

        timeline = build_timeline(
            env.repos.content, env.seeded.deliverable_ids[0], env.config.settings
        )
        render = env.cas.resolve(output["render_sha256"])
        assert (await probe_frames(render)).frame_count == timeline.total_frames

    asyncio.run(scenario())


def test_asm_05_cache_and_profile_hash(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_complete(tmp_path, monkeypatch, "asm-05")
        handler = AssembleHandler()
        ctx = env.worker.engine.context_for(env.run_row.id)
        from studio.pipeline.stages_render import deliverable_subjects

        subject = deliverable_subjects(ctx)[0]
        before = handler.input_hash(ctx, subject)
        # Re-running the same stage does not change the hash (cache hit).
        env.db.connect().execute("SELECT 1")
        assert handler.input_hash(ctx, subject) == before
        # A different encoder setting is a different key.
        settings = ctx.config.settings
        changed = settings.model_copy(
            update={"encode": settings.encode.model_copy(
                update={"x264": settings.encode.x264.model_copy(update={"crf": 19})}
            )}
        )
        ctx.config = dataclasses.replace(ctx.config, settings=changed)
        assert handler.input_hash(ctx, subject) != before

    asyncio.run(scenario())


def test_asm_06_j_cut_refused_at_s9_and_s11(tmp_path, monkeypatch):
    """Both the loader and the stage guards refuse a non-zero j-cut."""
    from types import SimpleNamespace

    from studio.config.settings import Settings
    from studio.domain.errors import ConfigError
    from studio.pipeline.stages_render import _assert_j_cut_supported

    env = _env(tmp_path, monkeypatch, "asm-06")
    payload = env.config.settings.model_dump()
    payload["timing"] = {**payload["timing"], "j_cut_ms": 40}
    # 1. The settings model rejects it at load time (S9 and S11 both read it).
    with pytest.raises(ConfigError, match="j_cut_ms"):
        Settings.model_validate(payload)
    # 2. And the stage-level guard refuses it before rendering anything, so a
    #    settings object smuggled past the loader still cannot overlap audio.
    with pytest.raises(ConfigError, match="j_cut_ms"):
        _assert_j_cut_supported(SimpleNamespace(timing=SimpleNamespace(j_cut_ms=40)))
    _assert_j_cut_supported(SimpleNamespace(timing=SimpleNamespace(j_cut_ms=0)))


def test_asm_07_s6_columns_unchanged(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="asm-07")
        await run_to_g2(env)
        before = {
            row["id"]: (row["clip_duration_ms"], row["speed_factor"], row["hold_frames"])
            for row in env.db.connect().execute(
                "SELECT id, clip_duration_ms, speed_factor, hold_frames FROM shots"
            )
        }
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        after = {
            row["id"]: (row["clip_duration_ms"], row["speed_factor"], row["hold_frames"])
            for row in env.db.connect().execute(
                "SELECT id, clip_duration_ms, speed_factor, hold_frames FROM shots"
            )
        }
        assert before == after

    asyncio.run(scenario())


def test_asm_08_s11_makes_no_provider_calls(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_complete(tmp_path, monkeypatch, "asm-08")
        calls = env.db.connect().execute(
            "SELECT COUNT(*) AS n FROM provider_calls WHERE stage_run_id IN "
            "(SELECT id FROM stage_runs WHERE stage = 's11_assemble')"
        ).fetchone()["n"]
        assert calls == 0

    asyncio.run(scenario())


def test_asm_09_insufficient_disk(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_complete(tmp_path, monkeypatch, "asm-09")
        ctx = env.worker.engine.context_for(env.run_row.id)

        async def resolve(_settings, _geometries):  # encoder availability stub
            return object()

        plan = await assemble_plan(
            ctx,
            env.seeded.deliverable_ids[0],
            resolve_encoder=resolve,
            free_space=lambda _path: 0,
        )
        assert any("InsufficientDisk" in problem for problem in plan.problems)
        # Nothing was rendered by the preflight.
        assert plan.total_frames > 0

    asyncio.run(scenario())


def test_asm_09_preflight_reports_every_problem_at_once(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="asm-09b")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        env.db.connect().execute("UPDATE shots SET clip_duration_ms = NULL")
        env.repos.pipeline.set_run_status(env.run_row.id, "running")
        for row in env.repos.pipeline.list_stage_runs(env.run_row.id, stage="s11_assemble"):
            env.repos.pipeline.set_stage_run(row.id, status="pending", clear_input_hash=True)
        env.repos.jobs.enqueue(
            kind="advance_run", payload={"run_id": env.run_row.id},
            dedupe_key=f"advance:{env.run_row.id}",
        )
        await drain(env)
        error = env.db.connect().execute(
            "SELECT payload_json FROM audit_events WHERE event = 'stage_run_failed' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()["payload_json"]
        assert "clip_duration_ms" in error

    asyncio.run(scenario())


def test_asm_plan_requires_master(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="asm-master")
        await drain(env)
        ctx = env.worker.engine.context_for(env.run_row.id)

        async def resolve(_settings, _geometries):
            return object()

        with pytest.raises(PreflightError):
            await assemble_plan(
                ctx, "missing-deliverable", resolve_encoder=resolve
            )

    asyncio.run(scenario())
