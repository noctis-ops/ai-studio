"""T-GATE-01 (PHASE4 §P hardening): paid stage + pending ancestor gate.

A paid stage (S5 / S8a / S8 / S10) whose ancestor gate is pending must make
ZERO provider calls and ZERO ledger reservations when its job is claimed —
the H0 barrier fires before any handler runs. The same holds after a gate
was approved and the ancestor output then changed (snapshot mismatch). The
gateway-level guard is the second lock: a direct non-cached TTS/image/video
call without a cleared context raises GateNotCleared.

Fully offline: sandbox profile + mock providers; nothing is ever sent.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest
from conftest import PAID_MOCK_PRICES, make_home

from studio.config.loader import load_all
from studio.core.budget import LedgerService
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.core.gateway import CallContext, ProviderGateway
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import Aspect, Language, Scope
from studio.domain.errors import GateNotCleared
from studio.domain.refs import SubjectRef
from studio.pipeline.barrier import stage_outputs_snapshot_hash
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService
from studio.pipeline.specs import production_specs
from studio.pipeline.worker import Worker
from studio.providers.base import MediaRequest, TTSRequest


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"gate-{next(self._counter):06d}"


def _env(tmp_path, monkeypatch, *, filename="gate"):
    home = make_home(tmp_path, registry_overrides=PAID_MOCK_PRICES, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = _Ids()
    repos = build_repos(db, clock, ids)
    return config, db, clock, ids, repos, home


def _engine_worker(config, db, repos, clock, ids):
    dag = Dag(production_specs())
    gateway = ProviderGateway(
        db=db,
        settings=config.settings,
        registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock,
        ids=ids,
        poll_interval_s=0.0,
        sleep=no_op_sleep,
    )
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config,
                    ids=ids, gateway=gateway)
    worker = Worker(db, engine, repos, config, clock,
                    worker_id="gate-test", sleep=no_op_sleep)
    return dag, gateway, worker


def _episode_subject() -> SubjectRef:
    return SubjectRef(
        scope=Scope.EPISODE,
        id="ep-gate-test",
        lineage=((Scope.EPISODE, "ep-gate-test"),),
    )


def _mark_ancestors_succeeded(repos, dag: Dag, run_id: str, paid_stage: str) -> None:
    for ancestor in dag.ancestors(paid_stage):
        spec = dag.spec(ancestor)
        row = repos.pipeline.create_stage_run(
            run_id=run_id,
            stage=ancestor,
            subject=SubjectRef(
                scope=spec.scope,
                id=f"subj-{ancestor}",
                lineage=((Scope.EPISODE, "ep-gate-test"),),
            ),
        )
        repos.pipeline.set_stage_run(
            row.id,
            status="succeeded",
            output_json=json.dumps({"stage": ancestor, "ok": True}),
        )


def _create_pending_gates(repos, db, clock, ids, dag: Dag, run_id: str,
                          paid_stage: str) -> list[str]:
    """One pending round per gated ancestor of the paid stage."""
    service = GateService(db, repos, clock, dag, ids)
    gates: list[str] = []
    for ancestor in dag.ancestors(paid_stage):
        spec = dag.spec(ancestor)
        if spec.gate_after is None:
            continue
        succeeded = [
            row
            for row in repos.pipeline.list_stage_runs(run_id, stage=ancestor)
            if row.status == "succeeded"
        ]
        snapshot = stage_outputs_snapshot_hash(succeeded)
        gate, auto_approved = service.create_gate_round(
            run_id=run_id,
            stage=ancestor,
            gate=spec.gate_after,
            snapshot_hash=snapshot,
            profile="sandbox",
            auto_approve=False,
            allow_auto_approve_in=[],
        )
        assert not auto_approved
        gates.append(gate.id)
    return gates


def _claim_paid_stage(repos, dag: Dag, run_id: str, paid_stage: str) -> str:
    spec = dag.spec(paid_stage)
    row = repos.pipeline.create_stage_run(
        run_id=run_id,
        stage=paid_stage,
        subject=SubjectRef(
            scope=spec.scope,
            id="paid-subject",
            lineage=((Scope.EPISODE, "ep-gate-test"),),
        ),
    )
    repos.jobs.enqueue(
        kind="run_stage",
        payload={"stage_run_id": row.id},
        dedupe_key=f"test-paid:{row.id}",
    )
    return row.id


def _ledger_counts(db):
    calls = db.connect().execute("SELECT count(*) AS n FROM provider_calls").fetchone()["n"]
    reservations = db.connect().execute(
        "SELECT count(*) AS n FROM cost_ledger WHERE entry_type = 'reserve'"
    ).fetchone()["n"]
    return int(calls), int(reservations)


def _deferred_audits(db):
    rows = db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = 'paid_stage_deferred'"
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


PAID_STAGES = ["s5_tts", "s8a_character_sheets", "s8_keyframes", "s10_clips"]


@pytest.mark.asyncio
@pytest.mark.parametrize("paid_stage", PAID_STAGES)
async def test_g01_pending_ancestor_gate_means_zero_spend(
    tmp_path, monkeypatch, paid_stage
):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch,
                                                filename=f"gate_{paid_stage}")
    dag, _gateway, worker = _engine_worker(config, db, repos, clock, ids)
    run = repos.pipeline.create_run(
        episode_id=None, profile="sandbox", auto_approve=False,
        config_snapshot_json=None,
    )
    _mark_ancestors_succeeded(repos, dag, run.id, paid_stage)
    gate_ids = _create_pending_gates(repos, db, clock, ids, dag, run.id, paid_stage)
    assert gate_ids, "expected at least one gated ancestor"
    stage_run_id = _claim_paid_stage(repos, dag, run.id, paid_stage)

    await worker.run(until_idle=True)

    row = repos.pipeline.get_stage_run(stage_run_id)
    assert row is not None and row.status == "pending"
    calls, reservations = _ledger_counts(db)
    assert calls == 0
    assert reservations == 0
    deferred = _deferred_audits(db)
    assert len(deferred) == 1
    assert deferred[0]["stage"] == paid_stage
    assert deferred[0]["problems"]


@pytest.mark.asyncio
@pytest.mark.parametrize("paid_stage", PAID_STAGES)
async def test_g01_approved_then_ancestor_changed_blocks_again(
    tmp_path, monkeypatch, paid_stage
):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch,
                                                filename=f"gate_snap_{paid_stage}")
    dag, _gateway, worker = _engine_worker(config, db, repos, clock, ids)
    run = repos.pipeline.create_run(
        episode_id=None, profile="sandbox", auto_approve=False,
        config_snapshot_json=None,
    )
    _mark_ancestors_succeeded(repos, dag, run.id, paid_stage)
    _create_pending_gates(repos, db, clock, ids, dag, run.id, paid_stage)

    # Approve every pending gate of this run.
    service = GateService(db, repos, clock, dag, ids)
    for gate in repos.gates.list_pending(run.id):
        service.approve(gate.id, "looks right")

    # ...and now a *gated* ancestor's succeeded output changes underneath us.
    gated = sorted(
        a for a in dag.ancestors(paid_stage) if dag.spec(a).gate_after is not None
    )
    assert gated
    ancestor = gated[0]
    rows = [
        r for r in repos.pipeline.list_stage_runs(run.id, stage=ancestor)
        if r.status == "succeeded"
    ]
    assert rows
    repos.pipeline.set_stage_run(
        rows[0].id, output_json=json.dumps({"stage": ancestor, "ok": False, "changed": True})
    )

    # Barrier-level snapshot mismatch (pure, before any advance_run job can
    # re-gate the changed ancestor into a fresh pending round): the approval
    # no longer describes the current output, so the paid stage is blocked.
    from studio.pipeline.barrier import ancestor_gate_problems

    problems = ancestor_gate_problems(repos=repos, dag=dag, run_id=run.id,
                                      stage_id=paid_stage)
    assert any("stale" in problem for problem in problems)

    stage_run_id = _claim_paid_stage(repos, dag, run.id, paid_stage)
    await worker.run(until_idle=True)

    row = repos.pipeline.get_stage_run(stage_run_id)
    assert row is not None and row.status == "pending"
    calls, reservations = _ledger_counts(db)
    assert calls == 0
    assert reservations == 0
    deferred = _deferred_audits(db)
    assert len(deferred) == 1
    problems = deferred[0]["problems"]
    assert any("stale" in p or "pending" in p for p in problems)


@pytest.mark.asyncio
async def test_g01_gateway_level_guard_rejects_direct_media_calls(
    tmp_path, monkeypatch
):
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch,
                                                filename="gate_gateway")
    _dag, gateway, _worker = _engine_worker(config, db, repos, clock, ids)
    ctx = CallContext(
        episode_id=None,
        stage_run_id=None,
        nonce="gate01",
        purpose="gate-01-direct",
        gate_cleared=False,
    )
    media = MediaRequest(
        capability="image_reference_generate",
        params={"prompt": "guard check"},
        aspect=Aspect.TALL,
        seed=1,
    )
    tts = TTSRequest(text="مرحبا", voice_id="v1", language=Language.AR)
    video = MediaRequest(
        capability="image_to_video",
        params={"prompt": "guard check"},
        aspect=Aspect.TALL,
        duration_s=5.0,
        seed=1,
    )
    with pytest.raises(GateNotCleared):
        await gateway.tts("mock.tts", tts, ctx)
    with pytest.raises(GateNotCleared):
        await gateway.image("mock.image", media, ctx)
    with pytest.raises(GateNotCleared):
        await gateway.video("mock.video", video, ctx)
    calls, reservations = _ledger_counts(db)
    assert calls == 0
    assert reservations == 0


def test_g01_gateway_guard_still_allows_exempt_context(tmp_path, monkeypatch):
    """The guard's bypass: an explicit, audited gate exemption gets past the
    barrier (mock provider succeeds) and writes a gate_exempt_call audit."""
    config, db, clock, ids, repos, _home = _env(tmp_path, monkeypatch,
                                                filename="gate_exempt")
    _dag, gateway, _worker = _engine_worker(config, db, repos, clock, ids)
    ctx = CallContext(
        episode_id=None,
        stage_run_id=None,
        nonce="gate01-exempt",
        purpose="gate-01-exempt",
        gate_exempt="audition",
    )

    async def call():
        return await gateway.tts(
            "mock.tts",
            TTSRequest(text="مرحبا", voice_id="v1", language=Language.AR),
            ctx,
        )

    result = asyncio.run(call())  # must not raise
    assert result is not None
    audits = db.connect().execute(
        "SELECT count(*) AS n FROM audit_events WHERE event = 'gate_exempt_call'"
    ).fetchone()["n"]
    assert int(audits) == 1
