"""T-MOCK-01, T-CLI-01, T-CLI-02 (BEGIN_PROMPT section I)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import make_home
from typer.testing import CliRunner

from studio.cli import app
from studio.domain.enums import Aspect, Language
from studio.providers.base import MediaRequest, TTSRequest
from studio.providers.mock.common import MockFaultPlan
from studio.providers.mock.tts import MockTTSProvider
from studio.providers.mock.video import MockVideoProvider
from studio.video.probe import probe

runner = CliRunner()


# --------------------------------------------------------------- T-MOCK-01


@pytest.mark.ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize("aspect,dims", [(Aspect.TALL, (270, 480)), (Aspect.WIDE, (480, 270))])
async def test_mock_video_duration_and_aspect(tmp_path, aspect, dims):
    ids = _Ids()
    provider = MockVideoProvider(
        ledger_path=tmp_path / "video_jobs.json",
        width_16x9=480,
        height_16x9=270,
        fps=24,
        ids=ids,
        poll_ticks=1,
    )
    req = MediaRequest(
        capability="image_to_video",
        params={"prompt": "mock check"},
        aspect=aspect,
        duration_s=5.0,
        seed=123,
    )
    handle = await provider.submit(req)
    for _ in range(10):
        state = await provider.poll(handle)
        if state.state == "succeeded":
            break
    else:
        pytest.fail("mock video never reached succeeded")
    payloads = await provider.fetch(handle)
    path = payloads[0].local_path
    assert path is not None and path.is_file() and path.stat().st_size > 0

    result = probe(path)
    frame_ms = 1000 // 24
    assert abs((result.duration_ms or 0) - 5000) <= frame_ms
    assert (result.width, result.height) == dims


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_mock_tts_duration_matches_chars_over_rate(tmp_path):
    ids = _Ids()
    workdir = tmp_path / "tts_work"
    provider = MockTTSProvider(
        chars_per_second=15, workdir=workdir, fault_plan=MockFaultPlan(), ids=ids
    )
    text = "a" * 150  # 150 chars / 15 cps = 10s
    req = TTSRequest(text=text, voice_id="mock-voice-ar", language=Language.AR)
    result = await provider.synthesize(req)
    path = result.payload.local_path
    assert path is not None and path.is_file() and path.stat().st_size > 0

    duration_s = probe(path).duration_ms / 1000.0
    assert abs(duration_s - 10.0) < 0.15

    # minimum duration is 0.5s for very short input
    short = await provider.synthesize(TTSRequest(text="hi", voice_id="v", language=Language.AR))
    assert short.payload.local_path is not None
    assert probe(short.payload.local_path).duration_ms / 1000.0 >= 0.5 - 0.05


class _Ids:
    _n = 0

    def new(self) -> str:
        _Ids._n += 1
        return f"mocktest-{_Ids._n:06d}"


# --------------------------------------------------------------- T-CLI-01


def test_studio_init_is_idempotent(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="cliinit")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0, second.output
    assert (home / "data" / "studio.db").is_file()


def test_doctor_fails_when_ffmpeg_is_hidden(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="clidoctor")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0
    assert "ffmpeg" in result.output


def test_doctor_succeeds_with_ffmpeg_present(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="clidoctor2")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------- T-CLI-02


def _pending_gate_id(home: Path) -> str:
    conn = sqlite3.connect(str(home / "data" / "studio.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM approval_gates WHERE status = 'pending' LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None, "expected a pending gate"
    return str(row["id"])


def _run_status(home: Path) -> str:
    conn = sqlite3.connect(str(home / "data" / "studio.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM pipeline_runs LIMIT 1").fetchone()
    conn.close()
    return str(row["status"])


@pytest.mark.ffmpeg
def test_cli_end_to_end_demo(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="clie2e")
    monkeypatch.setenv("STUDIO_HOME", str(home))

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["run", "--demo", "--profile", "sandbox"])
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip()
        for line in result.output.splitlines()
        if line.startswith("run id:")
    )
    assert run_id

    # worker#1: script stage, then parks at G1 with nothing enqueued
    result = runner.invoke(app, ["worker", "--until-idle"])
    assert result.exit_code == 0, result.output
    assert _run_status(home) == "awaiting_approval"

    for stage in ("G1_SCRIPT", "G2_KEYFRAMES", "G3_FINAL_CUT"):
        gate_id = _pending_gate_id(home)
        result = runner.invoke(app, ["gates", "approve", gate_id])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["worker", "--until-idle"])
        assert result.exit_code == 0, result.output
        if stage != "G3_FINAL_CUT":
            assert _run_status(home) == "awaiting_approval"

    assert _run_status(home) == "completed"

    # the manifest asset exists and resolves
    conn = sqlite3.connect(str(home / "data" / "studio.db"))
    conn.row_factory = sqlite3.Row
    manifest = conn.execute(
        "SELECT sha256, rel_path FROM assets WHERE kind = 'manifest'"
    ).fetchone()
    assert manifest is not None
    rel_path = str(manifest["rel_path"])
    assert "/" in rel_path and "\\" not in rel_path
    assert (home / "data" / "cas" / rel_path).is_file()
    conn.close()


# --------------------------------------------------------------- T-CLI-03


def _deliverable_id(home: Path) -> str:
    conn = sqlite3.connect(str(home / "data" / "studio.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM deliverables LIMIT 1").fetchone()
    conn.close()
    assert row is not None
    return str(row["id"])


def _series_slug(home: Path) -> str:
    conn = sqlite3.connect(str(home / "data" / "studio.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT slug FROM series LIMIT 1").fetchone()
    conn.close()
    assert row is not None
    return str(row["slug"])


@pytest.mark.ffmpeg
def test_cli_03_library_scan_voice_set_run_preview_doctor(tmp_path, monkeypatch):
    """T-CLI-03: library scan → voice set → sandbox run → audio preview →
    doctor reports the new Phase 3 checks."""
    home = make_home(tmp_path, filename="clit03")
    monkeypatch.setenv("STUDIO_HOME", str(home))

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    # scan on a home without a library yet: warns, exits 0
    result = runner.invoke(app, ["library", "scan"])
    assert result.exit_code == 0, result.output
    # rich wraps at console width; normalize whitespace before the substring check
    assert "does not exist" in " ".join(result.output.split())

    # seed the demo sandbox (fixtures land under <home>/library)
    result = runner.invoke(app, ["run", "--demo", "--profile", "sandbox"])
    assert result.exit_code == 0, result.output

    # now the scan reconciles the seeded fixtures
    result = runner.invoke(app, ["library", "scan"])
    assert result.exit_code == 0, result.output
    assert "active tracks:" in result.output

    # voice set (re-attest explicitly)
    slug = _series_slug(home)
    result = runner.invoke(
        app,
        ["voice", "set", "narrator", "--series", slug, "--language", "ar",
         "--voice-id", "mock-voice-ar", "--model-key", "mock.tts", "--attest-commercial"],
    )
    assert result.exit_code == 0, result.output
    assert "attestation:" in result.output

    # drain the pipeline through the three gates
    result = runner.invoke(app, ["worker", "--until-idle"])
    assert result.exit_code == 0, result.output
    for stage in ("G1_SCRIPT", "G2_KEYFRAMES", "G3_FINAL_CUT"):
        gate_id = _pending_gate_id(home)
        result = runner.invoke(app, ["gates", "approve", gate_id])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["worker", "--until-idle"])
        assert result.exit_code == 0, result.output
        if stage != "G3_FINAL_CUT":
            assert _run_status(home) == "awaiting_approval"
    assert _run_status(home) == "completed"

    # audio preview: a playable stereo FLAC in data/previews/
    deliverable_id = _deliverable_id(home)
    result = runner.invoke(app, ["audio", "preview", deliverable_id])
    assert result.exit_code == 0, result.output
    previews = sorted((home / "data" / "previews").glob("*-master-*.flac"))
    assert previews, "expected a master FLAC in data/previews"
    info = probe(previews[-1])
    assert (info.duration_ms or 0) > 0
    assert info.audio_channels == 2

    # doctor reports the new Phase 3 checks (no FAILs on the demo home)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for expected in (
        "ffmpeg filtergraph file option",
        "sample rate % fps",
        "ELEVENLABS_API_KEY",
        "tts.keep_diacritics",
        "audio library",
        "voice attestations",
        "TTS registry tts.main",
    ):
        assert expected in result.output
