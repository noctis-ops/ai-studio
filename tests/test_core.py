"""T-CK-*, T-CAS-*, T-RETRY-01, T-RL-01, T-PRICE-01, T-MONEY-01."""

from __future__ import annotations

import asyncio
import os
import random
from decimal import Decimal
from pathlib import Path

import pytest

from studio.config.registry import ModelEntry, Per1kChars, Per1mTokens, PerImage, PerSecond
from studio.core.cache_key import make_cache_key
from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, fake_sleep
from studio.core.pricing import estimate_cost_musd
from studio.core.ratelimit import ProviderLimiter
from studio.core.retry import with_retry
from studio.domain.enums import Aspect, Language
from studio.domain.errors import TransientProviderError, ZeroByteAssetError
from studio.domain.money import musd_to_usd, usd_to_musd
from studio.providers.base import LLMRequest, MediaRequest, TTSRequest


def _key(**overrides):
    params = {"b": 2, "a": 1}
    base = dict(
        capability="llm",
        model_key="mock.llm",
        model_ref="mock-llm",
        params=params,
        input_asset_sha256s=["b" * 64, "a" * 64],
        prompt_version_sha="c" * 40,
        seed=42,
        nonce="n-1",
    )
    base.update(overrides)
    return make_cache_key(**base)


# --------------------------------------------------------------- T-CK-01


def test_cache_key_stable_under_dict_reorder_and_asset_order():
    assert _key() == _key(params={"a": 1, "b": 2})
    assert _key(input_asset_sha256s=["a" * 64, "b" * 64]) == _key()


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "n-2"},
        {"model_ref": "mock-llm-2"},
        {"seed": 43},
        {"prompt_version_sha": "d" * 40},
        {"input_asset_sha256s": ["c" * 64, "a" * 64]},
    ],
)
def test_cache_key_changes_with_each_input(change):
    assert _key(**change) != _key()


# --------------------------------------------------------------- T-CK-02


def test_nfc_and_nfd_latin_strings_give_same_key():
    nfc = "café"
    nfd = "café"
    assert nfc != nfd
    assert canonical_json({"t": nfc}) == canonical_json({"t": nfd})


def test_arabic_diacritics_give_different_keys():
    with_fatha = "سَلَام"
    without = "سَلام"
    assert canonical_json({"t": with_fatha}) != canonical_json({"t": without})


def test_nan_rejected():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})
    with pytest.raises(ValueError):
        make_cache_key(
            capability="llm",
            model_key="m",
            model_ref="r",
            params={"x": float("nan")},
            input_asset_sha256s=[],
            prompt_version_sha=None,
            seed=None,
            nonce="n",
        )


# --------------------------------------------------------------- T-CAS-01


def _cas_payload(tmp_path: Path, name: str = "blob.bin", data: bytes = b"hello cas") -> Path:
    payload = tmp_path / name
    payload.write_bytes(data)
    return payload


def test_cas_put_layout_idempotent_paths(tmp_path, clock, db):
    store = ContentStore(tmp_path / "cas", db, clock)
    payload = _cas_payload(tmp_path)

    ref = store.put_file(payload, kind="asset", ext="bin", move=False)
    expected = Path(store.root) / ref.rel_path
    assert expected.is_file()
    sha = ref.sha256
    assert ref.rel_path == f"{sha[:2]}/{sha[2:4]}/{sha}.bin"
    assert "\\" not in ref.rel_path  # forward slashes only

    # re-put of identical bytes is a no-op and keeps the same reference
    ref2 = store.put_file(payload, kind="asset", ext="bin", move=False)
    assert ref2.sha256 == sha
    rows = db.connect().execute("SELECT count(*) FROM assets WHERE sha256 = ?", (sha,)).fetchone()
    assert rows[0] == 1


def test_cas_put_zero_byte_raises(tmp_path, clock, db):
    store = ContentStore(tmp_path / "cas", db, clock)
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(ZeroByteAssetError):
        store.put_file(empty, kind="asset", ext="bin")


# --------------------------------------------------------------- T-CAS-02


def test_crash_between_temp_write_and_replace_leaves_no_final_file(tmp_path, clock, db, monkeypatch):
    store = ContentStore(tmp_path / "cas", db, clock)
    payload = tmp_path / "blob.bin"
    payload.write_bytes(b"crash me")

    monkeypatch.setattr(os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("simulated crash")))
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.put_file(payload, kind="asset", ext="bin")

    leftovers = list(Path(store.root).rglob("*.bin")) if Path(store.root).exists() else []
    assert leftovers == []


def test_purged_asset_resolve_raises(tmp_path, clock, db):
    from studio.domain.errors import AssetNotFoundError

    store = ContentStore(tmp_path / "cas", db, clock)
    payload = tmp_path / "blob.bin"
    payload.write_bytes(b"purge me")
    ref = store.put_file(payload, kind="asset", ext="bin")
    assert store.resolve(ref.sha256).is_file()
    store.mark_purged(ref.sha256)
    with pytest.raises(AssetNotFoundError):
        store.resolve(ref.sha256)


# --------------------------------------------------------------- T-RETRY-01


@pytest.mark.asyncio
async def test_retry_full_jitter_bounds_and_max_attempts():
    sleeps: list[float] = []

    async def fake_sleep_local(seconds: float) -> None:
        sleeps.append(seconds)

    calls = {"n": 0}

    async def always_transient() -> str:
        calls["n"] += 1
        raise TransientProviderError("boom")

    with pytest.raises(TransientProviderError):
        await with_retry(
            always_transient,
            classify=lambda exc: isinstance(exc, TransientProviderError),
            max_attempts=4,
            base_s=2.0,
            cap_s=10.0,
            sleep=fake_sleep_local,
            rng=random.Random(1234),
        )
    assert calls["n"] == 4
    assert len(sleeps) == 3
    for attempt, delay in enumerate(sleeps, start=1):
        upper = min(10.0, 2.0 * (2 ** (attempt - 1)))
        assert 0.0 <= delay <= upper


