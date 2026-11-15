"""T-REC-01: the crash-recovery matrix (BEGIN_PROMPT section I).

For each crash point and each job-based mock provider, arm the point, run the
call, catch `SimulatedCrash`, rebuild Gateway/Ledger/DB handles on the same
files, repeat the identical call, and assert the zero-repay invariants.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from conftest import PAID_MOCK_PRICES, build_gateway, make_home

from studio.config.loader import load_all
from studio.core.budget import LedgerService
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock
from studio.core.faults import CRASH_POINTS, FaultInjector, SimulatedCrash
from studio.db import Database, migrate
from studio.domain.enums import Aspect
from studio.providers.base import MediaRequest
from studio.providers.mock.image import MockImageProvider
from studio.providers.mock.video import MockVideoProvider

# expected submit_count after resume, per crash point
_EXPECTED_SUBMITS = {
    "after_reserve": 1,
    "before_submit": 1,
    "after_submit_before_persist": 2,  # one provider-side orphan
    "after_persist_id": 1,
    "during_poll": 1,
    "after_fetch_before_store": 1,
    "after_store_before_complete": 1,
}


class _Ids:
    """Deterministic ids; the counter is class-level so fresh handles built on
    the same DB never reuse an id."""

    _counter = itertools.count(1)

    def new(self) -> str:
        return f"rec-{next(self._counter):06d}"


def _image(tmp_path, ids, plan=None):
    return MockImageProvider(
        ledger_path=tmp_path / "media_jobs.json",
        width_16x9=160,
        height_16x9=90,
        fault_plan=plan,
        ids=ids,
        poll_ticks=1,
    )


def _video(tmp_path, ids, plan=None):
    return MockVideoProvider(
        ledger_path=tmp_path / "media_jobs.json",
        width_16x9=160,
        height_16x9=90,
        fps=24,
        fault_plan=plan,
        ids=ids,
        poll_ticks=1,
    )


def _media_request(kind: str) -> MediaRequest:
    if kind == "image":
        return MediaRequest(
            capability="image_reference_generate",
            params={"prompt": "crash matrix"},
            aspect=Aspect.WIDE,
            seed=17,
        )
    return MediaRequest(
        capability="image_to_video",
        params={"prompt": "crash matrix"},
        aspect=Aspect.WIDE,
        duration_s=5.0,
        seed=17,
    )


@pytest.mark.ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("crash_point", CRASH_POINTS)
async def test_crash_matrix(tmp_path, monkeypatch, kind, crash_point):
    home = make_home(tmp_path, registry_overrides=PAID_MOCK_PRICES, filename="rec")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db_path = home / "data" / "studio.db"
    db = Database(db_path)
    migrate(db)
    clock = FakeClock()
    ids = _Ids()

    make_provider = _image if kind == "image" else _video
    model_key = "mock.image" if kind == "image" else "mock.video"
    req = _media_request(kind)

    injector = FaultInjector()
    injector.arm(crash_point)

    gateway1 = build_gateway(
        db=db,
        config=config,
        clock=clock,
        ids=ids,
        faults=injector,
        providers={model_key: make_provider(tmp_path, ids)},
        seed=1,
    )

    with pytest.raises(SimulatedCrash):
        if kind == "image":
            await gateway1.image(model_key, req, _ctx())
        else:
            await gateway1.video(model_key, req, _ctx())

    # --- resume: brand-new Gateway, Ledger and DB handles on the same files
    db2 = Database(db_path)
    clock2 = FakeClock()
    ids2 = _Ids()
    ledger2 = LedgerService(db2, clock2, config.settings, ids2)
    store2 = ContentStore(Path(config.settings.paths.cas), db2, clock2)
    provider2 = make_provider(tmp_path, ids2)
    gateway2 = build_gateway(
        db=db2,
        config=config,
        clock=clock2,
        ids=ids2,
        cas=store2,
        ledger=ledger2,
        providers={model_key: provider2},
        seed=1,
    )

    if kind == "image":
        result = await gateway2.image(model_key, req, _ctx())
    else:
        result = await gateway2.video(model_key, req, _ctx())

    # --- assertions ------------------------------------------------------
    ledger_state = json.loads((tmp_path / "media_jobs.json").read_text(encoding="utf-8"))
    assert ledger_state["submit_count"] == _EXPECTED_SUBMITS[crash_point]

    call_row = db2.connect().execute(
        "SELECT status, est_cost_musd FROM provider_calls"
    ).fetchone()
    est = call_row["est_cost_musd"]
    assert est > 0  # non-zero prices prove the settled amount
    assert call_row["status"] == "completed"

    # no dangling reservation: ledger reconciles to exactly one call's cost per scope
    for scope_ref in ("2026-01", "global"):
        held = db2.connect().execute(
            "SELECT COALESCE(SUM(amount_musd), 0) AS h FROM cost_ledger "
            "WHERE scope_ref = ?",
            (scope_ref,),
        ).fetchone()["h"]
        assert held == est

    settle = db2.connect().execute(
        "SELECT COALESCE(SUM(amount_musd), 0) AS s FROM cost_ledger WHERE entry_type = 'settle'"
    ).fetchone()["s"]
    assert settle == 2 * est  # settled exactly 1x per scope (month + global)

    # exactly one potential_orphan audit when (and only when) the provider may
    # have accepted work before we lost the remote id
    orphan_audits = db2.connect().execute(
        "SELECT count(*) AS n FROM audit_events WHERE event = 'potential_orphan'"
    ).fetchone()["n"]
    if crash_point == "after_submit_before_persist":
        assert orphan_audits == 1
    else:
        assert orphan_audits == 0

    reserve_rows = db2.connect().execute(
        "SELECT count(*) AS n FROM cost_ledger WHERE entry_type = 'reserve'"
    ).fetchone()["n"]
    release_rows = db2.connect().execute(
        "SELECT count(*) AS n FROM cost_ledger WHERE entry_type = 'release'"
    ).fetchone()["n"]
    assert reserve_rows == release_rows  # every reservation was settled exactly once

    # exactly one asset cached and it is resolvable
    cached = db2.connect().execute("SELECT count(*) AS n FROM cache_entries").fetchone()["n"]
    assert cached == 1
    assert len(result.assets) == 1
    assert store2.resolve(result.assets[0].sha256).is_file()
    cas_files = list(Path(config.settings.paths.cas).rglob("*.*"))
    assert len([p for p in cas_files if p.is_file()]) == 1


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_resume_after_crash_is_a_cache_hit_for_identical_replay(
    tmp_path, monkeypatch
):
    """After a full crash+resume cycle, a third identical call is free."""
    home = make_home(tmp_path, registry_overrides=PAID_MOCK_PRICES, filename="rec2")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db_path = home / "data" / "studio.db"
    db = Database(db_path)
    migrate(db)
    clock = FakeClock()
    ids = _Ids()

    injector = FaultInjector()
    injector.arm("during_poll")
    req = _media_request("image")

    gateway1 = build_gateway(
        db=db, config=config, clock=clock, ids=ids, faults=injector,
        providers={"mock.image": _image(tmp_path, ids)}, seed=1,
    )
    with pytest.raises(SimulatedCrash):
        await gateway1.image("mock.image", req, _ctx())

    db2 = Database(db_path)
    gateway2 = build_gateway(
        db=db2, config=config, clock=FakeClock(), ids=_Ids(),
        providers={"mock.image": _image(tmp_path, _Ids())}, seed=1,
    )
    await gateway2.image("mock.image", req, _ctx())

    gateway3 = build_gateway(
        db=db2, config=config, clock=FakeClock(), ids=_Ids(),
        providers={"mock.image": _image(tmp_path, _Ids())}, seed=1,
    )
    third = await gateway3.image("mock.image", req, _ctx())
    assert third.cache_hit is True
    assert third.cost_musd == 0


def _ctx():
    from studio.core.gateway import CallContext

    return CallContext(episode_id=None, stage_run_id=None, nonce="m1", purpose="crash-matrix")


def test_crash_points_cover_the_seven_defined_points():
    assert set(CRASH_POINTS) == {
        "after_reserve",
        "before_submit",
        "after_submit_before_persist",
        "after_persist_id",
        "during_poll",
        "after_fetch_before_store",
        "after_store_before_complete",
    }
