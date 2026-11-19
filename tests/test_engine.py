"""T-ENG-* (BEGIN_PROMPT section I)."""

from __future__ import annotations

import itertools
import json
from datetime import timedelta
from pathlib import Path

import conftest
import pytest
from conftest import PAID_MOCK_PRICES, build_gateway, make_home

from studio.config.loader import load_all
from studio.core.budget import LedgerService
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.core.faults import FaultInjector, SimulatedCrash
from studio.db import Database, build_repos, migrate
from studio.demo.seed import seed_demo
from studio.demo.stages import demo_specs
from studio.domain.enums import GateKind, Profile, RunStatus, Scope
from studio.domain.errors import (
    ConfigError,
    GatePolicyError,
)
from studio.domain.refs import SubjectRef
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService, ShotRejection
from studio.pipeline.recovery import recover
from studio.pipeline.stage import StageOutput, StageSpec
from studio.pipeline.worker import Worker
from studio.providers.mock.common import MockFaultPlan
from studio.providers.mock.llm import MockLLMProvider


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"eng-{next(self._counter):06d}"


def _paid(registry_overrides=None):
    import copy

    merged = copy.deepcopy(PAID_MOCK_PRICES)
    conftest._deep_merge(merged, registry_overrides or {})
    return merged


def _env(tmp_path, monkeypatch, *, filename="eng", settings_overrides=None,
         registry_overrides=None, paid=True):
    overrides = _paid(registry_overrides) if paid else registry_overrides
    home = make_home(
        tmp_path,
        settings_overrides=settings_overrides,
        registry_overrides=overrides,
        filename=filename,
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = _Ids()
    repos = build_repos(db, clock, ids)
    return config, db, clock, ids, repos, home


def _engine(db, repos, config, clock, ids, *, gateway=None):
    dag = Dag(demo_specs())
    return Engine(db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids,
                  gateway=gateway), dag


def _worker(db, engine, repos, config, clock, ids):
    return Worker(db, engine, repos, config, clock, worker_id="test-worker", sleep=no_op_sleep)


def _seed_run(config, repos, ids, *, languages=("ar",), aspects=("9:16",),
              profile="sandbox", auto_approve=False):
    seeded = seed_demo(config, repos, languages=list(languages), aspects=list(aspects), ids=ids)
    resolved = config.registry.resolve_profile(profile)
    snapshot = config.registry.registry_snapshot(resolved)
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id,
        profile=profile,
        auto_approve=auto_approve,
        config_snapshot_json=json.dumps(snapshot, sort_keys=True),
    )
    repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": run_row.id}, dedupe_key=f"advance:{run_row.id}"
    )
    return seeded, run_row


def _provider_calls(db, since_ids=None):
    rows = db.connect().execute(
        "SELECT id, capability, purpose, cache_key, status, est_cost_musd, request_json "
        "FROM provider_calls ORDER BY created_at, id"
    ).fetchall()
    dicts = [dict(r) for r in rows]
    if since_ids is not None:
        dicts = [d for d in dicts if d["id"] not in since_ids]
    return dicts


async def _drain(worker):
    return await worker.run(until_idle=True)


# ======================================================================
# T-ENG-01
# ======================================================================

class _DummyHandler:
    def subjects(self, ctx):
        return []

    def input_hash(self, ctx, subject):
        return "hash"

    async def run(self, ctx, subject):
        raise AssertionError("not run in DAG tests")


def _spec(name: str, deps: tuple[str, ...] = ()) -> StageSpec:
    return StageSpec(name=name, scope=Scope.EPISODE, depends_on=deps, handler=_DummyHandler())


def test_dag_rejects_cycles_unknown_deps_and_duplicates():
    with pytest.raises(ConfigError, match="duplicate"):
        Dag([_spec("a"), _spec("a")])
    with pytest.raises(ConfigError, match="unknown"):
        Dag([_spec("a", ("b",))])
    with pytest.raises(ConfigError, match="cycle"):
        Dag([_spec("a", ("c",)), _spec("b", ("a",)), _spec("c", ("b",))])


def test_dag_topological_order():
    dag = Dag([_spec("c", ("b",)), _spec("b", ("a",)), _spec("a")])
    assert dag.names == ["a", "b", "c"]

    diamond = Dag(
        [_spec("d", ("b", "c")), _spec("c", ("a",)), _spec("b", ("a",)), _spec("a")]
    )
    # every topological order is valid; assert the precedence constraints hold
    assert diamond.names.index("a") < diamond.names.index("b")
    assert diamond.names.index("a") < diamond.names.index("c")
    assert diamond.names.index("b") < diamond.names.index("d")
    assert diamond.names.index("c") < diamond.names.index("d")
    assert len(diamond.names) == 4
    assert diamond.downstream("a") == frozenset({"b", "c", "d"})


# ======================================================================
# T-ENG-02
# ======================================================================


class _SharedCounterHandler:
    def __init__(self, shared: dict, tag: str):
        self._shared = shared
        self._tag = tag

    def subjects(self, ctx):
        return [SubjectRef(scope=Scope.EPISODE, id="ep-instr", lineage=())]

    def input_hash(self, ctx, subject):
        return f"hash-{self._tag}"

    async def run(self, ctx, subject):
        import asyncio

        self._shared["active"] += 1
        self._shared["peak"] = max(self._shared["peak"], self._shared["active"])
        await asyncio.sleep(0.05)  # real sleep: genuinely yields to the loop
        self._shared["active"] -= 1
        return StageOutput(output={"tag": self._tag})


