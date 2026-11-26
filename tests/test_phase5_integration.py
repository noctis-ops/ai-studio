"""Phase 5 step 9: integration (T-ENG-17/18/19) and the CLI surface (T-CLI-05).

Three end-to-end shapes:

- **T-ENG-17** the sandbox *demo* DAG through S13 with the real handlers and
  real ffmpeg: paid-call counts follow the Phase 4 formula, S14 makes exactly
  one mock LLM call, and S6's locked columns are unchanged.
- **T-ENG-18** a production-shaped run whose gates are approved in order
  G1 → G0 → G2 → G3, asserting that no paid call happens before its ancestors
  are approved.
- **T-ENG-19** `--languages ar,en`: image/video counts unchanged, TTS/mix/S9/
  S11/S12/S13 double, and two export directories are written.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import build_gateway, make_home
from media_env import (
    SimpleEnv,
    add_music_and_scene,
    approve_parked,
    build_env,
    drain,
    pending_gates,
)

from studio.config.loader import load_all
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.demo.seed import seed_demo
from studio.demo.stages import demo_specs
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.worker import Worker
from studio.providers.mock.image import MockImageProvider
from studio.providers.mock.tts import MockTTSProvider
from studio.providers.mock.video import MockVideoProvider

pytestmark = pytest.mark.ffmpeg


class _Ids:
    def __init__(self, prefix: str = "itg") -> None:
        self._n = 0
        self._prefix = prefix

    def new(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n:06d}"


def _demo_env(tmp_path, monkeypatch, *, languages=("ar",), filename="demo-int"):
    home = make_home(tmp_path, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = _Ids(filename)
    repos = build_repos(db, clock, ids)
    seeded = seed_demo(config, repos, languages=list(languages), aspects=["9:16"], ids=ids)
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id,
        profile="sandbox",
        auto_approve=False,
        config_snapshot_json=None,
    )
    repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": run_row.id}, dedupe_key=f"advance:{run_row.id}"
    )
    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    mock_dir = home / "data" / "mock"
    mock_dir.mkdir(parents=True, exist_ok=True)
    image = MockImageProvider(
        ledger_path=mock_dir / "image_jobs.json", width_16x9=960, height_16x9=540,
        ids=ids, poll_ticks=2,
    )
    video = MockVideoProvider(
        ledger_path=mock_dir / "video_jobs.json", width_16x9=960, height_16x9=540,
        fps=24, ids=ids, poll_ticks=2,
    )
    tts = MockTTSProvider(
        chars_per_second=int(config.settings.mock.tts_chars_per_second),
        workdir=mock_dir / "tts", ids=ids,
    )
    gateway = build_gateway(
        db=db, config=config, clock=clock, ids=ids, cas=cas,
        providers={"mock.image": image, "mock.video": video, "mock.tts": tts},
    )
    dag = Dag(demo_specs())
    engine = Engine(
        db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids, gateway=gateway
    )
    worker = Worker(db, engine, repos, config, clock, worker_id="itg-worker", sleep=no_op_sleep)
    return SimpleEnv(
        home=home, config=config, db=db, clock=clock, ids=ids, repos=repos, seeded=seeded,
        run_row=run_row, gateway=gateway, dag=dag, worker=worker, image=image, video=video,
        tts=tts, cas=cas,
    )


async def _approve_everything(env, *, order=("G1_SCRIPT", "G0_CHARACTERS", "G2_KEYFRAMES", "G3_FINAL_CUT")):
    for _ in range(12):
        progressed = False
        for gate in order:
            if pending_gates(env, gate):
                approve_parked(env, gate)
                await env.worker.run(until_idle=True)
                progressed = True
        if not progressed:
            return
    raise AssertionError("gates kept parking; the run never settled")


def _calls(env) -> dict[str, int]:
    rows = env.db.connect().execute(
        "SELECT capability, count(*) AS n FROM provider_calls GROUP BY capability"
    ).fetchall()
    return {str(row["capability"]): int(row["n"]) for row in rows}


def _stage_status(env, stage: str) -> list[str]:
    rows = env.db.connect().execute(
        "SELECT status FROM stage_runs WHERE stage = ?", (stage,)
    ).fetchall()
    return [str(row["status"]) for row in rows]


def _s6_snapshot(env) -> dict[str, tuple]:
    rows = env.db.connect().execute(
        "SELECT id, clip_duration_ms, speed_factor, hold_frames FROM shots"
    ).fetchall()
    return {
        row["id"]: (row["clip_duration_ms"], row["speed_factor"], row["hold_frames"])
        for row in rows
    }


def test_eng_17_demo_dag_through_s13(tmp_path, monkeypatch):
    async def scenario() -> dict:
        env = _demo_env(tmp_path, monkeypatch)
        await env.worker.run(until_idle=True)
        s6_before = None
        for _ in range(12):
            progressed = False
            for gate in ("G1_SCRIPT", "G0_CHARACTERS", "G2_KEYFRAMES", "G3_FINAL_CUT"):
                if pending_gates(env, gate):
                    approve_parked(env, gate)
                    await env.worker.run(until_idle=True)
                    progressed = True
            if s6_before is None and _stage_status(env, "s6_timing") == ["succeeded"]:
                # S6 is the last stage that may touch shot timing; lock it in as
                # soon as it lands so S9/S11/S12/S13 are proven not to move it.
                s6_before = _s6_snapshot(env)
            if not progressed:
                break
        return {"env": env, "s6_before": s6_before}

    state = asyncio.run(scenario())
    env, s6_before = state["env"], state["s6_before"]
    assert s6_before, "S6 never succeeded"

    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == "completed"

    # The real tail ran: animatic, assemble, render QC, export.
    for stage in ("s9_animatic", "s11_assemble", "s12_render_qc", "s13_export"):
        assert _stage_status(env, stage), f"{stage} never ran"
        assert all(status == "succeeded" for status in _stage_status(env, stage)), stage

    # Exactly one export directory per deliverable, with the sidecar and thumbs.
    deliverables = env.repos.content.list_deliverables(env.run_row.episode_id)
    assert len(deliverables) == 1
    exports = env.repos.exports.list_for_deliverable(deliverables[0].id)
    assert len(exports) == 1
    directory = Path(exports[0].dir_path)
    assert (directory / "metadata.json").is_file()
    assert list((directory / "thumbnail_candidates").glob("frame_*.png"))

    # S14: exactly one mock LLM call for bible_update, one pending proposal.
    calls = _calls(env)
    assert calls["llm"] == 2  # demo_script + bible_update
    proposals = env.repos.proposals.list_for_episode(env.run_row.episode_id)
    assert len(proposals) == 1 and proposals[0].status == "pending"
    # ...and the bible was not touched by the run.
    assert len(env.repos.bible.list_versions(env.seeded.series_id)) <= 1

    # TTS is unchanged from Phase 4 and no stage re-paid on resume.
    assert calls["tts"] == 3
    assert calls["image_reference_generate"] == 3
    assert calls["image_to_video"] == 3

    # S6's locked timing values are byte-identical after S9/S11/S12/S13.
    assert _s6_snapshot(env) == s6_before
    assert len(_stage_status(env, "s6_timing")) == 1


def test_eng_18_no_paid_call_before_its_gates(tmp_path, monkeypatch):
    """Gates are approved strictly G1 → G0 → G2 → G3 and each blocks the spend
    of the stage it guards (nothing downstream runs on an unapproved gate)."""

    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="eng-18")
        await drain(env)
        add_music_and_scene(env)

        seen: list[str] = []

        def pending() -> list[str]:
            rows = env.db.connect().execute(
                "SELECT gate FROM approval_gates WHERE status = 'pending' ORDER BY rowid"
            ).fetchall()
            return [str(row["gate"]) for row in rows]

        # Park 1: G1 round 1. Only the free text stages have run.
        assert pending() == ["G1_SCRIPT"]
        seen.append("G1_SCRIPT")
        assert _calls(env) == {"llm": 4}
        assert _stage_status(env, "s5_tts") == []
        assert _stage_status(env, "s8_keyframes") == []
        assert _stage_status(env, "s14_bible_proposals") == []

        approve_parked(env, "G1_SCRIPT")
        await drain(env)
        # G1 round 2 (post-S14) parks; S14 has run, still nothing paid in media.
        assert pending() == ["G1_SCRIPT"]
        seen.append("G1_SCRIPT")
        assert _stage_status(env, "s14_bible_proposals") == ["succeeded"]
        media = {"image_reference_generate", "image_to_video", "tts"}
        assert media.isdisjoint(_calls(env)), "media spend before G0"

        approve_parked(env, "G1_SCRIPT")
        await drain(env)
        # Park 2: G0. Voice + mix + character sheets ran, keyframes did not.
        assert pending() == ["G0_CHARACTERS"]
        seen.append("G0_CHARACTERS")
        info = _calls(env)
        assert info["tts"] == 3
        assert info["image_reference_generate"] == 2
        assert "image_to_video" not in info
        assert _stage_status(env, "s8_keyframes") == []

        approve_parked(env, "G0_CHARACTERS")
        await drain(env)
        # Park 3: G2. Keyframes + animatic exist; no clip has been generated.
        assert pending() == ["G2_KEYFRAMES"]
        seen.append("G2_KEYFRAMES")
        info = _calls(env)
        assert info["image_reference_generate"] == 5
        assert "image_to_video" not in info
        assert _stage_status(env, "s9_animatic") == ["succeeded"]
        assert _stage_status(env, "s10_clips") == []

        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        # Park 4: G3. Clips, assemble and render QC ran; nothing was exported.
        assert pending() == ["G3_FINAL_CUT"]
        seen.append("G3_FINAL_CUT")
        info = _calls(env)
        assert info["image_to_video"] == 3
        assert _stage_status(env, "s12_render_qc")[0] == "succeeded"
        assert _stage_status(env, "s13_export") == []

        approve_parked(env, "G3_FINAL_CUT")
        await drain(env)
        assert pending() == []
        assert _stage_status(env, "s13_export") == ["succeeded"]
        run = env.repos.pipeline.get_run(env.run_row.id)
        assert run is not None and run.status == "completed"
        assert seen == ["G1_SCRIPT", "G1_SCRIPT", "G0_CHARACTERS", "G2_KEYFRAMES", "G3_FINAL_CUT"]

    asyncio.run(scenario())


def test_eng_19_two_languages_double_the_per_language_work(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="eng-19", languages=("ar", "en"))
        await drain(env)
        add_music_and_scene(env)
        for _ in range(12):
            progressed = False
            for gate in ("G1_SCRIPT", "G0_CHARACTERS", "G2_KEYFRAMES", "G3_FINAL_CUT"):
                if pending_gates(env, gate):
                    approve_parked(env, gate)
                    await drain(env)
                    progressed = True
            if not progressed:
                break

        deliverables = env.repos.content.list_deliverables(env.run_row.episode_id)
        assert len(deliverables) == 2
        dirs = set()
        for deliverable in deliverables:
            exports = env.repos.exports.list_for_deliverable(deliverable.id)
            assert len(exports) == 1, deliverable.id
            dirs.add(exports[0].dir_path)
        assert len(dirs) == 2, "one export directory per language"

        calls = _calls(env)
        # Per-language work doubles (TTS); per-series/per-shot work does not:
        # 2 character sheets + 3 keyframes and 3 clips either way.
        assert calls["tts"] == 6
        assert calls["image_reference_generate"] == 5
        assert calls["image_to_video"] == 3
        for stage in ("s9_animatic", "s11_assemble", "s12_render_qc", "s13_export"):
            assert len(_stage_status(env, stage)) == 2, stage

    asyncio.run(scenario())


def test_cli_05_new_commands_refuse_without_yes(tmp_path, monkeypatch):
    """Every destructive/paid Phase 5 command refuses without `--yes`."""

    async def prepare():
        env = await _prepared(tmp_path, monkeypatch)
        return env

    monkeypatch.setenv("COLUMNS", "220")
    env = asyncio.run(prepare())
    from typer.testing import CliRunner

    from studio.cli import app

    runner = CliRunner()
    episode = env.run_row.episode_id or ""
    deliverable = env.repos.content.list_deliverables(episode)[0]

    # `studio export` is free but refuses an unknown deliverable / run.
    assert runner.invoke(app, ["export", env.run_row.id]).exit_code == 0
    assert runner.invoke(app, ["export", "nope"]).exit_code != 0
    assert (
        runner.invoke(app, ["export", env.run_row.id, "--deliverable", "nope"]).exit_code != 0
    )
    assert runner.invoke(
        app, ["export", env.run_row.id, "--override-commercial-use"]
    ).exit_code != 0  # needs --reason

    # Paid actions print their worst-case cost *first* and refuse without --yes.
    propose = runner.invoke(app, ["bible", "propose", episode])
    assert propose.exit_code != 0
    assert "worst-case cost: $" in propose.output
    assert "re-run with --yes" in propose.output
    assert len(env.repos.proposals.list_for_episode(episode)) == 1  # the S14 one only

    approved_propose = runner.invoke(app, ["bible", "propose", episode, "--yes"])
    assert approved_propose.exit_code == 0, approved_propose.output
    assert "worst-case cost: $" in approved_propose.output
    assert "pending operator review" in approved_propose.output
    assert len(env.repos.proposals.list_for_episode(episode)) == 2  # S14 + this one
    assert runner.invoke(
        app, ["publish", "mark", deliverable.id, "--url", "https://e.test"]
    ).exit_code != 0
    assert runner.invoke(app, ["purge", "run", "--episode", episode, "--yes"]).exit_code != 0

    # Free, read-only commands all work.
    for args in (
        ["bible", "proposals"],
        ["purge", "plan", "--episode", episode],
        ["purge", "gc"],
        ["purge", "run", "--episode", episode],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)

    # `doctor` renders the Phase 5 rows (it exits 1 only for missing keys/NVENC).
    doctor = runner.invoke(app, ["doctor"])
    assert "export.root writable" in doctor.output
    assert "presets vs channels" in doctor.output
    assert "retention windows" in doctor.output


async def _prepared(tmp_path, monkeypatch):
    """A production-shaped run driven all the way through G3 to S13."""
    env = build_env(tmp_path, monkeypatch, filename="cli-05")
    await drain(env)
    add_music_and_scene(env)
    for _ in range(12):
        progressed = False
        for gate in ("G1_SCRIPT", "G0_CHARACTERS", "G2_KEYFRAMES", "G3_FINAL_CUT"):
            if pending_gates(env, gate):
                approve_parked(env, gate)
                await drain(env)
                progressed = True
        if not progressed:
            return env
    raise AssertionError("gates kept parking; the run never settled")
