"""Phase 3 hardening tests: T-CAS-03 (retention upgrade) and
T-REC-02 (single-call crash semantics, D2)."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from conftest import PAID_MOCK_PRICES, build_gateway, make_home

from studio.config.loader import load_all
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock
from studio.core.faults import FaultInjector, SimulatedCrash
from studio.core.gateway import CallContext
from studio.db import Database, migrate
from studio.domain.enums import Language
from studio.providers.base import LLMRequest, TTSRequest
from studio.providers.mock.llm import MockLLMProvider
from studio.providers.mock.tts import MockTTSProvider


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"rec-{next(self._counter):06d}"


def _ctx() -> CallContext:
    return CallContext(episode_id=None, stage_run_id=None, nonce="m1", purpose="t-rec-02")


# --------------------------------------------------------------- T-CAS-03

@pytest.mark.parametrize("order", ["up", "down"])
def test_t_cas_03_retention_upgrade_both_orders(tmp_path, clock, db, order):
    store = ContentStore(tmp_path / "cas", db, clock)
    payload = tmp_path / "blob.bin"
    payload.write_bytes(b"retention upgrade bytes")

    if order == "up":
        first = store.put_file(payload, kind="asset", ext="bin", move=False, retention="intermediate")
        second = store.put_file(payload, kind="asset", ext="bin", move=False, retention="permanent")
    else:
        first = store.put_file(payload, kind="asset", ext="bin", move=False, retention="permanent")
        second = store.put_file(payload, kind="asset", ext="bin", move=False, retention="intermediate")
    assert first.sha256 == second.sha256
    row = db.connect().execute(
        "SELECT retention, purge_after FROM assets WHERE sha256 = ?", (first.sha256,)
    ).fetchone()
    assert row["retention"] == "permanent"
    assert row["purge_after"] is None


@pytest.mark.parametrize("order", ["intermediate_to_keep", "keep_to_intermediate"])
def test_t_cas_03_keep_n_days_wins_over_intermediate_whichever_orders(tmp_path, clock, db, order):
    store = ContentStore(tmp_path / "cas", db, clock)
    payload = tmp_path / "blob.bin"
    payload.write_bytes(b"keep-n-days bytes")
    keep_purge = "2026-12-09T00:00:00Z"

    if order == "intermediate_to_keep":
        store.put_file(payload, kind="asset", ext="bin", move=False, retention="intermediate")
        store.put_file(payload, kind="asset", ext="bin", move=False,
                       retention="keep_n_days", purge_after=keep_purge)
    else:
        store.put_file(payload, kind="asset", ext="bin", move=False,
                       retention="keep_n_days", purge_after=keep_purge)
        store.put_file(payload, kind="asset", ext="bin", move=False, retention="intermediate")
    row = db.connect().execute(
        "SELECT retention, purge_after FROM assets WHERE sha256 = (SELECT sha256 FROM assets LIMIT 1)"
    ).fetchone()
    assert row["retention"] == "keep_n_days"
    assert row["purge_after"] == keep_purge


def test_t_cas_03_keep_n_days_later_purge_after_wins(tmp_path, clock, db):
    store = ContentStore(tmp_path / "cas", db, clock)
    payload = tmp_path / "blob.bin"
    payload.write_bytes(b"keep-n-days ordering bytes")

    store.put_file(payload, kind="asset", ext="bin", move=False,
                   retention="keep_n_days", purge_after="2026-12-01T00:00:00Z")
    store.put_file(payload, kind="asset", ext="bin", move=False,
                   retention="keep_n_days", purge_after="2027-01-01T00:00:00Z")
    row = db.connect().execute("SELECT purge_after FROM assets").fetchone()
    assert row["purge_after"] == "2027-01-01T00:00:00Z"
    # earlier purge_after never downgrades the stored one
    store.put_file(payload, kind="asset", ext="bin", move=False,
                   retention="keep_n_days", purge_after="2026-12-15T00:00:00Z")
    row = db.connect().execute("SELECT purge_after FROM assets").fetchone()
    assert row["purge_after"] == "2027-01-01T00:00:00Z"


def test_t_cas_03_keep_n_days_requires_purge_after(tmp_path, clock, db):
    from studio.domain.errors import ConfigError

    store = ContentStore(tmp_path / "cas", db, clock)
    payload = tmp_path / "blob.bin"
    payload.write_bytes(b"needs purge after")
    with pytest.raises(ConfigError):
        store.put_file(payload, kind="asset", ext="bin", move=False, retention="keep_n_days")


# --------------------------------------------------------------- T-REC-02

_SINGLE_CALL_POINTS = ("after_reserve", "before_submit", "during_single_call", "after_store_before_complete")
_EXPECTED_ORPHANS = {
    "after_reserve": 0,
    "before_submit": 0,
    "during_single_call": 1,
    "after_store_before_complete": 1,
}


def _llm_request() -> LLMRequest:
    return LLMRequest(system="system", prompt="say hi", max_tokens=16)


def _tts_request() -> TTSRequest:
    return TTSRequest(
        text="he-llo crash matrix",
        voice_id="mock-voice",
        language=Language.AR,
        settings={"stability": 0.5},
    )


@pytest.mark.ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize("call_kind", ["llm", "tts"])
@pytest.mark.parametrize("crash_point", _SINGLE_CALL_POINTS)
async def test_t_rec_02_single_call_crash_resume(tmp_path, monkeypatch, call_kind, crash_point):
    home = make_home(tmp_path, registry_overrides=PAID_MOCK_PRICES, filename="rec02")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db_path = home / "data" / "studio.db"
    db = Database(db_path)
    migrate(db)
    ids = _Ids()
    clock = FakeClock()
    model_key = "mock.llm" if call_kind == "llm" else "mock.tts"

    def make_provider():
        if call_kind == "llm":
            return MockLLMProvider(ids=ids)
        return MockTTSProvider(
            chars_per_second=15,
            workdir=Path(tmp_path / "mock-tts"),
            ids=ids,
        )

    req = _llm_request() if call_kind == "llm" else _tts_request()

    injector = FaultInjector()
    injector.arm(crash_point)
    gateway1 = build_gateway(
        db=db, config=config, clock=clock, ids=ids, faults=injector,
        providers={model_key: make_provider()}, seed=1,
    )
    with pytest.raises(SimulatedCrash):
        if call_kind == "llm":
            await gateway1.llm(model_key, req, _ctx())
        else:
            await gateway1.tts(model_key, req, _ctx())

    # --- resume with brand-new Gateway/Ledger/DB handles on the same files
    db2 = Database(db_path)
    clock2 = FakeClock()
    ids2 = _Ids()

    def make_provider2():
        if call_kind == "llm":
            return MockLLMProvider(ids=ids2)
        return MockTTSProvider(
            chars_per_second=15,
            workdir=Path(tmp_path / "mock-tts"),
            ids=ids2,
        )

    gateway2 = build_gateway(
        db=db2, config=config, clock=clock2, ids=ids2,
        providers={model_key: make_provider2()}, seed=1,
    )
    if call_kind == "llm":
        result = await gateway2.llm(model_key, req, _ctx())
    else:
        result = await gateway2.tts(model_key, req, _ctx())

    # --- invariants ------------------------------------------------------
    # LLM/TTS actuals come from provider-reported usage and generally differ
    # from the reservation; the invariant is one settle and a reconciled
    # ledger, not equality with est.
    call_row = db2.connect().execute(
        "SELECT status, est_cost_musd, actual_cost_musd FROM provider_calls"
    ).fetchone()
    assert call_row["status"] == "completed"
    assert call_row["est_cost_musd"] > 0
    actual = call_row["actual_cost_musd"]
    assert actual is not None and actual > 0

    # ledger reconciles to exactly one call: reserve(est) + release(est) cancel,
    # settle(actual) remains -> held == actual per scope (month + global).
    for scope_ref in ("2026-01", "global"):
        held = db2.connect().execute(
            "SELECT COALESCE(SUM(amount_musd), 0) AS h FROM cost_ledger "
            "WHERE scope_ref = ?",
            (scope_ref,),
        ).fetchone()["h"]
        assert held == actual

    settle = db2.connect().execute(
        "SELECT COALESCE(SUM(amount_musd), 0) AS s FROM cost_ledger WHERE entry_type = 'settle'"
    ).fetchone()["s"]
    assert settle == 2 * actual

    orphan_audits = db2.connect().execute(
        "SELECT count(*) AS n FROM audit_events WHERE event = 'potential_orphan'"
    ).fetchone()["n"]
    assert orphan_audits == _EXPECTED_ORPHANS[crash_point]

    reserve_rows = db2.connect().execute(
        "SELECT count(*) AS n FROM cost_ledger WHERE entry_type = 'reserve'"
    ).fetchone()["n"]
    release_rows = db2.connect().execute(
        "SELECT count(*) AS n FROM cost_ledger WHERE entry_type = 'release'"
    ).fetchone()["n"]
    assert reserve_rows == release_rows

    if call_kind == "tts":
        cached = db2.connect().execute(
            "SELECT count(*) AS n FROM cache_entries"
        ).fetchone()["n"]
        assert cached == 1
        assert len(result.assets) == 1
    # second invocation happened only where the provider may have billed:
    if crash_point == "during_single_call":
        pass  # documented re-invocation; exactly one orphan already asserted