class _Fatal(Exception):
    pass


@pytest.mark.asyncio
async def test_retry_never_retries_non_transient():
    sleeps: list[float] = []

    async def fake_sleep_local(seconds: float) -> None:
        sleeps.append(seconds)

    async def always_fatal() -> str:
        raise _Fatal("nope")

    with pytest.raises(_Fatal):
        await with_retry(
            always_fatal,
            classify=lambda exc: isinstance(exc, TransientProviderError),
            max_attempts=5,
            base_s=1.0,
            cap_s=8.0,
            sleep=fake_sleep_local,
            rng=random.Random(1),
        )
    assert sleeps == []


# --------------------------------------------------------------- T-RL-01


@pytest.mark.asyncio
async def test_limiter_enforces_max_concurrency():
    async def noop_sleep(seconds: float) -> None:
        return None

    limiter = ProviderLimiter(2, 0.0, FakeClock(), noop_sleep)
    entered = 0
    peak = 0

    async def work():
        nonlocal entered, peak
        async with limiter:
            entered += 1
            peak = max(peak, entered)
            await asyncio.sleep(0.02)
            entered -= 1

    await asyncio.gather(*(work() for _ in range(5)))
    assert peak == 2


@pytest.mark.asyncio
async def test_limiter_enforces_min_interval_spacing():
    clock = FakeClock()
    limiter = ProviderLimiter(1, 0.5, clock, fake_sleep(clock))
    entries: list[float] = []

    async def work():
        async with limiter:
            entries.append(clock.monotonic())
            await fake_sleep(clock)(0.1)

    for _ in range(3):
        await work()

    assert entries[1] - entries[0] >= 0.5 - 1e-9
    assert entries[2] - entries[1] >= 0.5 - 1e-9


# --------------------------------------------------------------- T-PRICE-01


def _entry(pricing) -> ModelEntry:
    return ModelEntry(provider="mock", capability="llm", model_id="mock-x", pricing=pricing)


def test_per_1m_tokens_arabic_worst_case():
    entry = _entry(Per1mTokens(unit="per_1m_tokens", in_usd=Decimal("2.0"), out_usd=Decimal("4.0")))
    arabic_text = "مرحبا بالعالم " * 13  # 182 chars -> 91 worst-case tokens
    req = LLMRequest(system="", prompt=arabic_text, max_tokens=100)
    est = estimate_cost_musd(entry, req)
    # ceil(182 * 0.5) = 91 input tokens; (91*2 + 100*4)/1e6 USD
    expected = usd_to_musd(Decimal(91) * Decimal("2.0") / Decimal(1_000_000) + Decimal(100) * Decimal("4.0") / Decimal(1_000_000))
    assert est == expected


def test_per_1k_chars_estimator():
    entry = _entry(Per1kChars(unit="per_1k_chars", usd=Decimal("0.25")))
    req = TTSRequest(text="a" * 2000, voice_id="v", language=Language.AR)
    assert estimate_cost_musd(entry, req) == usd_to_musd(Decimal("0.5"))


def test_per_image_estimator():
    entry = _entry(PerImage(unit="per_image", usd=Decimal("1.5")))
    req = MediaRequest(capability="image_reference_generate", params={"n_images": 2}, aspect=Aspect.TALL)
    assert estimate_cost_musd(entry, req) == usd_to_musd(Decimal("3.0"))


def test_per_second_estimator():
    entry = _entry(PerSecond(unit="per_second", usd=Decimal("2.0")))
    req = MediaRequest(capability="image_to_video", duration_s=5.0, aspect=Aspect.WIDE)
    assert estimate_cost_musd(entry, req) == usd_to_musd(Decimal("10.0"))


def test_per_1m_tokens_exact_boundary():
    entry = _entry(Per1mTokens(unit="per_1m_tokens", in_usd=Decimal("1.0"), out_usd=Decimal("1.0")))
    req = LLMRequest(system="", prompt="x" * 2, max_tokens=0)
    # ceil(2 * 0.5) = 1 input token, 0 output -> exactly 0.000001 USD
    assert estimate_cost_musd(entry, req) == 1


# --------------------------------------------------------------- T-MONEY-01


def test_usd_to_musd_rounds_half_up():
    assert usd_to_musd(Decimal("0.0000015")) == 2  # 1.5 musd rounds up, not banker's rounding
    assert usd_to_musd(Decimal("0.0000025")) == 3
    assert usd_to_musd(Decimal("1.0000005")) == 1_000_001
    assert usd_to_musd(Decimal("0")) == 0


def test_musd_round_trip():
    assert musd_to_usd(1_500_000) == Decimal("1.5")
    assert usd_to_musd(musd_to_usd(999_123)) == 999_123


def test_money_amounts_are_integer_typed(ledger, repos):
    from studio.db.repos.providers import NewCall
    from studio.domain.refs import AssetRef  # noqa: F401 - ensure module import works

    call = NewCall(
        episode_id=None,
        stage_run_id=None,
        capability="llm",
        model_key="mock.llm",
        provider="mock",
        purpose="test",
        cache_key="c" * 64,
        request_json="{}",
        est_cost_musd=123,
    )
    inserted = ledger.begin_call(call)
    assert isinstance(inserted.est_cost_musd, int)
    rows = repos.providers.reserve_scopes_for_call(inserted.id)
    assert rows, "expected reserve scope rows"
    total = repos.budgets.held_musd("month", "2026-01")
    assert isinstance(total, int)
    ledger.fail_call(inserted.id, "permanent", "cleanup")