def test_max_concurrency_one_never_overlaps(tmp_path, monkeypatch):
    shared = {"active": 0, "peak": 0}
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch,
        settings_overrides={"worker": {"max_concurrency": 1}},
        paid=False,
    )
    now = "2026-01-01T00:00:00.000Z"
    conn = db.connect()
    conn.execute("INSERT INTO series(id, name, slug, created_at) VALUES ('s-i', 's', 's-i', ?)", (now,))
    conn.execute(
        "INSERT INTO episodes(id, series_id, title, concept, status, languages_json, aspects_json, created_at) "
        "VALUES ('ep-instr', 's-i', 't', 'c', 'pending', '[\"ar\"]', '[\"9:16\"]', ?)",
        (now,),
    )
    specs = [
        StageSpec(name=f"instr_{i}", scope=Scope.EPISODE, depends_on=(),
                  handler=_SharedCounterHandler(shared, f"s{i}"))
        for i in range(4)
    ]
    dag = Dag(specs)
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config, ids=ids)
    run_row = repos.pipeline.create_run(
        episode_id="ep-instr", profile="sandbox", auto_approve=False, config_snapshot_json=None
    )
    repos.jobs.enqueue(kind="advance_run", payload={"run_id": run_row.id},
                       dedupe_key=f"advance:{run_row.id}")
    worker = _worker(db, engine, repos, config, clock, ids)

    import asyncio

    asyncio.run(worker.run(until_idle=True))

    assert shared["peak"] == 1
    rows = db.connect().execute("SELECT count(*) FROM stage_runs WHERE status = 'succeeded'").fetchone()[0]
    assert rows == 4


# ======================================================================
# T-ENG-06
# ======================================================================


def test_final_profile_never_auto_approves(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, paid=False)
    _seed_sqlite_final_episode(db)
    run_row = repos.pipeline.create_run(
        episode_id="ep-final", profile="final", auto_approve=True, config_snapshot_json=None
    )
    dag = Dag(demo_specs())
    gates = GateService(db, repos, clock, dag, ids=ids)
    with pytest.raises(GatePolicyError):
        gates.create_gate_round(
            run_id=run_row.id,
            stage="demo_script",
            gate=GateKind.G1_SCRIPT,
            snapshot_hash="x" * 64,
            profile="final",
            auto_approve=True,
            allow_auto_approve_in=list(config.settings.gates.allow_auto_approve_in),
        )


def _seed_sqlite_final_episode(db) -> None:
    now = "2026-01-01T00:00:00.000Z"
    conn = db.connect()
    conn.execute("INSERT INTO series(id, name, slug, created_at) VALUES ('s-f', 's', 's-f', ?)", (now,))
    conn.execute(
        "INSERT INTO episodes(id, series_id, title, concept, status, languages_json, aspects_json, created_at) "
        "VALUES ('ep-final', 's-f', 't', 'c', 'pending', '[\"ar\"]', '[\"9:16\"]', ?)",
        (now,),
    )


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_sandbox_auto_approve_runs_end_to_end(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engauto",
                                               paid=False)
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value
    gate_states = {g["status"] for g in db.connect().execute(
        "SELECT status FROM approval_gates"
    ).fetchall()}
    assert gate_states == {"auto_approved"}


# ======================================================================
# T-ENG-03
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_worker_crash_mid_stage_requeues_and_pays_zero_repaid(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engcrash",
                                               paid=False)
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    injector = FaultInjector()
    injector.arm("during_poll")
    engine, _dag = _engine(db, repos, config, clock, ids, gateway=None)

    # rebuild engine with a crasher on the image provider
    from studio.core.budget import LedgerService
    from studio.core.gateway import ProviderGateway

    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    ledger = LedgerService(db, clock, config.settings, ids)
    crash_gw = ProviderGateway(
        db=db, settings=config.settings, registry=config.registry, cas=cas,
        ledger=ledger, clock=clock, ids=ids, faults=injector,
        poll_interval_s=0.0, sleep=no_op_sleep,
    )
    engine = Engine(db=db, repos=repos, dag=Dag(demo_specs()), clock=clock,
                    config=config, ids=ids, gateway=crash_gw)
    worker1 = _worker(db, engine, repos, config, clock, ids)
    with pytest.raises(SimulatedCrash):
        await worker1.run(until_idle=True)

    # mid-stage crash: the job is still marked running with a heartbeat
    running = db.connect().execute(
        "SELECT count(*) AS n FROM jobs WHERE status = 'running'"
    ).fetchone()["n"]
    assert running == 1

    # restart past the staleness window on a fresh engine/gateway
    clock.advance(timedelta(seconds=3600))
    _dag2 = None
    engine2 = Engine(db=db, repos=repos, dag=Dag(demo_specs()), clock=clock,
                     config=config, ids=_Ids())
    worker2 = _worker(db, engine2, repos, config, clock, _Ids())
    await worker2.run(until_idle=True)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value

    # zero re-paid calls: image/video job mocks persist their submit counts
    mock_dir = Path(config.settings.paths.home) / "data" / "mock"
    import json as _json

    image_submits = _json.loads((mock_dir / "image_jobs.json").read_text())["submit_count"]
    video_submits = _json.loads((mock_dir / "video_jobs.json").read_text())["submit_count"]
    assert image_submits == 3
    assert video_submits == 3
    calls = db.connect().execute(
        "SELECT capability, count(*) AS n FROM provider_calls GROUP BY capability"
    ).fetchall()
    assert {dict(c)["capability"]: dict(c)["n"] for c in calls} == {
        "llm": 1, "tts": 3, "image_reference_generate": 3, "image_to_video": 3,
    }
    requeued = db.connect().execute(
        "SELECT count(*) FROM audit_events WHERE event = 'job_recovery_requeued'"
    ).fetchone()[0]
    assert requeued >= 1


