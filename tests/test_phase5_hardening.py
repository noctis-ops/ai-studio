"""Phase 5 §D hardening tests.

* **T-P4F-01** — policy-blocked recovery: a shot reaches ``failed_policy``, the
  operator edits with ``studio shot regenerate --prompt-file``, ``studio resume``
  re-runs it, and sibling shots are **not** re-paid.
* **T-P4F-02** — clip regeneration never changes S6's locked values and the new
  conformed clip has exactly ``clip_frames`` frames.
* **T-CFG-06** — the deprecated circuit-breaker key loads with a warning and is
  absent from the model and its defaults.

Fully offline: sandbox profile, mock providers, real ffmpeg.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from media_env import (
    SpyImageProvider,
    approve_parked,
    build_env,
    drain,
    pending_gates,
    run_to_g0,
    run_to_s10,
    stage_outputs,
)
from typer.testing import CliRunner

from studio.cli import app
from studio.core.clock import format_ts
from studio.domain.errors import ContentPolicyError
from studio.media.conform import clip_frames_for
from studio.providers.mock.image import MockImageProvider

runner = CliRunner()

TRAP = "POLICY-TRAP"


class TrapImageProvider(SpyImageProvider):
    """Refuses (once) any image request whose prompt contains :data:`TRAP`."""

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self.trapped = 0

    async def submit(self, req):  # type: ignore[no-untyped-def]
        prompt = str(req.params.get("prompt", ""))
        if TRAP in prompt:
            self.trapped += 1
            raise ContentPolicyError("mock: content policy refusal", billed=True)
        return await super().submit(req)


async def _run_worker(env, rounds: int = 3) -> None:
    """Drain the worker, re-anchoring CLI-enqueued jobs to the harness clock.

    The CLI opens its own repositories and stamps `jobs.run_after` with the
    system clock, while the test harness runs on `FakeClock`. Rewinding the
    queued rows is the test-only equivalent of the worker's real-time wait.
    """
    for _ in range(rounds):
        env.db.connect().execute(
            "UPDATE jobs SET run_after = ? WHERE status = 'queued'",
            (format_ts(env.clock.now()),),
        )
        await drain(env)


def _shot_rows(env) -> list:
    return env.db.connect().execute("SELECT * FROM shots ORDER BY shot_no").fetchall()


# ======================================================================
# T-P4F-01 — policy-blocked recovery costs nothing for siblings
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_p4f_01_policy_blocked_recovery_reaches_g2_without_repaying_siblings(
    tmp_path, monkeypatch
):
    image = TrapImageProvider(
        ledger_path=tmp_path / "img_jobs.json",
        width_16x9=960,
        height_16x9=540,
        poll_ticks=1,
    )
    env = build_env(
        tmp_path,
        monkeypatch,
        image_provider=image,
        settings_overrides={"sanitizer": {"max_levels": 0}},
    )
    await run_to_g0(env)
    # Shots exist once S3 has run. Trap exactly one shot's keyframe prompt
    # before approving G0 (sheets are already generated, so no trap fires yet).
    shots = _shot_rows(env)
    assert len(shots) >= 2
    target = shots[0]
    env.db.connect().execute(
        "UPDATE shots SET prompt = ? WHERE id = ?",
        (f"{TRAP}: the lighthouse keeper faces the storm", target["id"]),
    )
    approve_parked(env, "G0_CHARACTERS", note="sheets approved")
    await drain(env)

    # The trapped shot failed policy; the run is failed and parks the shot.
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == "failed"
    attempts = env.db.connect().execute(
        "SELECT * FROM shot_attempts WHERE shot_id = ? AND kind = 'keyframe'",
        (target["id"],),
    ).fetchall()
    assert [row["qc_status"] for row in attempts] == ["failed_policy"]
    assert image.trapped == 1, "the sanitizer cap must stop after one refusal"

    # Siblings were paid exactly once; nothing else is re-submitted yet.
    before = len(image.requests)
    siblings = {row["id"] for row in shots[1:]}

    prompt_file = tmp_path / "edited-prompt.txt"
    prompt_file.write_text("the lighthouse keeper shields the lantern", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "shot", "regenerate", env.run_row.id, target["id"],
            "--stage", "keyframe",
            "--prompt-file", str(prompt_file),
            "--reason", "policy edit",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "worst-case reserve" in result.output

    # `shot regenerate` reopens a failed run itself (documented). If an
    # operator instead runs `studio resume`, the engine resets failed rows.
    if env.repos.pipeline.get_run(env.run_row.id).status == "failed":
        resume = runner.invoke(app, ["resume", env.run_row.id])
        assert resume.exit_code == 0, resume.output
    await _run_worker(env)

    assert env.repos.pipeline.get_run(env.run_row.id).status != "failed"
    assert pending_gates(env, "G2_KEYFRAMES"), "recovery must reach the G2 park"

    keyframes = {
        out["shot_id"]: out
        for out in stage_outputs(env, "s8_keyframes")
    }
    assert keyframes[target["id"]]["qc_status"] in ("pass", "warn")

    # Only the edited shot was paid again: the new submits are exactly its own.
    new_submits = image.requests[before:]
    assert new_submits, "the edited shot must be regenerated"
    per_shot = {}
    for shot_id in siblings | {target["id"]}:
        row = env.db.connect().execute(
            "SELECT COUNT(*) AS n FROM shot_attempts WHERE shot_id = ? AND kind = 'keyframe'",
            (shot_id,),
        ).fetchone()
        per_shot[shot_id] = int(row["n"])
    assert per_shot[target["id"]] == 2, "the edited shot has two attempts"
    assert all(per_shot[s] == 1 for s in siblings), "siblings must not be re-attempted"


# ======================================================================
# T-P4F-02 — clip regeneration preserves every locked S6 value
# ======================================================================


def _count_frames(path: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_entries", "stream=nb_read_frames",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
    )
    data = json.loads(out.stdout.decode("utf-8"))
    return int(data["streams"][0]["nb_read_frames"])


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_p4f_02_clip_regeneration_keeps_s6_locked_values(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch)
    await run_to_s10(env)

    locked_before = {
        row["id"]: (
            row["clip_duration_ms"],
            row["speed_factor"],
            row["hold_frames"],
        )
        for row in _shot_rows(env)
    }
    timing_before = json.loads(
        env.db.connect()
        .execute("SELECT output_json FROM stage_runs WHERE stage = 's6_timing'")
        .fetchone()["output_json"]
    )
    target = _shot_rows(env)[0]

    result = runner.invoke(
        app,
        [
            "shot", "regenerate", env.run_row.id, target["id"],
            "--stage", "clip", "--reason", "T-P4F-02", "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    await _run_worker(env)

    locked_after = {
        row["id"]: (
            row["clip_duration_ms"],
            row["speed_factor"],
            row["hold_frames"],
        )
        for row in _shot_rows(env)
    }
    assert locked_after == locked_before

    timing_after = json.loads(
        env.db.connect()
        .execute("SELECT output_json FROM stage_runs WHERE stage = 's6_timing'")
        .fetchone()["output_json"]
    )
    assert json.dumps(timing_after, sort_keys=True) == json.dumps(
        timing_before, sort_keys=True
    )

    clips = {out["shot_id"]: out for out in stage_outputs(env, "s10_clips")}
    out = clips[target["id"]]
    expected = clip_frames_for(int(target["clip_duration_ms"]), env.config.settings.render.fps)
    assert int(out["clip_frames"]) == expected
    conformed = env.cas.resolve(out["conformed_asset_id"])
    assert _count_frames(conformed) == expected

    # Two clip attempts for the regenerated shot, one for every sibling.
    rows = env.db.connect().execute(
        "SELECT shot_id, COUNT(*) AS n FROM shot_attempts WHERE kind = 'clip' GROUP BY shot_id"
    ).fetchall()
    counts = {row["shot_id"]: int(row["n"]) for row in rows}
    assert counts[target["id"]] == 2
    assert all(count == 1 for shot_id, count in counts.items() if shot_id != target["id"])


# ======================================================================
# T-CFG-06 — deprecated circuit-breaker key
# ======================================================================


def test_cfg_06_deprecated_circuit_breaker_is_ignored_with_warning(tmp_path, monkeypatch):
    import warnings

    from conftest import make_home

    from studio.config.settings import (
        MediaSettings,
        Settings,
        deprecated_settings_in_file,
        load_settings_file_with_report,
    )

    home = make_home(
        tmp_path,
        settings_overrides={"media": {"circuit_breaker_consecutive_failures": 3}},
        registry_overrides={},
        filename="deprecated",
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    path = home / "configs" / "settings.yaml"
    assert "circuit_breaker_consecutive_failures" in path.read_text(encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        settings, notes = load_settings_file_with_report(path)
    assert isinstance(settings, Settings)
    assert notes and "fail-fast" in notes[0]
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)

    assert not hasattr(MediaSettings(), "circuit_breaker_consecutive_failures")
    assert not hasattr(settings.media, "circuit_breaker_consecutive_failures")
    assert "circuit_breaker_consecutive_failures" not in MediaSettings.model_fields
    assert deprecated_settings_in_file(path) == [
        "media.circuit_breaker_consecutive_failures"
    ]

    # An unknown key still fails closed (the deprecation list is not a hole).
    from studio.domain.errors import ConfigError

    bad = home / "configs" / "settings_bad.yaml"
    bad.write_text(
        (path.read_text(encoding="utf-8")).replace(
            "  max_paid_attempts_per_shot: 4",
            "  max_paid_attempts_per_shot: 4\n  not_a_real_knob: 1",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings_file_with_report(bad)


# keep the import used on all platforms (mypy strict)
assert MockImageProvider is not None


# ======================================================================
# T-COV-01 — the per-module coverage floor gate actually fails
# ======================================================================


def test_cov_01_floor_script_fails_below_floor_and_on_stale_rules(tmp_path):
    import importlib.util
    import json

    script = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage_floors.py"
    spec = importlib.util.spec_from_file_location("check_coverage_floors", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    coverage = {
        "files": {
            "src/studio/assemble/render.py": {
                "summary": {"percent_covered": 89.0}
            },
            "src/studio/media/clips.py": {"summary": {"percent_covered": 70.0}},
        },
        "totals": {"percent_covered": 92.0},
    }
    floors = {
        "default_floor": 0.0,
        "total_floor": 91.0,
        "floors": [
            {"glob": "src/studio/assemble/*.py", "floor": 88.0},
            {"glob": "src/studio/media/clips.py", "floor": 85.0},
            {"glob": "src/studio/export/*.py", "floor": 88.0},
        ],
    }
    problems = module.check(coverage, floors)
    assert any("clips.py" in line and "70.00% < floor 85.00%" in line for line in problems)
    assert any("export" in line and "matched no file" in line for line in problems)
    assert not any("assemble" in line and "floor" in line for line in problems)

    cov_path = tmp_path / "coverage.json"
    floors_path = tmp_path / "floors.json"
    cov_path.write_text(json.dumps(coverage), encoding="utf-8")
    floors_path.write_text(json.dumps(floors), encoding="utf-8")
    assert module.main(["check", str(cov_path), str(floors_path)]) == 1

    floors["floors"] = [{"glob": "src/studio/assemble/*.py", "floor": 88.0}]
    floors["default_floor"] = 60.0
    floors_path.write_text(json.dumps(floors), encoding="utf-8")
    assert module.main(["check", str(cov_path), str(floors_path)]) == 0

    # A missing total floor violation is reported too.
    coverage["totals"] = {"percent_covered": 90.0}
    cov_path.write_text(json.dumps(coverage), encoding="utf-8")
    assert module.main(["check", str(cov_path), str(floors_path)]) == 1
