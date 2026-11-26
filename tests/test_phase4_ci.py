"""T-CI-02: CI wiring for the Phase 4 surface (PHASE4 §P / H3).

The YAML workflows are parsed, not greped, so a malformed workflow fails
loudly. The ffmpeg capability list is the shared
``studio.video.ffmpeg.REQUIRED_FILTERS`` constant — the same one
``scripts/check_ffmpeg.py`` and ``studio doctor`` use.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SLOW_YML = REPO_ROOT / ".github" / "workflows" / "slow.yml"
CHECK_FFMPEG = REPO_ROOT / "scripts" / "check_ffmpeg.py"

# H3 extension list (PHASE4_IMPLEMENTATION_PROMPT.md H3).
NEW_FILTERS = (
    "signalstats", "blackdetect", "freezedetect", "ssim", "fps", "trim",
    "setpts", "tpad", "scale", "setsar", "format",
)

# The audio set from PHASE4 §D H3 that must remain present.
AUDIO_FILTERS = (
    "loudnorm", "ebur128", "astats", "sidechaincompress", "volume", "amix",
    "adelay", "afade", "atrim", "apad", "aresample", "aformat",
    "silenceremove", "areverse", "alimiter", "anullsrc", "asetnsamples",
    "ametadata", "pan", "asplit",
)

COV_PACKAGES = (
    "studio.core", "studio.db", "studio.pipeline", "studio.audio",
    "studio.providers.tts", "studio.qc", "studio.video", "studio.media",
    "studio.providers",
    # Phase 5 §Q: the new packages joined the gate.
    "studio.assemble", "studio.export", "studio.retention", "studio.bible",
)


def _ci() -> dict:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def _slow() -> dict:
    return yaml.safe_load(SLOW_YML.read_text(encoding="utf-8"))


def _steps(workflow: dict) -> list[dict]:
    steps: list[dict] = []
    for job in workflow["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _run_lines(workflow: dict) -> list[str]:
    return [str(step.get("run", "")) for step in _steps(workflow)]


def test_ci_02_ffmpeg_capability_list_has_the_new_filters() -> None:
    from studio.video.ffmpeg import REQUIRED_FILTERS

    for name in (*NEW_FILTERS, *AUDIO_FILTERS):
        assert name in REQUIRED_FILTERS, f"required ffmpeg filter missing: {name}"


def test_ci_02_check_ffmpeg_script_uses_the_shared_capability_check() -> None:
    from studio.video.ffmpeg import capability_check  # noqa: F401  (import must succeed)

    source = CHECK_FFMPEG.read_text(encoding="utf-8")
    assert "capability_check" in source
    assert "REQUIRED_FILTERS" in source


def test_ci_02_ci_runs_the_ffmpeg_capability_gate_before_tests() -> None:
    steps = _steps(_ci())
    runs = [str(step.get("run", "")) for step in steps]
    gate_index = next(
        (i for i, run in enumerate(runs) if "check_ffmpeg.py" in run), None
    )
    assert gate_index is not None, "ci.yml never runs scripts/check_ffmpeg.py"
    test_index = next(
        i for i, run in enumerate(runs) if "pytest" in run and "tests/" in run
    )
    assert gate_index < test_index, "ffmpeg capability gate must run before the test step"


def test_ci_02_ci_coverage_paths_and_threshold() -> None:
    runs = _run_lines(_ci())
    coverage_runs = [run for run in runs if "--cov=" in run]
    assert coverage_runs, "ci.yml has no coverage step"
    joined = "\n".join(coverage_runs)
    for package in COV_PACKAGES:
        assert f"--cov={package}" in joined, f"coverage path missing: {package}"
    # Phase 5 §Q raised the total gate from 90% to 91%.
    assert "--cov-fail-under=91" in joined
    # ...and CI runs the per-module floors script on the JSON report.
    runs = _run_lines(_ci())
    assert any("check_coverage_floors.py" in run for run in runs)
    assert any("--cov-report=json" in run for run in runs)


def test_ci_02_ci_matrix_and_quality_steps() -> None:
    workflow = _ci()
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]
    for operating_system in ("ubuntu-latest", "windows-latest"):
        assert operating_system in matrix["os"]
    for version in ("3.11", "3.12"):
        assert version in matrix["python-version"]
    runs = _run_lines(workflow)
    assert any("ruff check src tests" in run for run in runs)
    assert any(run.strip() == "mypy" or "mypy" in run for run in runs)


def test_ci_02_slow_workflow_present_with_weekly_cron_and_manual_dispatch() -> None:
    workflow = _slow()
    # YAML parses the bare `on:` key as boolean True.
    on = workflow.get("on", workflow.get(True))
    assert on is not None, "slow.yml has no trigger"
    assert "workflow_dispatch" in on
    crons = [entry["cron"] for entry in on["schedule"]]
    assert crons, "slow.yml schedule must carry at least one cron entry"
    runs = _run_lines(workflow)
    assert any("-m slow" in run for run in runs), "slow workflow must run -m slow"
    assert any("check_ffmpeg.py" in run for run in runs)


def test_ci_02_pyproject_declares_the_ffmpeg_and_slow_markers() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"slow:' in pyproject
    assert '"ffmpeg:' in pyproject