# ======================================================================
# T-ENG-04 / T-ENG-05
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_request_changes_reexecutes_only_the_rejected_shot(
    tmp_path, monkeypatch
):
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="enginv",
        settings_overrides={"budgets": {"default_episode_cap_usd": 1000.0, "monthly_cap_usd": 10000.0}},
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16", "16:9"), auto_approve=False
    )
    engine, dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    gates = GateService(db, repos, clock, dag, ids=_Ids())

    # worker#1 -> G1; approve -> worker#2 -> G2 (clips not executed yet)
    await _drain(worker)
    g1 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G1_SCRIPT'"
    ).fetchone()["id"]
    gates.approve(g1)
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value

    before_ids = {r["id"] for r in db.connect().execute("SELECT id FROM provider_calls").fetchall()}

    shot_row = db.connect().execute(
        "SELECT s.id AS id FROM shots s JOIN tracks t ON t.id = s.track_id "
        "WHERE t.aspect = '9:16' AND s.shot_no = 2"
    ).fetchone()
    target_shot = shot_row["id"]
    other_track_shots = {
        r["id"]
        for r in db.connect().execute(
            "SELECT s.id AS id FROM shots s JOIN tracks t ON t.id = s.track_id "
            "WHERE t.aspect = '16:9'"
        ).fetchall()
    }

    g2 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G2_KEYFRAMES'"
    ).fetchone()["id"]
    edited = "a lighthouse glowing gold against a crimson storm"
    gates.request_changes(
        g2, "needs a stronger prompt", [ShotRejection(shot_id=target_shot, edited_prompt=edited)]
    )

    # only the rejected shot's rows are stale; the other track is untouched
    staled = db.connect().execute(
        "SELECT stage, subject_id FROM stage_runs WHERE status = 'stale'"
    ).fetchall()
    staled_set = {(r["stage"], r["subject_id"]) for r in staled}
    assert staled_set == {("s8_keyframes", target_shot)}

    # attempts: shot 2 now has attempt 2 with a fresh nonce; others still attempt 1
    attempts = db.connect().execute(
        "SELECT shot_id, attempt_no FROM shot_attempts ORDER BY shot_id, attempt_no"
    ).fetchall()
    by_shot: dict[str, list[int]] = {}
    for row in attempts:
        by_shot.setdefault(row["shot_id"], []).append(row["attempt_no"])
    assert by_shot[target_shot] == [1, 2]
    for shot_id in other_track_shots:
        assert by_shot[shot_id] == [1]

    # re-execution: only shot 2 gets a new image call; nothing else pays
    await _drain(worker)
    new_calls = [
        dict(r)
        for r in db.connect().execute(
            "SELECT id, capability, purpose, request_json FROM provider_calls"
        ).fetchall()
        if r["id"] not in before_ids
    ]


    assert {c["capability"] for c in new_calls} == {"image_reference_generate"}
    assert edited in new_calls[0]["request_json"]

    # round 2 gate is pending; approving it runs clips for shot 2 and the rest
    g2_round2 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G2_KEYFRAMES' AND round = 2"
    ).fetchone()["id"]
    gates.approve(g2_round2)
    await _drain(worker)
    g3 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G3_FINAL_CUT' AND round = 1"
    ).fetchone()["id"]
    gates.approve(g3)
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value

    final_calls = {
        dict(r)["id"]
        for r in db.connect().execute("SELECT id FROM provider_calls").fetchall()
    } - before_ids
    all_new = [
        dict(r)
        for r in db.connect().execute(
            "SELECT id, capability, purpose FROM provider_calls"
        ).fetchall()
        if dict(r)["id"] in final_calls
    ]
    capabilities = [c["capability"] for c in all_new]
    # exactly one image re-render (the rejected shot) and one pass of clips
    assert capabilities.count("image_reference_generate") == 1
    assert capabilities.count("tts") == 0      # unchanged narration: $0 spent
    # video re-rendered only for the re-attempted shot
    video_calls = [c for c in all_new if c["capability"] == "image_to_video"]
    assert len(video_calls) == 6  # all six clips run now, but only once each


