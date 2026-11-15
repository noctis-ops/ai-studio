"""T-GW-* (BEGIN_PROMPT section I)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FAKE_SECRET, PAID_MOCK_PRICES, build_gateway, make_home

from studio.config.loader import load_all
from studio.core.budget import LedgerService
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock
from studio.core.gateway import CallContext
from studio.db import Database, migrate
from studio.domain.enums import Aspect
from studio.domain.errors import (
    PolicyRefusalError,
    TransientProviderError,
    ZeroByteAssetError,
)
from studio.providers.base import LLMRequest, MediaRequest
from studio.providers.mock.common import MockFaultPlan
from studio.providers.mock.image import MockImageProvider
from studio.providers.mock.llm import MockLLMProvider


def _ctx(episode_id=None, nonce: str = "0") -> CallContext:
    return CallContext(episode_id=episode_id, stage_run_id=None, nonce=nonce, purpose="test")


def _ledger_count(db, entry_type: str | None = None) -> int:
    if entry_type is None:
        return db.connect().execute("SELECT count(*) FROM cost_ledger").fetchone()[0]
    return db.connect().execute(
        "SELECT count(*) FROM cost_ledger WHERE entry_type = ?", (entry_type,)
    ).fetchone()[0]


def _paid_env(tmp_path, monkeypatch):
    home = make_home(tmp_path, registry_overrides=PAID_MOCK_PRICES, filename="paid")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = _SeqIds("gw")
    ledger = LedgerService(db, clock, config.settings, ids)
    return config, db, clock, ids, ledger


class _SeqIds:
    def __init__(self, prefix: str):
        self._prefix = prefix
        self._n = 0

    def new(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n:06d}"


def _image_provider(tmp_path, ids, fault_plan=None, poll_ticks=1):
    return MockImageProvider(
        ledger_path=tmp_path / "img_jobs.json",
        width_16x9=480,
        height_16x9=270,
        fault_plan=fault_plan,
        ids=ids,
        poll_ticks=poll_ticks,
    )


# --------------------------------------------------------------- T-GW-01


@pytest.mark.asyncio
async def test_second_identical_call_is_free_cache_hit(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = MockLLMProvider(ids=ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                        providers={"mock.llm": provider})

    req = LLMRequest(system="s", prompt="cache hit me", max_tokens=16)
    await gw.llm("mock.llm", req, _ctx(nonce="n1"))
    n_ledger = _ledger_count(db)
    n_calls = len(provider.calls)

    second = await gw.llm("mock.llm", req, _ctx(nonce="n1"))
    assert second.cache_hit is True
    assert second.cost_musd == 0
    assert len(provider.calls) == n_calls  # provider untouched
    assert _ledger_count(db) == n_ledger   # no new ledger rows


# --------------------------------------------------------------- T-GW-02


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_new_nonce_pays_again_and_old_asset_retained(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = _image_provider(tmp_path, ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                      providers={"mock.image": provider})
    store = ContentStore(Path(config.settings.paths.cas), db, clock)

    req = MediaRequest(capability="image_reference_generate",
                       params={"prompt": "a lighthouse"},
                       aspect=Aspect.TALL, seed=7)
    first = await gw.image("mock.image", req, _ctx(nonce="n1"))
    sha = first.assets[0].sha256
    second = await gw.image("mock.image", req, _ctx(nonce="n2"))
    assert second.cache_hit is False
    assert provider.submit_count == 2
    assert store.resolve(sha).is_file()  # the original asset was retained


# --------------------------------------------------------------- T-GW-03


@pytest.mark.asyncio
async def test_transient_failures_then_success_single_reservation(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = MockLLMProvider(MockFaultPlan(fail_transient_n=2), ids=ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                       providers={"mock.llm": provider})

    result = await gw.llm("mock.llm", LLMRequest(prompt="flaky", max_tokens=8), _ctx(nonce="n1"))
    assert result.cache_hit is False
    assert len(provider.calls) == 1  # only the successful op is recorded
    call_rows = db.connect().execute(
        "SELECT status, attempt FROM provider_calls"
    ).fetchall()
    assert len(call_rows) == 1
    assert call_rows[0]["status"] == "completed"
    assert call_rows[0]["attempt"] == 3  # two failures + final success
    # per scope: exactly one reserve and one settle
    assert _ledger_count(db, "reserve") == 2  # month + global (no episode)
    assert _ledger_count(db, "settle") == 2


@pytest.mark.asyncio
async def test_transient_exhaustion_leaves_net_zero_ledger(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = MockLLMProvider(MockFaultPlan(fail_transient_n=99), ids=ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                       providers={"mock.llm": provider})

    with pytest.raises(TransientProviderError):
        await gw.llm("mock.llm", LLMRequest(prompt="hopeless", max_tokens=8), _ctx(nonce="n1"))

    call_rows = db.connect().execute(
        "SELECT status, error_class, attempt FROM provider_calls"
    ).fetchall()
    assert len(call_rows) == 1
    assert call_rows[0]["status"] == "failed"
    assert call_rows[0]["error_class"] == "transient"
    assert call_rows[0]["attempt"] == 5  # retry.transient_max
    held = db.connect().execute(
        "SELECT COALESCE(SUM(amount_musd), 0) AS h FROM cost_ledger"
    ).fetchone()["h"]
    assert held == 0


# --------------------------------------------------------------- T-GW-04


@pytest.mark.asyncio
async def test_policy_refusal_releases_and_never_retries(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = MockLLMProvider(MockFaultPlan(fail_policy=True), ids=ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                       providers={"mock.llm": provider})

    with pytest.raises(PolicyRefusalError):
        await gw.llm("mock.llm", LLMRequest(prompt="refused", max_tokens=8), _ctx(nonce="n1"))

    row = db.connect().execute(
        "SELECT status, error_class, attempt FROM provider_calls"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["error_class"] == "policy"
    assert row["attempt"] == 1  # no retry on a policy refusal
    held = db.connect().execute(
        "SELECT COALESCE(SUM(amount_musd), 0) AS h FROM cost_ledger"
    ).fetchone()["h"]
    assert held == 0


# --------------------------------------------------------------- T-GW-05


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_purged_asset_turns_cache_hit_into_miss(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = _image_provider(tmp_path, ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                       providers={"mock.image": provider})
    store = ContentStore(Path(config.settings.paths.cas), db, clock)

    req = MediaRequest(capability="image_reference_generate",
                       params={"prompt": "a whale"},
                       aspect=Aspect.TALL, seed=11)
    first = await gw.image("mock.image", req, _ctx(nonce="n1"))
    store.mark_purged(first.assets[0].sha256)

    second = await gw.image("mock.image", req, _ctx(nonce="n1"))
    assert second.cache_hit is False
    assert provider.submit_count == 2


# --------------------------------------------------------------- T-GW-06


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_zero_byte_result_settles_estimate_marks_failed_caches_nothing(
    tmp_path, monkeypatch
):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = _image_provider(tmp_path, ids, fault_plan=MockFaultPlan(zero_byte=True))
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                       providers={"mock.image": provider})

    req = MediaRequest(capability="image_reference_generate",
                       params={"prompt": "empty"},
                       aspect=Aspect.TALL, seed=13)
    with pytest.raises(ZeroByteAssetError):
        await gw.image("mock.image", req, _ctx(nonce="n1"))

    row = db.connect().execute(
        "SELECT status, error_class FROM provider_calls"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["error_class"] == "permanent"
    # estimate settled (provider likely billed): month scope holds the estimate
    month_held = db.connect().execute(
        "SELECT COALESCE(SUM(amount_musd), 0) AS h FROM cost_ledger WHERE scope = 'month'"
    ).fetchone()["h"]
    assert month_held == ledger.budgets.held_musd("month", "2026-01")
    assert month_held > 0
    cache_rows = db.connect().execute("SELECT count(*) FROM cache_entries").fetchone()[0]
    assert cache_rows == 0
    # the failure was not cached: paying again goes back to the provider
    with pytest.raises(ZeroByteAssetError):
        await gw.image("mock.image", req, _ctx(nonce="n2"))
    assert provider.submit_count == 2


# --------------------------------------------------------------- T-GW-07


@pytest.mark.asyncio
async def test_llm_results_cached_via_inline_json(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = MockLLMProvider(ids=ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                       providers={"mock.llm": provider})

    req = LLMRequest(system="s", prompt="give shots", schema_name="demo_shots", max_tokens=64)
    first = await gw.llm("mock.llm", req, _ctx(nonce="n1"))
    assert first.inline_json is not None and "shots" in first.inline_json

    second = await gw.llm("mock.llm", req, _ctx(nonce="n1"))
    assert second.cache_hit is True
    assert second.inline_json == first.inline_json
    assert len(provider.calls) == 1  # no provider call on the hit


# --------------------------------------------------------------- T-GW-08


@pytest.mark.asyncio
async def test_request_json_never_contains_secret_values(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FAL_KEY", FAKE_SECRET)
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    provider = MockLLMProvider(ids=ids)
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger,
                       providers={"mock.llm": provider})

    # smuggle the secret into the payload itself: redact must catch it
    req = LLMRequest(system=f"context {FAKE_SECRET} more", prompt=f"key is {FAKE_SECRET}", max_tokens=8)
    await gw.llm("mock.llm", req, _ctx(nonce="n1"))

    row = db.connect().execute("SELECT request_json FROM provider_calls").fetchone()
    assert FAKE_SECRET not in row["request_json"]
    assert "***" in row["request_json"]
    # and nowhere in the raw database file either
    raw = db.path.read_bytes()
    assert FAKE_SECRET.encode() not in raw
