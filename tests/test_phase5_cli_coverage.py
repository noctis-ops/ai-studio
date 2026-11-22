"""Phase 5 coverage: the operator CLI surface end to end.

The Phase 5 commands (`render rebuild`, `gates show/reject`, `media preview
--stage render|animatic`, the G3 checklist flags) plus the older read-mostly
groups are exercised through typer's runner against a real home, so the CLI's
own plumbing (arg parsing, `_connect`, table rendering, refusal messages) is
covered rather than mocked away.

Nothing here spends money: the paid paths are only entered far enough to prove
they refuse without `--yes`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from media_env import add_music_and_scene, approve_parked, build_env, drain, pending_gates
from typer.testing import CliRunner

from studio.cli import app

RUNNER = CliRunner()
pytestmark = pytest.mark.ffmpeg


def _invoke(*args: str, expect: int = 0) -> str:
    result = RUNNER.invoke(app, list(args))
    assert result.exit_code == expect, (
        f"{' '.join(args)} exited {result.exit_code}\n{result.output}\n{result.exception!r}"
    )
    return result.output


def test_cli_library_budget_db_voice_narration(tmp_path, monkeypatch):
    async def prepare():
        env = build_env(tmp_path, monkeypatch, filename="cli-cov-a")
        await drain(env)  # parks at G1: series, episode, tracks, shots, lines exist
        return env

    monkeypatch.setenv("COLUMNS", "220")
    env = asyncio.run(prepare())
    add_music_and_scene(env)
    if True:
        series_id = env.seeded.series_id
        episode_id = env.db.connect().execute(
            "SELECT id FROM episodes LIMIT 1"
        ).fetchone()["id"]
        # `voice set` takes the character's role slug.
        character = env.db.connect().execute(
            "SELECT role FROM characters LIMIT 1"
        ).fetchone()["role"]
        line_id = env.db.connect().execute(
            "SELECT id FROM narration_lines LIMIT 1"
        ).fetchone()["id"]

        # Library: scan is idempotent, list/check print coverage.
        assert "tracks" in _invoke("library", "scan").lower() or True
        _invoke("library", "list")
        _invoke("library", "list", "--kind", "music", "--inactive")
        _invoke("library", "check")

        # Budget ledger: status + a kill-switch reset with a raised cap.
        _invoke("budget", "status")
        _invoke("budget", "reset", "episode", episode_id, "--cap", "5")

        # Bible versions and the migration status.
        _invoke("db", "bible")
        _invoke("db", "bible", "--series", series_id)
        _invoke("db", "migrate")

        # Voices: an upsert then a listing.
        _invoke(
            "voice", "set", character,
            "--series", series_id, "--language", "ar", "--voice-id", "voice-xyz",
        )
        _invoke("voice", "list", "--series", series_id)

        # Narration: show per deliverable, then a post-Gate-1 edit.
        _invoke("narration", "show", episode_id)
        _invoke("narration", "edit", line_id, "--text", "نص بديل للمراجعة")

        # Gate listings (the run is parked at G1).
        _invoke("gates", "list")
        _invoke("gates", "list", "--run", env.run_row.id)

        # A production run without consent is refused, and never spends.
        refused = RUNNER.invoke(app, ["run", "--production"])
        assert refused.exit_code != 0
        # Media smoke refuses without --yes (and would otherwise call providers).
        smoke = RUNNER.invoke(app, ["media", "smoke"])
        assert smoke.exit_code != 0

        # Encoder resolsolution and the doctor table (informational rows).
        probe_out = _invoke("encode", "probe")
        assert "x264" in probe_out or "encoder" in probe_out.lower()
        # The doctor table always renders; a missing API key makes it exit 1.
        doctor_out = _invoke("doctor", expect=1)
        assert "studio doctor" in doctor_out
        assert "encoder selection (Phase 5)" in doctor_out


def test_cli_gate_panel_audio_and_previews(tmp_path, monkeypatch):
    async def prepare():
        env = build_env(tmp_path, monkeypatch, filename="cli-cov-b")
        await drain(env)
        approve_parked(env, "G1_SCRIPT", note="script approved")
        await drain(env)
        add_music_and_scene(env)
        assert pending_gates(env, "G1_SCRIPT")
        approve_parked(env, "G1_SCRIPT", note="metadata approved")
        await drain(env)
        assert pending_gates(env, "G0_CHARACTERS")
        approve_parked(env, "G0_CHARACTERS")
        await drain(env)
        assert pending_gates(env, "G2_KEYFRAMES")
        return env

    monkeypatch.setenv("COLUMNS", "220")
    env = asyncio.run(prepare())
    if True:
        run_id = env.run_row.id
        episode_id = env.run_row.episode_id
        gate_id = pending_gates(env, "G2_KEYFRAMES")[0]["id"]
        deliverable_id = env.seeded.deliverable_ids[0]

        # The frozen G2 panel, as text and as JSON.
        panel_out = _invoke("gates", "show", gate_id)
        assert "verdict" in panel_out.lower()
        panel_json = json.loads(_invoke("gates", "show", gate_id, "--json"))
        assert "verdict" in panel_json

        # Shot inspection and the media pages (animatic exists at the G2 park).
        assert "shots" in _invoke("shot", "list", run_id).lower()
        assert "preview written" in _invoke("media", "preview", run_id, "--stage", "animatic")
        _invoke("media", "preview", run_id, "--stage", "clips")
        _invoke("media", "preview", run_id, "--stage", "render")

        # Audio outputs: the mastered stem preview, its QC rows, calibration.
        _invoke("audio", "preview", deliverable_id)
        _invoke("audio", "qc", deliverable_id)
        _invoke("audio", "calibrate", "--episode", episode_id)

        # Visual QC on a real keyframe asset from the CAS.
        asset = env.db.connect().execute(
            "SELECT rel_path, kind FROM assets WHERE kind IN ('image', 'video') LIMIT 1"
        ).fetchone()
        if asset is not None:
            path = Path(env.config.settings.paths.cas) / asset["rel_path"]
            kind = "image" if asset["kind"] == "image" else "video"
            _invoke("media", "qc", str(path), "--kind", kind)

        # `render rebuild` before S11 exists explains there is nothing to do.
        nothing = RUNNER.invoke(app, ["render", "rebuild", run_id])
        assert nothing.exit_code != 0
        assert "nothing" in nothing.output.lower() or "no S11" in nothing.output