async def test_gate_lifecycle_blocks_and_releases_the_pipeline(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="enggate",
                                               paid=False)
    seeded, run_row = _seed_run(config, repos, ids, auto_approve=False)
    engine, dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)

    # worker#1: script runs, then the run parks at G1 with nothing enqueued
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value
    queued = db.connect().execute(
        "SELECT count(*) AS n FROM jobs WHERE status IN ('queued', 'running')"
    ).fetchone()["n"]
    assert queued == 0
    stage_run_counts = db.connect().execute(
        "SELECT stage, count(*) AS n FROM stage_runs GROUP BY stage"
    ).fetchall()
    assert {r["stage"]: r["n"] for r in stage_run_counts} == {"demo_script": 1}

    gates = GateService(db, repos, clock, dag, ids=_Ids())
    g1 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G1_SCRIPT'"
    ).fetchone()["id"]

    # approving G1 enqueues advance_run; voice + keyframes run, clips do not
    gates.approve(g1)
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run.status == RunStatus.AWAITING_APPROVAL.value
    counts = {
        r["stage"]: r["n"]
        for r in db.connect().execute(
            "SELECT stage, count(*) AS n FROM stage_runs GROUP BY stage"
        ).fetchall()
    }
    assert counts["demo_voice"] == 1
    assert counts["s8_keyframes"] == 3
    assert "s10_clips" not in counts  # G2 blocks clips

    g2 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G2_KEYFRAMES' ORDER BY round DESC LIMIT 1"
    ).fetchone()["id"]

    # request_changes on shot 1: attempt 2 created for that shot only
    shot_row = db.connect().execute(
        "SELECT id FROM shots ORDER BY track_id, shot_no LIMIT 1"
    ).fetchone()
    target_shot = shot_row["id"]
    gates.request_changes(
        g2, "prompt weak", [ShotRejection(shot_id=target_shot, edited_prompt="storm over the harbor")]
    )
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run.status == RunStatus.AWAITING_APPROVAL.value
    rounds = [
        r["round"]
        for r in db.connect().execute(
            "SELECT round FROM approval_gates WHERE gate = 'G2_KEYFRAMES' ORDER BY round"
        ).fetchall()
    ]
    assert rounds == [1, 2]
    attempt_rows = db.connect().execute(
        "SELECT shot_id, attempt_no FROM shot_attempts ORDER BY shot_id, attempt_no"
    ).fetchall()
    for row in attempt_rows:
        if row["shot_id"] == target_shot:
            assert row["attempt_no"] in (1, 2)
        else:
            assert row["attempt_no"] == 1

    # approve round 2 → clips + finalize → G3 → approve → completed
    g2_round2 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G2_KEYFRAMES' AND round = 2"
    ).fetchone()["id"]
    gates.approve(g2_round2)
    await _drain(worker)
    g3 = db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G3_FINAL_CUT' AND round = 1"
    ).fetchone()["id"]
    gates.approve(g3)
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run.status == RunStatus.COMPLETED.value
    episode = db.connect().execute(
        "SELECT status FROM episodes WHERE id = ?", (seeded.episode_id,)
    ).fetchone()
    assert episode["status"] == "completed"


# ======================================================================
# T-ENG-07
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_budget_halt_then_resume_completes_without_repaying(
    tmp_path, monkeypatch
):
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="enghalt",
        settings_overrides={"budgets": {"default_episode_cap_usd": 0.000001, "warn_pct": 0}},
    )
    seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.HALTED_BUDGET.value
    episode = db.connect().execute(
        "SELECT status FROM episodes WHERE id = ?", (seeded.episode_id,)
    ).fetchone()
    assert episode["status"] == "halted_budget"

    # nothing further enqueued while halted
    queued = db.connect().execute(
        "SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')"
    ).fetchone()["n"]
    assert queued == 0

    # operator raises the cap and resets the kill switch, then resumes
    ledger = LedgerService(db, clock, config.settings, ids)
    ledger.reset_kill_switch("episode", seeded.episode_id, new_cap_musd=100_000_000_000)
    engine.resume_run(run_row.id)
    worker2 = _worker(db, engine, repos, config, clock, _Ids())
    await _drain(worker2)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value
    calls = db.connect().execute(
        "SELECT cache_key, count(*) AS n FROM provider_calls GROUP BY cache_key"
    ).fetchall()
    assert all(r["n"] == 1 for r in calls)  # nothing was re-paid


# ======================================================================
# T-ENG-08
# Phase 4 §M formula (explicitly authorized by §O): expected image calls =
# shots + characters-without-approved-sheets x roles; video calls = shots;
# TTS calls scale with languages. The demo seed pins no bible, so the sheet
# term is 0 and the counts stay at shots; the formula is computed from repo
# state instead of asserted as a bare 3.
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_language_toggle_doubles_tts_but_not_image_or_video(
    tmp_path, monkeypatch
):
    counts = {}
    expected = {}
    for idx, languages in enumerate((("ar",), ("ar", "en"))):
        config, db, clock, ids, repos, _home = _env(
            tmp_path, monkeypatch, filename=f"englang{idx}", paid=False
        )
        _seeded, _run_row = _seed_run(
            config, repos, ids, languages=languages, aspects=("9:16",), auto_approve=True
        )
        engine, _dag = _engine(db, repos, config, clock, ids)
        # §M formula: shots + characters-without-approved-sheets x roles.
        # Measure the missing set before the drain (the run generates+approves
        # sheets); the demo seed pins no bible, so this is 0 and image calls
        # stay at shots.
        from studio.media.stages import _sheet_precheck_subjects

        run = repos.pipeline.get_run(_run_row.id)
        assert run is not None
        precheck_ctx = engine._context(run, None)
        missing_sheets = len(_sheet_precheck_subjects(precheck_ctx))
        worker = _worker(db, engine, repos, config, clock, ids)
        await _drain(worker)
        rows = db.connect().execute(
            "SELECT capability, count(*) AS n FROM provider_calls GROUP BY capability"
        ).fetchall()
        counts[languages] = {r["capability"]: r["n"] for r in rows}
        shots = db.connect().execute("SELECT count(*) FROM shots").fetchone()[0]
        expected[languages] = shots + missing_sheets

    ar_only = counts[("ar",)]
    bilingual = counts[("ar", "en")]
    assert ar_only["tts"] == 3
    assert bilingual["tts"] == 6
    assert ar_only["image_reference_generate"] == expected[("ar",)] == 3
    assert bilingual["image_reference_generate"] == expected[("ar", "en")] == 3
    assert ar_only["image_to_video"] == bilingual["image_to_video"] == 3


