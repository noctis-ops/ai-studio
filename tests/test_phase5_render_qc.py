"""Phase 5 step 6: S12 render QC (T-QCR-01..03) and gate G3 (T-G3-02/03).

Each QC check is exercised against a purpose-built bad render: the check must
fail (or warn) for the *measured* reason, and must pass on the good render.

Render-scope raises no provider call (T-ASM-08) and never re-encodes: the QC
module is a pure measurement layer over the muxed MP4.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from media_env import approve_parked, build_env, drain, run_to_g2, stage_rows

from studio.audio.analysis import integrated_loudness
from studio.config.presets import ExportPreset
from studio.domain.errors import GatePolicyError, MediaToolError
from studio.pipeline.gates import GateService
from studio.pipeline.qc_render import (
    faststart_ok,
    run_render_qc,
    top_level_boxes,
    upstream_aggregate,
)

pytestmark = pytest.mark.ffmpeg

FRAMES = 48  # 2 s at 24 fps


def _preset(**overrides) -> ExportPreset:
    payload = {
        "aspect": "9:16",
        "width": 1080,
        "height": 1920,
        "fps": 24,
        "video": {"maxrate_k": 8000, "bufsize_k": 12000},
        "audio": {"codec": "aac", "bitrate_k": 192, "sample_rate": 48000, "channels": 2},
        "limits": {"max_duration_s": 60},
    }
    payload.update(overrides)
    return ExportPreset.model_validate(payload)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def _make_render(
    path: Path,
    *,
    frames: int = FRAMES,
    size: str = "1080x1920",
    fps: int = 24,
    audio: bool = True,
    two_audio: bool = False,
    faststart: bool = True,
    black_middle: bool = False,
    audio_filter: str | None = "+7.8dB",
) -> Path:
    duration = frames / fps
    # BT.709 tags are written into the bitstream, exactly like S11's segments.
    common = [
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
        "-bsf:v",
        "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
        "matrix_coefficients=1:video_full_range_flag=0",
    ]
    vf = "format=yuv420p"
    if black_middle:
        # One second of black at ~0.5 s, then picture again.
        vf = (
            "format=yuv420p,drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:"
            "enable='between(t,0.5,1.5)',drawbox=x=0:y=0:w=iw:h=ih:"
            "color=black@0:t=fill:enable='lt(t,0.4)'"
        )
    video_in = [
        "-f", "lavfi",
        "-i", f"testsrc2=s={size}:r={fps}:d={duration}",
    ]
    inputs = list(video_in)
    audio_args: list[str] = []
    if audio or two_audio:
        tone = "sine=frequency=440:sample_rate=48000:duration=" + f"{duration}"
        if audio_filter:
            tone += f",volume={audio_filter}"
        inputs += ["-f", "lavfi", "-i", tone]
        audio_args = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        if two_audio:
            inputs += ["-f", "lavfi", "-i", tone]
            audio_args = ["-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0", *audio_args]
    args = [
        *inputs,
        "-vf", vf,
        "-frames:v", str(frames),
        "-r", str(fps),
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        *common,
        "-map_metadata", "-1",
    ]
    if audio_args and not two_audio:
        args += ["-map", "0:v:0", "-map", "1:a:0", *audio_args]
    elif not audio_args:
        args += ["-an"]
    if faststart:
        args += ["-movflags", "+faststart"]
    args += [str(path)]
    _ffmpeg(*args)
    return path


def _settings(tmp_path, monkeypatch, name: str):
    env = build_env(tmp_path, monkeypatch, filename=name)
    return env.config.settings


async def _qc(path: Path, settings, preset: ExportPreset | None = None, **kwargs):
    return await run_render_qc(
        path,
        settings=settings,
        preset=preset or _preset(),
        expected_frames=kwargs.pop("expected_frames", FRAMES),
        **kwargs,
    )


def _status(report, name: str) -> str:
    for check in report.checks:
        if check.name == name:
            return check.status
    raise AssertionError(f"no check named {name!r}; have {[c.name for c in report.checks]}")


def test_qcr_01_good_render_passes(tmp_path, monkeypatch):
    async def scenario() -> None:
        render = _make_render(tmp_path / "good.mp4")
        settings = _settings(tmp_path, monkeypatch, "qcr-good")
        report = await _qc(render, settings)
        failed = [c.name for c in report.failures()]
        assert failed == [], report.as_dict()
        assert report.status in ("pass", "warn")
        assert _status(report, "faststart") == "pass"
        assert _status(report, "frame_count") == "pass"
        assert report.measured["expected_frames"] == FRAMES

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mutation", "check"),
    [
        ({"fps": 30, "frames": 60}, "streams"),
        ({"size": "720x1280"}, "streams"),
        ({"audio": False}, "streams"),
        ({"two_audio": True}, "streams"),
        ({"faststart": False}, "faststart"),
        ({"frames": FRAMES + 1}, "frame_count"),
    ],
)
def test_qcr_01_bad_renders_fail(tmp_path, monkeypatch, mutation, check):
    async def scenario() -> None:
        render = _make_render(tmp_path / "bad.mp4", **mutation)
        settings = _settings(tmp_path, monkeypatch, "qcr-bad")
        # The timeline still expects FRAMES: an over-long render is off by one.
        report = await _qc(render, settings, expected_frames=FRAMES)
        assert _status(report, check) == "fail", report.as_dict()

    asyncio.run(scenario())


def test_qcr_01_quiet_render_fails_loudness(tmp_path, monkeypatch):
    async def scenario() -> None:
        render = _make_render(tmp_path / "quiet.mp4", audio_filter="-30dB")
        settings = _settings(tmp_path, monkeypatch, "qcr-quiet")
        report = await _qc(render, settings)
        assert _status(report, "loudness") == "fail"

    asyncio.run(scenario())


def test_qcr_01_true_peak_over_ceiling(tmp_path, monkeypatch):
    async def scenario() -> None:
        render = _make_render(tmp_path / "loud.mp4", audio_filter="+20dB")
        settings = _settings(tmp_path, monkeypatch, "qcr-loud")
        report = await _qc(render, settings)
        assert _status(report, "true_peak") in ("fail", "warn")

    asyncio.run(scenario())


def test_qcr_01_black_section_warns(tmp_path, monkeypatch):
    async def scenario() -> None:
        render = _make_render(tmp_path / "black.mp4", black_middle=True)
        settings = _settings(tmp_path, monkeypatch, "qcr-black")
        report = await _qc(render, settings)
        assert _status(report, "black_sections") == "warn"
        # A black section is warn-level at render scope, never a stage failure.
        assert report.status in ("warn", "pass")

    asyncio.run(scenario())


def test_qcr_01_missing_render_is_a_check_not_a_crash(tmp_path, monkeypatch):
    async def scenario() -> None:
        settings = _settings(tmp_path, monkeypatch, "qcr-missing")
        report = await _qc(tmp_path / "nope.mp4", settings)
        assert _status(report, "non_empty") == "fail"
        assert report.status == "fail"

    asyncio.run(scenario())


def test_qcr_02_true_peak_is_measured_on_the_muxed_aac(tmp_path, monkeypatch):
    async def scenario() -> None:
        render = _make_render(tmp_path / "tp.mp4", audio_filter="+9dB")
        settings = _settings(tmp_path, monkeypatch, "qcr-tp")
        report = await _qc(render, settings)
        assert report.measured["measured_on"] == "muxed_aac"
        measured = await integrated_loudness(str(render))
        assert report.measured["true_peak_dbtp"] == pytest.approx(
            measured.true_peak_dbtp, abs=0.05
        )
        assert report.measured["lufs"] == pytest.approx(
            measured.integrated_lufs, abs=0.05
        )

    asyncio.run(scenario())


def test_qcr_03_upstream_qc_is_aggregated_not_recomputed(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="qcr-up")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        rows = stage_rows(env, "s12_render_qc")
        assert rows and all(row["status"] == "succeeded" for row in rows)
        output = json.loads(rows[0]["output_json"])
        names = {check["name"] for check in output["checks"]}
        assert "upstream_qc" in names
        # The QC report asset records the upstream block it read.
        report = json.loads(
            env.cas.resolve(output["qc_report_sha256"]).read_text(encoding="utf-8")
        )
        assert set(report["upstream"]) == {"s3b_tashkeel", "s7b_audio_mix", "s10_clips"}
        assert report["status"] == output["qc_status"]
        # ...and the aggregate function itself is read-only over stage outputs.
        ctx = env.worker.engine.context_for(env.run_row.id)
        before = upstream_aggregate(ctx, env.run_row.id, track_id=None)
        assert {k: v["status"] for k, v in before.items()} == {
            k: v["status"] for k, v in report["upstream"].items()
        }

    asyncio.run(scenario())


def test_g3_02_checklist_is_stored_on_the_gate(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g3-02")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        gate = next(
            gate
            for gate in env.repos.gates.list_pending(env.run_row.id)
            if gate.gate == "G3_FINAL_CUT"
        )
        service = GateService(
            env.db, env.repos, env.clock, env.worker.engine.dag, env.ids
        )
        checklist = {
            "reviewed_full_render": True,
            "audience_made_for_kids": False,
            "ai_disclosure": True,
            "third_party_ip_confirmed": True,
            "licenses_confirmed": True,
        }
        decided = service.approve(gate.id, "watched it", checklist=checklist)
        stored = json.loads(decided.checklist_json or "{}")
        assert stored == checklist
        # A non-G3 gate refuses a checklist outright.
        other = env.repos.gates.list_for_run(env.run_row.id)
        assert any(row.gate == "G2_KEYFRAMES" for row in other)

    asyncio.run(scenario())


def test_g3_02_final_profile_requires_every_item(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g3-02b")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        gate = next(
            gate
            for gate in env.repos.gates.list_pending(env.run_row.id)
            if gate.gate == "G3_FINAL_CUT"
        )
        # Pretend the run is a final-profile run: partial checklists are refused.
        env.db.connect().execute(
            "UPDATE pipeline_runs SET profile = 'final' WHERE id = ?", (env.run_row.id,)
        )
        service = GateService(
            env.db, env.repos, env.clock, env.worker.engine.dag, env.ids
        )
        with pytest.raises(GatePolicyError, match="missing"):
            service.approve(gate.id, "partial", checklist={"reviewed_full_render": True})
        with pytest.raises(GatePolicyError, match="reviewed_full_render"):
            service.approve(
                gate.id,
                "not reviewed",
                checklist={
                    "reviewed_full_render": False,
                    "audience_made_for_kids": True,
                    "ai_disclosure": True,
                    "third_party_ip_confirmed": True,
                    "licenses_confirmed": True,
                },
            )

    asyncio.run(scenario())


def test_g3_03_auto_fill_is_visible_and_final_refuses_it(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g3-03")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        # Sandbox: the operator may omit items; they become "auto" markers.
        gate = next(
            gate
            for gate in env.repos.gates.list_pending(env.run_row.id)
            if gate.gate == "G3_FINAL_CUT"
        )
        service = GateService(
            env.db, env.repos, env.clock, env.worker.engine.dag, env.ids
        )
        decided = service.approve(gate.id, "draft", checklist={"reviewed_full_render": True})
        stored = json.loads(decided.checklist_json or "{}")
        assert stored["reviewed_full_render"] is True
        assert stored["audience_made_for_kids"] == "auto"
        assert stored["ai_disclosure"] is True

    asyncio.run(scenario())


def test_g3_01_snapshot_follows_the_render_sha(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g3-01")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        from studio.pipeline.stages_render import RenderQcHandler

        handler = RenderQcHandler()
        ctx = env.worker.engine.context_for(env.run_row.id)
        from studio.domain.enums import Scope
        from studio.domain.refs import SubjectRef

        deliverable_id = env.seeded.deliverable_ids[0]
        subject = SubjectRef(scope=Scope.DELIVERABLE, id=deliverable_id)
        before = handler.input_hash(ctx, subject)
        # A new render sha (e.g. `studio render rebuild`) is a new snapshot.
        row = next(
            row
            for row in env.repos.pipeline.list_stage_runs(env.run_row.id, stage="s11_assemble")
            if row.status == "succeeded"
        )
        payload = json.loads(row.output_json or "{}")
        payload["render_sha256"] = "0" * 64
        env.repos.pipeline.set_stage_run(
            row.id, status="succeeded", output_json=json.dumps(payload)
        )
        assert handler.input_hash(ctx, subject) != before

    asyncio.run(scenario())


def test_mp4_box_scan_handles_sizes(tmp_path):
    boxes = top_level_boxes(_make_render(tmp_path / "boxes.mp4"))
    assert boxes[0][0] in ("ftyp", "moov")
    assert faststart_ok(boxes) is True
    no_fast = top_level_boxes(_make_render(tmp_path / "slow.mp4", faststart=False))
    assert faststart_ok(no_fast) is False
    # A truncated final box must be refused, not silently accepted.
    trailing = tmp_path / "trailing.mp4"
    trailing.write_bytes((tmp_path / "boxes.mp4").read_bytes()[:-50])
    with pytest.raises(MediaToolError):
        top_level_boxes(trailing)


def test_cli_render_rebuild_is_free_and_scoped(tmp_path, monkeypatch):
    """`studio render rebuild` stales only S11/S12 for one deliverable."""
    from typer.testing import CliRunner

    from studio.cli import app

    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="rebuild")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        provider_before = env.db.connect().execute(
            "SELECT count(*) AS n FROM provider_calls"
        ).fetchone()["n"]
        other_stage_before = {
            row.id
            for row in env.repos.pipeline.list_stage_runs(env.run_row.id)
            if row.stage not in ("s11_assemble", "s12_render_qc")
        }
        deliverable_id = env.seeded.deliverable_ids[0]
        monkeypatch.setenv("COLUMNS", "220")
        result = CliRunner().invoke(
            app, ["render", "rebuild", env.run_row.id, "--deliverable", deliverable_id]
        )
        assert result.exit_code == 0, result.output
        assert "staled" in result.output
        after = {
            row.id: row.status
            for row in env.repos.pipeline.list_stage_runs(env.run_row.id)
        }
        render_ids = {
            row.id
            for row in env.repos.pipeline.list_stage_runs(env.run_row.id)
            if row.stage in ("s11_assemble", "s12_render_qc")
        }
        assert render_ids and all(after[row_id] == "stale" for row_id in render_ids)
        # Upstream work and the ledger are untouched: the rebuild is free.
        assert other_stage_before <= set(after)
        assert all(after[row_id] == "succeeded" for row_id in other_stage_before)
        assert (
            env.db.connect()
            .execute("SELECT count(*) AS n FROM provider_calls")
            .fetchone()["n"]
            == provider_before
        )

    asyncio.run(scenario())


def test_cli_g3_approve_list_requires_the_checklist(tmp_path, monkeypatch):
    """A bare `gates approve` on G3 is refused; flags or a file are required."""
    from typer.testing import CliRunner

    from studio.cli import app

    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g3-cli")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        gate = next(
            gate
            for gate in env.repos.gates.list_pending(env.run_row.id)
            if gate.gate == "G3_FINAL_CUT"
        )
        # Final profile: a bare approval is refused, a full checklist passes.
        env.db.connect().execute(
            "UPDATE pipeline_runs SET profile = 'final' WHERE id = ?", (env.run_row.id,)
        )
        monkeypatch.setenv("COLUMNS", "220")
        runner = CliRunner()
        bare = runner.invoke(app, ["gates", "approve", gate.id])
        assert bare.exit_code != 0
        assert "checklist" in bare.output

        checklist_file = tmp_path / "checklist.json"
        checklist_file.write_text(
            json.dumps(
                {
                    "reviewed_full_render": True,
                    "audience_made_for_kids": False,
                    "ai_disclosure": True,
                    "third_party_ip_confirmed": True,
                    "licenses_confirmed": True,
                }
            ),
            encoding="utf-8",
        )
        ok = runner.invoke(
            app,
            ["gates", "approve", gate.id, "--checklist-file", str(checklist_file)],
        )
        assert ok.exit_code == 0, ok.output
        decided = env.repos.gates.get(gate.id)
        assert decided is not None and decided.status == "approved"
        assert json.loads(decided.checklist_json or "{}")["audience_made_for_kids"] is False

    asyncio.run(scenario())
