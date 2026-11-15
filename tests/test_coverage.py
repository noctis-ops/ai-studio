"""Targeted unit tests closing line-coverage gaps (see §I coverage rule)."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import build_gateway

from studio.config.registry import ModelEntry, Per1kChars, Per1mTokens, PerImage, PerSecond
from studio.core.budget import fmt_cap
from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, SystemClock, format_ts, parse_ts
from studio.core.faults import FaultInjector, SimulatedCrash
from studio.core.ids import new_id
from studio.core.pricing import actual_cost_musd, estimate_cost_musd
from studio.core.ratelimit import ProviderLimiter
from studio.db.repos.providers import NewCall
from studio.domain.enums import Language, Scope
from studio.domain.errors import (
    AssetNotFoundError,
    ConfigError,
    PermanentProviderError,
    RegistryError,
    StudioError,
    TransientProviderError,
    ZeroByteAssetError,
)
from studio.domain.money import add_musd, ceil_div_musd, fmt_usd
from studio.domain.refs import (
    SubjectRef,
    lineage_contains,
    lineage_from_json,
    lineage_to_json,
    subjects_related,
)
from studio.providers.base import (
    LLMRequest,
    LLMResult,
    MediaPayload,
    MediaRequest,
    TTSRequest,
    TTSResult,
)


def _call(est: int, purpose: str = "cov") -> NewCall:
    return NewCall(
        episode_id=None,
        stage_run_id=None,
        capability="llm",
        model_key="mock.llm",
        provider="mock",
        purpose=purpose,
        cache_key=f"{random.Random(purpose).randrange(2**256):064x}",
        request_json="{}",
        est_cost_musd=est,
    )


# --------------------------------------------------------------- faults/ids/clock


def test_fault_injector_arm_forever_disarm_and_unknown_point():
    faults = FaultInjector()
    faults.arm("after_reserve", once=False)
    with pytest.raises(SimulatedCrash):
        faults.crash_at("after_reserve")
    with pytest.raises(SimulatedCrash):
        faults.crash_at("after_reserve")  # not consumed: once=False
    faults.disarm("after_reserve")
    faults.crash_at("after_reserve")  # no-op
    with pytest.raises(ConfigError):
        faults.arm("not-a-point")


def test_new_id_returns_string():
    assert isinstance(new_id(), str) and len(new_id()) > 0


def test_clock_format_parse_roundtrip_and_validation():
    stamp = format_ts(datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC))
    assert stamp.endswith("Z")
    assert parse_ts(stamp) == datetime(2026, 3, 4, 5, 6, 7, 123000, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        format_ts(datetime(2026, 3, 4, 5, 6, 7))
    assert SystemClock().now_iso().endswith("Z")
    assert len(SystemClock().month_tag()) == 7


def test_fake_clock_month_tag_and_advance():
    clock = FakeClock()
    assert clock.month_tag() == "2026-01"
    from datetime import timedelta

    before = clock.monotonic()
    clock.advance(timedelta(0))
    assert clock.monotonic() == before


# --------------------------------------------------------------- db.tx


def test_tx_rolls_back_on_exception(db):
    with pytest.raises(RuntimeError, match="boom"):
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO series(id, name, slug, created_at) VALUES ('s-tx', 's', 's-tx', 'x')"
            )
            raise RuntimeError("boom")
    row = db.connect().execute("SELECT count(*) FROM series WHERE id = 's-tx'").fetchone()
    assert row[0] == 0


# --------------------------------------------------------------- CAS


def test_cas_put_bytes_roundtrip_and_guards(tmp_path, clock, db):
    store = ContentStore(tmp_path / "cas", db, clock)
    ref = store.put_bytes(b"payload-bytes", kind="asset", ext="bin")
    assert store.resolve(ref.sha256).read_bytes() == b"payload-bytes"
    assert store.exists_live(ref.sha256)
    assert store.verify(ref.sha256)
    with pytest.raises(ZeroByteAssetError):
        store.put_bytes(b"", kind="asset", ext="bin")
    with pytest.raises((ConfigError, ValueError)):
        store.put_bytes(b"x", kind="asset", ext="no/slash")
    with pytest.raises(AssetNotFoundError):
        store.resolve("f" * 64)
    store.mark_purged(ref.sha256)
    assert not store.exists_live(ref.sha256)
    assert not store.verify(ref.sha256)


# --------------------------------------------------------------- canonical


def test_canonical_edge_cases():
    from pathlib import Path as _Path

    with pytest.raises(TypeError):
        canonical_json({"p": _Path("/tmp/x")})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        canonical_json({1: "x"})  # type: ignore[dict-item]
    assert canonical_json({"t": ("b", "a")}) == canonical_json({"t": ["b", "a"]})
    first = canonical_json({"s": {"b", "a", "c"}})
    second = canonical_json({"s": {"c", "a", "b"}})
    assert first == second  # sets are order-insensitive
    with pytest.raises(TypeError):
        canonical_json({"x": object()})  # type: ignore[dict-item]


# --------------------------------------------------------------- refs/subjects


def test_subjects_related_and_lineage_helpers():
    episode = SubjectRef(scope=Scope.EPISODE, id="ep", lineage=((Scope.SERIES, "s"),))
    shot = SubjectRef(scope=Scope.SHOT, id="sh", lineage=((Scope.SERIES, "s"), (Scope.EPISODE, "ep")))
    assert subjects_related(episode, shot)
    assert subjects_related(shot, episode)
    other = SubjectRef(scope=Scope.SHOT, id="sh2", lineage=((Scope.SERIES, "s"),))
    assert not subjects_related(shot, other)
    assert lineage_contains(shot.lineage, Scope.EPISODE, "ep")
    assert lineage_contains(shot.lineage, Scope.EPISODE, "nope") is False
    roundtrip = lineage_from_json(lineage_to_json(shot.lineage))
    assert roundtrip == shot.lineage


# --------------------------------------------------------------- ratelimit


def test_ratelimit_argument_validation():
    clock = FakeClock()

    async def noop(_: float) -> None:
        return None

    with pytest.raises(ValueError):
        ProviderLimiter(0, 0.0, clock, noop)
    with pytest.raises(ValueError):
        ProviderLimiter(1, -1.0, clock, noop)


# --------------------------------------------------------------- pricing


def _entry(pricing) -> ModelEntry:
    return ModelEntry(provider="mock", capability="llm", model_id="m", pricing=pricing)


def test_pricing_wrong_request_type_raises():
    entry = _entry(Per1mTokens(unit="per_1m_tokens", in_usd=Decimal(1), out_usd=Decimal(1)))
    with pytest.raises(StudioError):
        estimate_cost_musd(entry, TTSRequest(text="x", voice_id="v", language=Language.AR))
    entry2 = _entry(PerSecond(unit="per_second", usd=Decimal(1)))
    with pytest.raises(StudioError):
        estimate_cost_musd(entry2, LLMRequest(prompt="p"))
    entry3 = _entry(Per1kChars(unit="per_1k_chars", usd=Decimal(1)))
    with pytest.raises(StudioError):
        estimate_cost_musd(entry3, LLMRequest(prompt="p"))


def test_pricing_bad_n_images_and_actual_cost_paths():
    entry = _entry(PerImage(unit="per_image", usd=Decimal("2")))
    bad = MediaRequest(capability="image_reference_generate", params={"n_images": 0})
    with pytest.raises(StudioError):
        estimate_cost_musd(entry, bad)
    bad2 = MediaRequest(capability="image_reference_generate", params={"n_images": "two"})
    with pytest.raises(StudioError):
        estimate_cost_musd(entry, bad2)

    llm_entry = _entry(Per1mTokens(unit="per_1m_tokens", in_usd=Decimal("1.5"), out_usd=Decimal("2.5")))
    actual = actual_cost_musd(
        llm_entry,
        LLMRequest(prompt="p"),
        LLMResult(text="t", tokens_in=1_000_000, tokens_out=2_000_000),
    )
    assert actual == 1_000_000 * 3 // 2 + 2_000_000 * 5 // 2

    tts_entry = _entry(Per1kChars(unit="per_1k_chars", usd=Decimal("0.4")))
    tts_actual = actual_cost_musd(
        tts_entry,
        TTSRequest(text="x" * 5000, voice_id="v", language=Language.AR),
        TTSResult(payload=MediaPayload(url=None, local_path=None, ext="wav", mime="audio/wav"), billed_chars=2500),
    )
    assert tts_actual == 1_000_000  # 2500 * 0.4 / 1000 = 1.0 USD


# --------------------------------------------------------------- money


def test_money_helpers():
    assert add_musd(100, 23) == 123
    assert ceil_div_musd(Decimal(7), Decimal(2)) == 4
    assert fmt_usd(1_500_000) == "$1.500000"
    assert fmt_cap(None) == "(no cap)"
    assert fmt_cap(2_500_000) == "$2.50"


# --------------------------------------------------------------- ledger edges


def test_begin_call_with_explicit_conn(ledger, db):
    conn = db.connect()
    call = ledger.begin_call(_call(100, "explicit-conn"), conn=conn)
    assert call.est_cost_musd == 100
    ledger.fail_call(call.id, "permanent", "done")


def test_complete_and_fail_call_unknown_id_raise(ledger):
    with pytest.raises(StudioError, match="unknown provider call"):
        ledger.complete_call("no-such-call", 0, [])
    with pytest.raises(StudioError, match="unknown provider call"):
        ledger.fail_call("no-such-call", "permanent", "nope")


def test_reset_kill_switch_validation(ledger):
    with pytest.raises(StudioError, match="unknown budget scope"):
        ledger.reset_kill_switch("bogus-scope", "x")
    with pytest.raises(StudioError, match="no budget row"):
        ledger.reset_kill_switch("month", "2099-01")


def test_budget_scopes_for_call_without_episode():
    from studio.domain.enums import GLOBAL_SCOPE_REF, BudgetScope, budget_scopes_for_call

    scopes = budget_scopes_for_call(None, "2026-11")
    assert scopes == [(BudgetScope.MONTH, "2026-11"), (BudgetScope.GLOBAL, GLOBAL_SCOPE_REF)]
    with_episode = budget_scopes_for_call("ep", "2026-11")
    assert with_episode[0] == (BudgetScope.EPISODE, "ep")


# --------------------------------------------------------------- gateway misc


@pytest.mark.asyncio
async def test_gateway_materialize_payload_download_path(
    tmp_path, monkeypatch, config, db, clock, ledger
):
    written = b"downloaded-bytes" * 64

    async def fake_downloader(url: str, dest: Path) -> None:
        dest.write_bytes(written)

    gw = build_gateway(
        db=db, config=config, clock=clock, ids=_SeqIdsForGateway(),
        ledger=ledger, providers={}, seed=3,
    )
    gw._downloader = fake_downloader
    payload = MediaPayload(url="https://provider.example/clip.png", local_path=None, ext="png", mime="image/png")
    local_path, tmp = await gw._materialize_payload(payload)
    assert tmp is not None
    assert local_path.read_bytes() == written
    tmp.unlink()

    async def failing_downloader(url: str, dest: Path) -> None:
        raise RuntimeError("connection reset")

    gw._downloader = failing_downloader
    with pytest.raises(TransientProviderError):
        await gw._materialize_payload(payload)

    with pytest.raises(PermanentProviderError):
        await gw._materialize_payload(
            MediaPayload(url=None, local_path=None, ext="png", mime="image/png")
        )


class _SeqIdsForGateway:
    _n = 0

    def new(self) -> str:
        _SeqIdsForGateway._n += 1
        return f"gwmisc-{_SeqIdsForGateway._n:06d}"


@pytest.mark.asyncio
async def test_gateway_entry_registry_errors(tmp_path, monkeypatch, config, db, clock, ledger):
    gw = build_gateway(
        db=db, config=config, clock=clock, ids=_SeqIdsForGateway(),
        ledger=ledger, providers={}, seed=3,
    )
    with pytest.raises(RegistryError, match="unknown model_key"):
        gw._entry("no.such.model", "llm")
    with pytest.raises(RegistryError, match="capability"):
        gw._entry("mock.llm", "tts")