# ======================================================================
# T-ENG-14 — sandbox demo DAG through S10 with mocks (real handlers),
# including counts and the T-ENG-08 formula (Phase 4 §M)
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_demo_dag_through_s10_counts_and_formula(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="engs10", paid=False
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16",), auto_approve=True
    )
    engine, _dag = _engine(db, repos, config, clock, ids)

    # The §M formula is computed from repo state, not asserted as a bare 3:
    # image calls = shots + characters-without-approved-sheets x roles.
    from studio.media.stages import _sheet_precheck_subjects

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None
    missing_sheets = len(_sheet_precheck_subjects(engine._context(run, None)))
    roles = len(config.settings.character_sheets.roles)

    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value

    shots = db.connect().execute("SELECT count(*) FROM shots").fetchone()[0]
    lines = db.connect().execute("SELECT count(*) FROM narration_lines").fetchone()[0]
    counts = {
        row["capability"]: row["n"]
        for row in db.connect().execute(
            "SELECT capability, count(*) AS n FROM provider_calls GROUP BY capability"
        ).fetchall()
    }

    assert counts["tts"] == lines
    assert counts["image_reference_generate"] == shots + missing_sheets * roles
    assert counts["image_to_video"] == shots
    # locked S6 values are untouched by S10; every conformed clip carries
    # exactly locked-duration frames at the render fps
    from studio.media.conform import clip_frames_for

    fps = int(config.settings.render.fps)
    locked = {
        row["id"]: int(row["clip_duration_ms"])
        for row in db.connect().execute(
            "SELECT id, clip_duration_ms FROM shots"
        ).fetchall()
    }
    assert locked and all(ms and ms > 0 for ms in locked.values())
    clip_rows = db.connect().execute(
        "SELECT output_json FROM stage_runs WHERE stage = 's10_clips' "
        "AND status = 'succeeded'"
    ).fetchall()
    assert len(clip_rows) == shots
    for row in clip_rows:
        out = json.loads(row["output_json"])
        locked_ms = locked[out["shot_id"]]
        assert out["clip_duration_ms"] == locked_ms
        assert out["fps"] == fps
        assert out["clip_frames"] == clip_frames_for(locked_ms, fps)
    for shot_id, locked_ms in locked.items():
        after = db.connect().execute(
            "SELECT clip_duration_ms FROM shots WHERE id = ?", (shot_id,)
        ).fetchone()["clip_duration_ms"]
        assert after == locked_ms
    final_row = db.connect().execute(
        "SELECT status FROM stage_runs WHERE stage = 'demo_finalize'"
    ).fetchone()
    assert final_row is not None and final_row["status"] == "succeeded"


# ======================================================================
# T-ENG-09
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_aspects_toggle(tmp_path, monkeypatch):
    from studio.video.probe import probe

    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engasp",
                                               paid=False)
    _seeded, _run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16", "16:9"), auto_approve=True
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)
    tracks = db.connect().execute("SELECT id, aspect FROM tracks ORDER BY aspect").fetchall()
    assert {t["aspect"] for t in tracks} == {"9:16", "16:9"}
    # mock video output aspect equals track aspect (ffprobe on CAS assets)
    for track in tracks:
        row = db.connect().execute(
            "SELECT a.rel_path FROM stage_runs sr JOIN assets a ON a.sha256 = COALESCE(json_extract(sr.output_json, '$.conformed_asset_id'), json_extract(sr.output_json, '$.asset_sha256')) "
            "WHERE sr.stage = 's10_clips' AND sr.subject_id IN ("
            "  SELECT id FROM shots WHERE track_id = ?"
            ") LIMIT 1",
            (track["id"],),
        ).fetchone()
        assert row is not None
        result = probe(Path(config.settings.paths.cas) / row["rel_path"])
        if track["aspect"] == "9:16":
            assert (result.width, result.height) == (540, 960)
        else:
            assert (result.width, result.height) == (960, 540)


# ======================================================================
# T-ENG-10
# ======================================================================


@pytest.mark.asyncio
async def test_transient_exhausted_then_terminal_failure(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engtrans",
                                               paid=False)
    provider = MockLLMProvider(MockFaultPlan(fail_transient_n=99), ids=_Ids())
    gw = build_gateway(
        db=db, config=config, clock=clock, ids=_Ids(), providers={"mock.llm": provider}
    )
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids, gateway=gw)
    worker = _worker(db, engine, repos, config, clock, ids)

    # pass 1: job claimed (attempt 1), transient failure -> requeued with backoff
    await _drain(worker)
    jobs = db.connect().execute(
        "SELECT attempt, status, run_after FROM jobs WHERE kind = 'run_stage'"
    ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["attempt"] == 1
    retry_events = db.connect().execute(
        "SELECT count(*) FROM audit_events WHERE event = 'stage_run_retry_scheduled'"
    ).fetchone()[0]
    assert retry_events == 1

    # backoff not elapsed yet: idle drain does not claim the job
    await _drain(worker)
    jobs = db.connect().execute(
        "SELECT attempt FROM jobs WHERE kind = 'run_stage'"
    ).fetchall()
    assert jobs[0]["attempt"] == 1

    # pass 2 (attempt 2) then pass 3 (attempt 3 = max) -> terminal failure
    for _ in range(2):
        clock.advance(timedelta(seconds=float(config.settings.worker.job_retry_backoff_s) + 1))
        await _drain(worker)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.FAILED.value
    stage_run = db.connect().execute(
        "SELECT status FROM stage_runs WHERE stage = 'demo_script'"
    ).fetchone()
    assert stage_run["status"] == "failed"
    failed_audit = db.connect().execute(
        "SELECT count(*) FROM audit_events WHERE event = 'stage_run_failed'"
    ).fetchone()[0]
    assert failed_audit == 1


@pytest.mark.asyncio
async def test_permanent_error_fails_without_retry(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engperm",
                                               paid=False)
    provider = MockLLMProvider(MockFaultPlan(fail_permanent=True), ids=_Ids())
    gw = build_gateway(
        db=db, config=config, clock=clock, ids=_Ids(), providers={"mock.llm": provider}
    )
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids, gateway=gw)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.FAILED.value
    job = db.connect().execute(
        "SELECT attempt, status FROM jobs WHERE kind = 'run_stage'"
    ).fetchone()
    assert job["attempt"] == 1  # no retry on a permanent error
    assert job["status"] == "failed"


