"""T-BUD-* (BEGIN_PROMPT section I)."""

from __future__ import annotations

import random
import sqlite3
import threading

import pytest
from conftest import PAID_MOCK_PRICES, build_gateway, make_home

from studio.config.loader import load_all
from studio.core.budget import LedgerService
from studio.core.clock import FakeClock
from studio.db import Database, build_repos, migrate
from studio.db.repos.providers import NewCall
from studio.domain.enums import Aspect
from studio.domain.errors import BudgetExceeded, KillSwitchTripped
from studio.providers.base import LLMRequest, MediaRequest


def _ensure_episode(db, episode_id: str) -> None:
    conn = db.connect()
    now = "2026-01-01T00:00:00.000Z"
    series_id = f"series-{episode_id}"
    conn.execute(
        "INSERT OR IGNORE INTO series(id, name, slug, created_at) VALUES (?, ?, ?, ?)",
        (series_id, "series", f"slug-{episode_id}", now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO episodes(id, series_id, title, concept, status, "
        "languages_json, aspects_json, created_at) "
        "VALUES (?, ?, 't', 'c', 'pending', '[\"ar\"]', '[\"9:16\"]', ?)",
        (episode_id, series_id, now),
    )


def _new_call(call_id_hint: str, est: int, episode_id: str | None = None) -> NewCall:
    return NewCall(
        episode_id=episode_id,
        stage_run_id=None,
        capability="llm",
        model_key="mock.llm",
        provider="mock",
        purpose=call_id_hint,
        cache_key=f"{abs(hash(call_id_hint)):064x}"[-64:].rjust(64, "0"),
        request_json="{}",
        est_cost_musd=est,
    )


def _paid_env(tmp_path, monkeypatch, *, settings_overrides=None):
    home = make_home(
        tmp_path,
        settings_overrides=settings_overrides,
        registry_overrides=PAID_MOCK_PRICES,
        filename="paid",
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids_seq = _SeqIds("call")
    ledger = LedgerService(db, clock, config.settings, ids_seq)
    return config, db, clock, ids_seq, ledger


class _SeqIds:
    def __init__(self, prefix: str):
        self._prefix = prefix
        self._n = 0

    def new(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n:06d}"


# --------------------------------------------------------------- T-BUD-01


def test_complete_nets_actual_and_fail_nets_zero(ledger, repos):
    call = ledger.begin_call(_new_call("complete-me", 500, episode_id=None))
    ledger.complete_call(call.id, 300, [])
    assert repos.budgets.held_musd("month", "2026-01") == 300
    assert repos.budgets.held_musd("global", "global") == 300

    call2 = ledger.begin_call(_new_call("fail-me", 500, episode_id=None))
    ledger.fail_call(call2.id, "permanent", "boom")
    assert repos.budgets.held_musd("month", "2026-01") == 300


def test_held_formula_after_random_interleavings(ledger, repos, config, db):
    _ensure_episode(db, "ep-rand")
    rng = random.Random(2024)
    expected: dict[tuple[str, str], int] = {}
    for round_no in range(24):
        est = rng.randint(100, 400)
        call = ledger.begin_call(_new_call(f"rand-{round_no}", est, episode_id="ep-rand"))
        for scope, ref in (("episode", "ep-rand"), ("month", "2026-01"), ("global", "global")):
            expected[(scope, ref)] = expected.get((scope, ref), 0) + est
        if rng.random() < 0.6:
            actual = rng.randint(0, est + 50)
            ledger.complete_call(call.id, actual, [])
            for scope, ref in (("episode", "ep-rand"), ("month", "2026-01"), ("global", "global")):
                expected[(scope, ref)] = expected[(scope, ref)] - est + actual
        else:
            ledger.fail_call(call.id, "permanent", "boom")
            for scope, ref in (("episode", "ep-rand"), ("month", "2026-01"), ("global", "global")):
                expected[(scope, ref)] = expected[(scope, ref)] - est
    for (scope, ref), want in expected.items():
        assert repos.budgets.held_musd(scope, ref) == want


# --------------------------------------------------------------- T-BUD-02


@pytest.mark.asyncio
async def test_cap_exceeded_refuses_before_provider_and_persists_kill_switch(
    tmp_path, monkeypatch
):
    config, db, clock, ids, ledger = _paid_env(
        tmp_path, monkeypatch, settings_overrides={"budgets": {"default_episode_cap_usd": 0.000001}}
    )
    from studio.providers.mock.image import MockImageProvider

    provider = MockImageProvider(
        ledger_path=tmp_path / "img_jobs.json",
        width_16x9=480,
        height_16x9=270,
        ids=ids,
        poll_ticks=1,
    )
    gateway = build_gateway(
        db=db, config=config, clock=clock, ids=ids, ledger=ledger, providers={"mock.image": provider}
    )
    req = MediaRequest(
        capability="image_reference_generate",
        params={"prompt": "x"},
        aspect=Aspect.TALL,
        seed=1,
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        await gateway.image("mock.image", req, _ctx("ep-cap"))
    assert excinfo.value.scope == "episode"
    assert provider.submit_count == 0  # refused before the provider saw the call

    row = db.connect().execute(
        "SELECT kill_switch_tripped FROM budgets WHERE scope = 'episode' AND scope_ref = 'ep-cap'"
    ).fetchone()
    assert row is not None and row["kill_switch_tripped"] == 1

    # survives closing and reopening the DB
    db2 = Database(db.path)
    row2 = db2.connect().execute(
        "SELECT kill_switch_tripped FROM budgets WHERE scope = 'episode' AND scope_ref = 'ep-cap'"
    ).fetchone()
    assert row2["kill_switch_tripped"] == 1


def _ctx(episode_id: str | None, nonce: str = "0"):
    from studio.core.gateway import CallContext

    return CallContext(episode_id=episode_id, stage_run_id=None, nonce=nonce, purpose="test")


# --------------------------------------------------------------- T-BUD-03


@pytest.mark.asyncio
async def test_kill_switch_blocks_paid_but_not_cache_hits_then_reset(
    tmp_path, monkeypatch
):
    config, db, clock, ids, ledger = _paid_env(tmp_path, monkeypatch)
    _ensure_episode(db, "ep-ks")
    gateway = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger)

    # prime the cache with a paid call
    req = LLMRequest(system="", prompt="cache me", max_tokens=16)
    first = await gateway.llm("mock.llm", req, _ctx("ep-ks", nonce="n1"))
    assert first.cache_hit is False
    assert first.cost_musd > 0

    repos = build_repos(db, clock, ids)
    repos.budgets.trip("episode", "ep-ks")

    # paid call is refused while tripped
    with pytest.raises(KillSwitchTripped):
        await gateway.llm("mock.llm", LLMRequest(system="", prompt="other", max_tokens=16),
                          _ctx("ep-ks", nonce="n2"))

    # cache hit still succeeds and costs nothing
    second = await gateway.llm("mock.llm", req, _ctx("ep-ks", nonce="n1"))
    assert second.cache_hit is True
    assert second.cost_musd == 0

    # reset re-enables spending and writes an audit event
    ledger.reset_kill_switch("episode", "ep-ks", new_cap_musd=10_000_000)
    assert repos.audit.has_event("kill_switch_reset", "episode:ep-ks")
    third = await gateway.llm("mock.llm", LLMRequest(system="", prompt="other2", max_tokens=16),
                              _ctx("ep-ks", nonce="n3"))
    assert third.cache_hit is False


# --------------------------------------------------------------- T-BUD-04


@pytest.mark.asyncio
async def test_episode_cap_trips_month_untouched(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(
        tmp_path, monkeypatch, settings_overrides={"budgets": {"default_episode_cap_usd": 0.000001}}
    )
    gateway = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger)
    with pytest.raises(BudgetExceeded) as excinfo:
        await gateway.llm("mock.llm", LLMRequest(prompt="hi", max_tokens=8), _ctx("ep-solo"))
    assert excinfo.value.scope == "episode"
    month_row = db.connect().execute(
        "SELECT kill_switch_tripped FROM budgets WHERE scope = 'month' AND scope_ref = '2026-01'"
    ).fetchone()
    assert month_row["kill_switch_tripped"] == 0


@pytest.mark.asyncio
async def test_month_cap_trips_episode_untouched(tmp_path, monkeypatch):
    config, db, clock, ids, ledger = _paid_env(
        tmp_path, monkeypatch, settings_overrides={"budgets": {"monthly_cap_usd": 0.000001}}
    )
    gateway = build_gateway(db=db, config=config, clock=clock, ids=ids, ledger=ledger)
    with pytest.raises(BudgetExceeded) as excinfo:
        await gateway.llm("mock.llm", LLMRequest(prompt="hi", max_tokens=8), _ctx("ep-solo"))
    assert excinfo.value.scope == "month"
    assert excinfo.value.scope_ref == "2026-01"  # clock's UTC month
    ep_row = db.connect().execute(
        "SELECT kill_switch_tripped FROM budgets WHERE scope = 'episode' AND scope_ref = 'ep-solo'"
    ).fetchone()
    assert ep_row["kill_switch_tripped"] == 0


# --------------------------------------------------------------- T-BUD-05


def test_warn_pct_emits_exactly_one_warning_per_scope(tmp_path, monkeypatch):
    _config, db, _clock, _ids, ledger = _paid_env(
        tmp_path,
        monkeypatch,
        settings_overrides={"budgets": {"default_episode_cap_usd": 0.01, "warn_pct": 50}},
    )
    _ensure_episode(db, "ep-warn")
    # 0.01 USD = 10_000 musd; warn at 50% => held >= 5000
    ledger.begin_call(_new_call("warn-1", 6000, episode_id="ep-warn"))
    ledger.begin_call(_new_call("warn-2", 10, episode_id="ep-warn"))
    warnings = db.connect().execute(
        "SELECT entity FROM audit_events WHERE event = 'budget_warning'"
    ).fetchall()
    entities = [w["entity"] for w in warnings]
    assert entities.count("episode:ep-warn") == 1


# --------------------------------------------------------------- T-BUD-06


def test_concurrent_reservations_never_oversubscribe(tmp_path, monkeypatch):
    _config, db, _clock, _ids, ledger = _paid_env(
        tmp_path,
        monkeypatch,
        settings_overrides={"budgets": {"default_episode_cap_usd": 0.001, "warn_pct": 0}},
    )
    # cap = 1000 musd; two racing reservations of 600 each
    _ensure_episode(db, "ep-race")
    results: list[str] = []
    lock = threading.Lock()

    def reserve(tag: str) -> None:
        try:
            ledger.begin_call(_new_call(tag, 600, episode_id="ep-race"))
            outcome = "ok"
        except BudgetExceeded:
            outcome = "exceeded"
        except sqlite3.Error:
            outcome = "db-error"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=reserve, args=(f"race-{n}",)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results) == ["exceeded", "ok"]
    assert ledger is not None
    held = repos_held(db, "episode", "ep-race")
    assert held == 600  # exactly one reservation landed; cap never oversubscribed


def repos_held(db, scope: str, scope_ref: str) -> int:
    row = db.connect().execute(
        "SELECT COALESCE(SUM(amount_musd), 0) AS held FROM cost_ledger WHERE scope = ? AND scope_ref = ?",
        (scope, scope_ref),
    ).fetchone()
    return int(row["held"])