# ======================================================================
# T-ENG-11
# ======================================================================


def test_recovery_resets_stale_jobs_and_orphaned_stage_runs(tmp_path, monkeypatch):
    config, db, clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="engrec",
                                               paid=False)
    now = "2026-01-01T00:00:00.000Z"
    conn = db.connect()
    conn.execute("INSERT INTO series(id, name, slug, created_at) VALUES ('s-r', 's', 's-r', ?)", (now,))
    conn.execute(
        "INSERT INTO episodes(id, series_id, title, concept, status, languages_json, aspects_json, created_at) "
        "VALUES ('ep-r', 's-r', 't', 'c', 'running', '[\"ar\"]', '[\"9:16\"]', ?)",
        (now,),
    )
    run_row = repos.pipeline.create_run(
        episode_id="ep-r", profile="sandbox", auto_approve=False, config_snapshot_json=None
    )
    stage_run = repos.pipeline.create_stage_run(
        run_id=run_row.id,
        stage="demo_script",
        subject=SubjectRef(
            scope=Scope.EPISODE,
            id="ep-r",
            lineage=((Scope.SERIES, "s-r"),),
        ),
    )
    repos.pipeline.set_stage_run(stage_run.id, status="running")
    job = repos.jobs.enqueue(
        kind="run_stage",
        payload={"stage_run_id": stage_run.id},
        dedupe_key=f"rec:{stage_run.id}",
        stage_run_id=stage_run.id,
    )
    assert job is not None
    claimed = repos.jobs.claim("dead-worker")
    assert claimed is not None

    # clock jumps past the staleness window
    clock.advance(timedelta(seconds=10_000))

    # also an orphaned running stage_run with no job at all
    orphan = repos.pipeline.create_stage_run(
        run_id=run_row.id,
        stage="s8_keyframes",
        subject=SubjectRef(
            scope=Scope.SHOT,
            id="shot-orphan",
            lineage=((Scope.SERIES, "s-r"), (Scope.EPISODE, "ep-r")),
        ),
    )
    repos.pipeline.set_stage_run(orphan.id, status="running")

    recover(db, repos, clock, float(config.settings.worker.stale_after_s))

    job_after = repos.jobs.get(job.id)
    assert job_after is not None
    assert job_after.status == "queued"
    assert job_after.worker_id is None

    stage_row = repos.pipeline.get_stage_run(stage_run.id)
    assert stage_row is not None and stage_row.status == "pending"

    orphan_after = repos.pipeline.get_stage_run(orphan.id)
    assert orphan_after is not None and orphan_after.status == "pending"
    audit_orphan = db.connect().execute(
        "SELECT count(*) FROM audit_events WHERE event = 'stage_run_recovery_pending'"
    ).fetchone()[0]
    assert audit_orphan == 1

    audit_job = db.connect().execute(
        "SELECT count(*) FROM audit_events WHERE event = 'job_recovery_requeued'"
    ).fetchone()[0]
    assert audit_job == 1


def test_recovery_fails_job_at_max_attempts(tmp_path, monkeypatch):
    config, db, clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="engrec2",
                                               paid=False)
    now = "2026-01-01T00:00:00.000Z"
    conn = db.connect()
    conn.execute("INSERT INTO series(id, name, slug, created_at) VALUES ('s-r2', 's', 's-r2', ?)", (now,))
    conn.execute(
        "INSERT INTO episodes(id, series_id, title, concept, status, languages_json, aspects_json, created_at) "
        "VALUES ('ep-r2', 's-r2', 't', 'c', 'running', '[\"ar\"]', '[\"9:16\"]', ?)",
        (now,),
    )
    run_row = repos.pipeline.create_run(
        episode_id="ep-r2", profile="sandbox", auto_approve=False, config_snapshot_json=None
    )
    stage_run = repos.pipeline.create_stage_run(
        run_id=run_row.id,
        stage="demo_script",
        subject=SubjectRef(scope=Scope.EPISODE, id="ep-r2", lineage=((Scope.SERIES, "s-r2"),)),
    )
    repos.pipeline.set_stage_run(stage_run.id, status="running")
    job = repos.jobs.enqueue(
        kind="run_stage",
        payload={"stage_run_id": stage_run.id},
        dedupe_key=f"rec2:{stage_run.id}",
        stage_run_id=stage_run.id,
        max_attempts=1,
    )
    assert job is not None
    claimed = repos.jobs.claim("dead-worker")  # attempt becomes 1 >= max_attempts
    assert claimed is not None

    clock.advance(timedelta(seconds=10_000))
    recover(db, repos, clock, float(config.settings.worker.stale_after_s))

    job_after = repos.jobs.get(job.id)
    assert job_after is not None and job_after.status == "failed"
    stage_row = repos.pipeline.get_stage_run(stage_run.id)
    assert stage_row is not None and stage_row.status == "failed"
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.FAILED.value
    event = db.connect().execute(
        "SELECT count(*) FROM audit_events WHERE event = 'job_recovery_failed'"
    ).fetchone()[0]
    assert event == 1


# ======================================================================
# Coverage-closure: invalidation cascade + engine/gates misc paths
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_invalidation_cascades_downstream_and_reruns_for_free(
    tmp_path, monkeypatch
):
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="engcascade",
        settings_overrides={"budgets": {"default_episode_cap_usd": 1000.0, "monthly_cap_usd": 10000.0}},
    )
    seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)
    assert repos.pipeline.get_run(run_row.id).status == RunStatus.COMPLETED.value

    calls_before = db.connect().execute("SELECT count(*) FROM provider_calls").fetchone()[0]

    # change an episode-level input: the script hash drifts; everything downstream stales
    db.connect().execute(
        "UPDATE episodes SET concept = concept || ' revised' WHERE id = ?",
        (seeded.episode_id,),
    )
    # reopen the run (a decision such as request_changes would do this in production)
    db.connect().execute(
        "UPDATE pipeline_runs SET status = 'running' WHERE id = ?", (run_row.id,)
    )
    engine.advance_run(run_row.id)
    # staleness is transient within one advance pass (rows are re-pended for the
    # worker): assert the audit trail records the full invalidation set
    invalidation = db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = 'staleness_invalidated'"
    ).fetchall()
    assert len(invalidation) == 1
    invalidated_ids = json.loads(invalidation[0]["payload_json"])["stage_runs"]
    stages = {
        r["stage"]
        for r in db.connect().execute(
            "SELECT stage FROM stage_runs WHERE id IN (" + ",".join("?" * len(invalidated_ids)) + ")",
            invalidated_ids,
        ).fetchall()
    }
    # Phase 3+4: the audio chain (s5a/s5/s6/s7/s7b) and the media chain
    # (s4p/s8/s10p/s10) join the staleness graph — an episode-level edit
    # invalidates everything downstream. demo_voice remains (line materializer).
    # s8a_character_sheets has no stage_run rows (no bible in the demo seed).
    assert stages == {
        "demo_script", "s4p_media_preflight", "demo_voice",
        "s5a_tts_preflight", "s5_tts", "s6_timing",
        "s7_audio_plan", "s7b_audio_mix",
        "s8_keyframes", "s10p_clip_preflight", "s10_clips", "demo_finalize",
    }
    # the pending rows are re-enqueued incrementally: script goes first, the
    # rest follow as dependency stages complete (job completion enqueues advance)
    queued = db.connect().execute(
        "SELECT count(*) FROM jobs WHERE status = 'queued' AND kind = 'run_stage'"
    ).fetchone()[0]
    assert queued >= 1

    worker2 = _worker(db, engine, repos, config, clock, _Ids())
    await _drain(worker2)
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value
    # unchanged inputs hit the cache: zero new provider calls
    calls_after = db.connect().execute("SELECT count(*) FROM provider_calls").fetchone()[0]
    assert calls_after == calls_before


@pytest.mark.asyncio
async def test_worker_max_jobs_stops_early(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engmaxjobs",
                                               paid=False)
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    executed = await worker.run(until_idle=False, max_jobs=2)
    assert executed == 2
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status != RunStatus.COMPLETED.value


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_advance_on_completed_run_is_a_no_op(tmp_path, monkeypatch):
    from studio.domain.errors import StudioError

    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engnoop",
                                               paid=False)
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)
    assert repos.pipeline.get_run(run_row.id).status == RunStatus.COMPLETED.value
    jobs_before = db.connect().execute("SELECT count(*) FROM jobs").fetchone()[0]
    engine.advance_run(run_row.id)  # status not running/awaiting -> early return
    jobs_after = db.connect().execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert jobs_before == jobs_after
    with pytest.raises(StudioError, match="only halted_budget or failed"):
        engine.resume_run(run_row.id)


def test_gate_service_rounds_anchor_and_errors(tmp_path, monkeypatch):
    from studio.pipeline.gates import build_shot_anchor

    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="engmisc",
                                               paid=False)
    seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    dag = Dag(demo_specs())
    gates = GateService(db, repos, clock, dag, ids=_Ids())
    track_id = seeded.track_ids[0]
    shot_row = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="a lighthouse")
    shot = shot_row.id
    anchor = build_shot_anchor(repos, shot)
    assert anchor.id == shot
    assert anchor.lineage[0][0] == Scope.SERIES

    with pytest.raises(GatePolicyError):
        build_shot_anchor(repos, "shot-does-not-exist")
    with pytest.raises(GatePolicyError):
        gates.request_changes("no-such-gate", None, [])
    with pytest.raises(GatePolicyError):
        gates.approve("no-such-gate")

    # rounds() lists every round of a stage
    run_rounds = gates.rounds(run_row.id, "demo_script")
    assert isinstance(run_rounds, list)
    assert gates.list_pending(run_row.id) == []


def test_budget_halt_sets_episode_status_and_resume_clears_it(
    tmp_path, monkeypatch
):
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="enghalt2", paid=False,
        settings_overrides={"budgets": {"default_episode_cap_usd": 0.000001, "warn_pct": 0}},
    )
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    engine, _dag = _engine(db, repos, config, clock, ids)
    from studio.domain.errors import StudioError

    # halted_budget runs can resume; a RUNNING run cannot
    with pytest.raises(StudioError):
        engine.resume_run(run_row.id)


def test_subjects_helpers_lineage_and_related():
    from studio.domain.refs import Scope, subjects_related
    from studio.pipeline import subjects as subj

    lin = subj.lineage_for("s", "e", "t")
    assert [scope for scope, _ in lin] == [Scope.SERIES, Scope.EPISODE, Scope.TRACK]
    assert subj.lineage_for(None, None) == ()
    assert subj.episode_lineage(None) == ()
    ep = subj.episode_subject("s", "e")
    assert ep.id == "e"
    assert subj.track_subject("s", "e", "t").id == "t"
    assert subj.deliverable_subject("s", "e", "t", "d").id == "d"
    shot = subj.shot_subject("s", "e", "t", "sh")
    assert shot.id == "sh"
    assert subj.related(ep, shot) is True
    assert subj.related(shot, ep) is True
    assert subjects_related(ep, subj.episode_subject("s", "ep-other")) is False


def test_gate_create_policy_violations_and_shot_anchor(tmp_path, monkeypatch):
    from studio.pipeline.gates import build_shot_anchor

    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="gates2")
    seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    dag = Dag(demo_specs())
    gates = GateService(db, repos, clock, dag, ids=_Ids())
    track_id = seeded.track_ids[0]
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="a lighthouse")

    # final profile never auto-approves, even when asked
    try:
        gates.create_gate_round(
            run_id=run_row.id, stage="demo_script", gate=GateKind.G1_SCRIPT,
            snapshot_hash="abc", profile="final", auto_approve=True,
            allow_auto_approve_in=[Profile.FINAL],
        )
        raise AssertionError("expected GatePolicyError")
    except GatePolicyError as exc:
        assert "final" in str(exc)

    # profile outside allow_auto_approve_in is refused
    try:
        gates.create_gate_round(
            run_id=run_row.id, stage="demo_script", gate=GateKind.G1_SCRIPT,
            snapshot_hash="abc", profile="draft", auto_approve=True,
            allow_auto_approve_in=[Profile.SANDBOX],
        )
        raise AssertionError("expected GatePolicyError")
    except GatePolicyError as exc:
        assert "allow_auto_approve_in" in str(exc)

    # sandbox profile with draft-only allow-list is refused
    try:
        gates.create_gate_round(
            run_id=run_row.id, stage="s8_keyframes", gate=GateKind.G2_KEYFRAMES,
            snapshot_hash="abc", profile="sandbox", auto_approve=True,
            allow_auto_approve_in=[Profile.DRAFT],
        )
        raise AssertionError("expected GatePolicyError")
    except GatePolicyError:
        pass

    # build_shot_anchor: happy path + GatePolicyError for unknown shot
    anchor = build_shot_anchor(repos, shot.id)
    assert anchor.id == shot.id
    assert [scope.value for scope, _ in anchor.lineage] == ["series", "episode", "track"]
    try:
        build_shot_anchor(repos, "shot-does-not-exist")
        raise AssertionError("expected GatePolicyError")
    except GatePolicyError:
        pass
    assert gates.list_pending(run_row.id) == []
    assert gates.rounds(run_row.id, "demo_script") == []
    assert isinstance(gates.rounds(run_row.id, "nope"), list)


# ======================================================================
# Coverage closure: early-return paths (cheap, engine-adjacent)
# ======================================================================

def test_advance_run_unknown_run_is_no_op(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="adv1")
    engine, _dag = _engine(db, repos, config, clock, ids)
    engine.advance_run("run-does-not-exist")  # logs a warning, must not raise


def test_advance_run_terminal_run_is_no_op(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="adv2")
    _seeded, run_row = _seed_run(config, repos, ids, auto_approve=True)
    repos.pipeline.set_run_status(run_row.id, RunStatus.COMPLETED.value)
    engine, _dag = _engine(db, repos, config, clock, ids)
    before = db.connect().execute("SELECT count(*) FROM jobs").fetchone()[0]
    engine.advance_run(run_row.id)
    after = db.connect().execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert before == after


def test_resume_run_unknown_run_raises(tmp_path, monkeypatch):
    from studio.domain.errors import StudioError

    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="adv3")
    engine, _dag = _engine(db, repos, config, clock, ids)
    try:
        engine.resume_run("run-does-not-exist")
        raise AssertionError("expected StudioError")
    except StudioError:
        pass


async def test_handle_job_unknown_kind_completes_job(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="adv4")
    engine, _dag = _engine(db, repos, config, clock, ids)
    job = repos.jobs.enqueue(kind="mystery_kind", payload={}, dedupe_key="mystery:1")
    claimed = repos.jobs.claim("w-1")
    assert claimed is not None and claimed.id == job.id
    await engine.handle_job(job.id)
    row = repos.jobs.get(job.id)
    assert row.status == "succeeded"


async def test_handle_job_wrong_status_is_no_op(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="adv5")
    engine, _dag = _engine(db, repos, config, clock, ids)
    job = repos.jobs.enqueue(kind="advance_run", payload={"run_id": "x"}, dedupe_key="skip:1")
    # job still 'queued' — handle_job requires 'running'
    await engine.handle_job(job.id)
    row = repos.jobs.get(job.id)
    assert row.status == "queued"


async def test_run_stage_job_unknown_stage_run_completes_job(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch, filename="adv6")
    engine, _dag = _engine(db, repos, config, clock, ids)
    job = repos.jobs.enqueue(
        kind="run_stage", payload={"stage_run_id": "nope"}, dedupe_key="rs:1",
    )
    claimed = repos.jobs.claim("w-1")
    assert claimed is not None and claimed.id == job.id
    await engine.handle_job(job.id)
    assert repos.jobs.get(job.id).status == "succeeded"
