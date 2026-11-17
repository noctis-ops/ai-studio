"""T-EL-* (PHASE3 §I) — offline ElevenLabs adapter tests.

No network: `httpx.MockTransport` simulates the ElevenLabs API.
STUDIO_LIVE_TTS stays unset in CI; T-EL-06 is opt-in and skipped otherwise.
"""

from __future__ import annotations

import base64
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import build_gateway, make_home

from studio.config.loader import load_all
from studio.core.clock import FakeClock
from studio.core.gateway import CallContext
from studio.core.pricing import actual_cost_musd, estimate_cost_musd
from studio.db import Database, migrate
from studio.domain.enums import Language
from studio.domain.errors import (
    ConfigError,
    PermanentProviderError,
    ProviderQuotaExceeded,
    TransientProviderError,
    ZeroByteAssetError,
)
from studio.domain.money import usd_to_musd
from studio.providers.base import TTSRequest
from studio.providers.tts.elevenlabs import (
    DEFAULT_BASE_URL,
    ElevenLabsTTSProvider,
)

API_KEY = "xi-marker-secret-aaaaabbbbbcccccc"
AR_SENTENCE = "مرحبا بكم في اختبار الاستوديو."


def _config(tmp_path, monkeypatch, filename="el"):
    home = make_home(tmp_path, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    return load_all(home)


def _entry(config, model_key="tts.main"):
    return config.registry.models[model_key]


def _request(**kwargs) -> TTSRequest:
    base: dict[str, Any] = {
        "text": AR_SENTENCE,
        "voice_id": "voice-ar-0001",
        "language": Language.AR,
        "settings": {"stability": 0.5, "similarity_boost": 0.75, "speaker_boost": True, "seed": 42},
        "previous_text": "السياق السابق",
        "next_text": "السياق اللاحق",
    }
    base.update(kwargs)
    return TTSRequest(**base)


_MINIMAL_MP3 = base64.b64decode(
    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYzLjEuMTAyAAAAAAAAAAAAAAD/83DAAAAAAAAAAAAAWGluZwAAAA8AAAAKAAADJwCMjIyMjIyMjIytra2tra2tra2ttbW1tbW1tbW1tb6+vr6+vr6+vr7GxsbGxsbGxsbGzs7Ozs7Ozs7OztbW1tbW1tbW1tbf39/f39/f39/f9/f39/f39/f39/////////////8AAAAATGF2YzYzLjEuAAAAAAAAAAAAAAAAJAMGAAAAAAAAAycFBhTaAAAAAAAAAAAAAAAAAP/zkMQABaAG3mFBAAGSEJ3rNGHh5+GYePPwQADv8PDw/+AAhTaBYCFlAHAIAOBgOA0pY2kpclVUAwozYYdRhJhAsDpywxM+nDmkmsgGgCQCv4SgCwBb8F0CiAqIL/EUIohHo9+DQVBVn5YKgqIgqgkwaNAAD5yI6HAXAJLTstUAMPBfDA7qADDwTwLhJWoAMPBfAoEVqkxBTUUAMPBPAuElagswiOAAD6rPRgkAuNAQk6IVIGMBcLRKJBAANpOM4TkiMMwKBWWJn17sqqiViHwO0UsUts8A7D5xxmwyo5vFwEUNDIixFvN+YkVIqZfTdBBkjJYK8uHwQAdMQU1MQU1FNC4xIP/zQMThEqjWrxmaUAAoYWxwaGEgMClVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//MQxPED6D7nAccAAVVVVVVVVVVVVVVVVVX/8xDE7gGoOu6AAdIkVVVVVVVVVVVVVVVVVf/zEMT0AdA26oAB0iVVVVVVVVVVVVVVVVVV//MQxPkB0DbqgAHSJVVVVVVVVVVVVVVVVVX/8xDE+gHQNuqAAdIlVVVVVVVVVVVVVVVVVf/zEMT/A8A66wFHAABVVVVVVVVVVVVVVVVV//MwxP0SOPbK+ZqAJFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//MQxPIAAAP8AcAAAFVVVVVVVVVVVVVVVVU="
)


class _Recorder:
    def __init__(self, *, status: int = 200, body: bytes = _MINIMAL_MP3,
                 headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = {"content-type": "audio/mpeg", **(headers or {})}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, content=self.body, headers=self.headers)


def _provider(config, recorder, *, model_key="tts.main", timeout_s=30.0):
    entry = _entry(config, model_key)
    return ElevenLabsTTSProvider(
        entry, api_key=API_KEY, timeout_s=timeout_s,
        transport=httpx.MockTransport(recorder),
    )


# ---------------------------------------------------------------- T-EL-01

@pytest.mark.asyncio
async def test_request_url_and_body_multilingual_v2(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    recorder = _Recorder(headers={"character-cost": "17", "request-id": "req-abc-1"})
    provider = _provider(config, recorder)
    result = await provider.synthesize(_request())

    (request,) = recorder.requests
    assert request.url.path == "/v1/text-to-speech/voice-ar-0001"
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert request.headers["xi-api-key"] == API_KEY
    assert request.method == "POST"

    body = json.loads(request.content)
    assert body["text"] == AR_SENTENCE
    assert body["model_id"] == "eleven_multilingual_v2"
    assert body["voice_settings"]["stability"] == 0.5
    assert body["voice_settings"]["similarity_boost"] == 0.75
    assert body["voice_settings"]["use_speaker_boost"] is True
    assert body["seed"] == 42
    # tts.main supports_context=true, supports_language_code=false
    assert body["previous_text"] == "السياق السابق"
    assert body["next_text"] == "السياق اللاحق"
    assert "language_code" not in body

    assert result.billed_chars == 17
    assert result.meta["request_id"] == "req-abc-1"
    assert result.payload.local_path is not None and result.payload.local_path.exists()
    payload_size = result.payload.local_path.stat().st_size
    assert payload_size == len(recorder.body)


@pytest.mark.asyncio
async def test_request_body_flash_language_code(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="elflash")
    recorder = _Recorder(headers={"character-cost": "3"})
    provider = _provider(config, recorder, model_key="tts.candidate.flash")
    await provider.synthesize(_request())

    (request,) = recorder.requests
    body = json.loads(request.content)
    assert body["model_id"] == "eleven_flash_v2_5"
    assert body["language_code"] == "ar"
    # supports_context=false -> context is never sent
    assert "previous_text" not in body
    assert "next_text" not in body


@pytest.mark.asyncio
async def test_context_omitted_when_none(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="elctx")
    recorder = _Recorder()
    provider = _provider(config, recorder)
    await provider.synthesize(_request(previous_text=None, next_text=None))
    (request,) = recorder.requests
    body = json.loads(request.content)
    assert "previous_text" not in body
    assert "next_text" not in body


@pytest.mark.asyncio
async def test_seed_must_be_an_integer(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="elseed")
    recorder = _Recorder()
    provider = _provider(config, recorder)
    with pytest.raises(PermanentProviderError, match="seed"):
        await provider.synthesize(_request(settings={"seed": "not-an-int"}))
    assert recorder.requests == []  # rejected before touching the network


# ---------------------------------------------------------------- T-EL-02

def _error_recorder(status: int, detail_status: str | None, message: str = "boom"):
    def handler(request: httpx.Request) -> httpx.Response:
        if detail_status is None:
            payload: Any = {"detail": message}
        else:
            payload = {"detail": {"status": detail_status, "message": message}}
        return httpx.Response(status, json=payload)

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "detail_status", "expected"),
    [
        (400, "quota_exceeded", ProviderQuotaExceeded),
        (401, "quota_exceeded", ProviderQuotaExceeded),
        (429, "quota_exceeded", ProviderQuotaExceeded),  # quota outranks the 429 transient bucket
        (429, None, TransientProviderError),
        (429, "rate_limit_exceeded", TransientProviderError),  # unknown 429 bucket stays transient
        (500, None, TransientProviderError),
        (503, "too_many_concurrent_requests", TransientProviderError),
        (500, "system_busy", TransientProviderError),
        (400, "voice_not_found", PermanentProviderError),
        (402, None, PermanentProviderError),
        (403, None, PermanentProviderError),
        (404, "not_found", PermanentProviderError),
        (422, None, PermanentProviderError),
    ],
)
async def test_error_mapping_table(tmp_path, monkeypatch, status, detail_status, expected):
    config = _config(tmp_path, monkeypatch, filename=f"elmap{status}{detail_status}")
    transport = httpx.MockTransport(_error_recorder(status, detail_status, f"msg-{status}"))
    provider = ElevenLabsTTSProvider(_entry(config), api_key=API_KEY, transport=transport)
    with pytest.raises(expected) as excinfo:
        await provider.synthesize(_request())
    message = str(excinfo.value)
    assert str(status) in message
    assert detail_status in message if detail_status else True
    assert "xi-marker-secret" not in message  # the key never rides along


@pytest.mark.asyncio
async def test_error_body_is_not_json(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="elbadjson")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"<html>bad gateway proxy</html>")

    provider = ElevenLabsTTSProvider(
        _entry(config), api_key=API_KEY, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PermanentProviderError):
        await provider.synthesize(_request())


@pytest.mark.asyncio
async def test_empty_200_body_raises_zero_byte(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="elempty")
    provider = ElevenLabsTTSProvider(
        _entry(config),
        api_key=API_KEY,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"")),
    )
    with pytest.raises(ZeroByteAssetError):
        await provider.synthesize(_request())


@pytest.mark.asyncio
async def test_timeout_and_connect_errors_are_transient(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="eltimeout")

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    provider = ElevenLabsTTSProvider(
        _entry(config), api_key=API_KEY, transport=httpx.MockTransport(timeout_handler)
    )
    with pytest.raises(TransientProviderError):
        await provider.synthesize(_request())

    def connect_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = ElevenLabsTTSProvider(
        _entry(config), api_key=API_KEY, transport=httpx.MockTransport(connect_handler)
    )
    with pytest.raises(TransientProviderError):
        await provider.synthesize(_request())


@pytest.mark.asyncio
async def test_missing_api_key_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    config = _config(tmp_path, monkeypatch, filename="elnokey")
    provider = ElevenLabsTTSProvider(_entry(config))
    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY"):
        await provider.synthesize(_request())


# ---------------------------------------------------------------- T-EL-03

@pytest.mark.asyncio
async def test_api_key_never_persisted_or_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ELEVENLABS_API_KEY", API_KEY)
    home = make_home(tmp_path, filename="elsecret")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    from studio.core.ids import SequentialIds

    ids = SequentialIds("el")
    recorder = _Recorder(headers={"character-cost": str(len(AR_SENTENCE)), "request-id": "req-sec-9"})
    entry = _entry(config)
    provider = ElevenLabsTTSProvider(
        entry, transport=httpx.MockTransport(recorder)
    )  # key comes from the env, never the constructor
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids,
                       providers={"tts.main": provider})
    with caplog.at_level("DEBUG"):
        await gw.tts(
            "tts.main",
            _request(),
            CallContext(episode_id=None, stage_run_id=None, nonce="n1", purpose="t-el-03"),
        )

    rows = db.connect().execute("SELECT request_json FROM provider_calls").fetchall()
    assert rows
    for row in rows:
        assert API_KEY not in (row["request_json"] or "")
    audit = db.connect().execute("SELECT payload_json FROM audit_events").fetchall()
    for row in audit:
        assert API_KEY not in (row["payload_json"] or "")
    assert API_KEY not in caplog.text
    # and nowhere in the raw database bytes either
    assert API_KEY.encode() not in db.path.read_bytes()


@pytest.mark.asyncio
async def test_exception_messages_never_contain_api_key(tmp_path, monkeypatch, caplog):
    # even if the *provider* mentions the key in its message, our typed error
    # mapping must not re-emit the local environment value
    monkeypatch.setenv("ELEVENLABS_API_KEY", API_KEY)
    home = make_home(tmp_path, filename="elsecret2")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"detail": {"status": "invalid_api_key", "message": "bad key"}}
        )

    provider = ElevenLabsTTSProvider(
        _entry(config), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PermanentProviderError) as excinfo:
        await provider.synthesize(_request())
    assert API_KEY not in str(excinfo.value)


# ---------------------------------------------------------------- T-EL-04

@pytest.mark.asyncio
async def test_billed_chars_header_valid(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="elbilled")
    recorder = _Recorder(headers={"character-cost": "123"})
    provider = _provider(config, recorder)
    result = await provider.synthesize(_request())
    assert result.billed_chars == 123
    entry = _entry(config)
    expected = (Decimal(123) * entry.pricing.usd) / Decimal(1000)
    assert actual_cost_musd(entry, _request(), result) == usd_to_musd(expected)
    assert isinstance(actual_cost_musd(entry, _request(), result), int)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["garbage", "-5", "", "3.5"])
async def test_billed_chars_header_invalid_falls_back_to_len(tmp_path, monkeypatch, raw):
    config = _config(tmp_path, monkeypatch, filename=f"elfallback{raw!r}")
    recorder = _Recorder(headers={"character-cost": raw})
    provider = _provider(config, recorder)
    result = await provider.synthesize(_request())
    assert result.billed_chars == len(AR_SENTENCE)


@pytest.mark.asyncio
async def test_billed_chars_header_absent_falls_back_to_len(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, filename="elnoheader")
    recorder = _Recorder()  # no character-cost header
    provider = _provider(config, recorder)
    result = await provider.synthesize(_request())
    assert result.billed_chars == len(AR_SENTENCE)


@pytest.mark.asyncio
async def test_settle_matches_ceil_chars_times_price(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", API_KEY)
    home = make_home(tmp_path, filename="elsettle")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    from studio.core.ids import SequentialIds

    ids = SequentialIds("el")
    text = "نص قصير جداً للاختبار"
    recorder = _Recorder(body=_MINIMAL_MP3,
                         headers={"character-cost": str(len(text)), "request-id": "req-settle-1"})
    provider = ElevenLabsTTSProvider(
        _entry(config), api_key=API_KEY, transport=httpx.MockTransport(recorder)
    )
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids,
                       providers={"tts.main": provider})
    ctx = CallContext(episode_id=None, stage_run_id=None, nonce="n1", purpose="t-el-04")
    await gw.tts("tts.main", _request(text=text), ctx)

    row = db.connect().execute(
        "SELECT status, actual_cost_musd, remote_request_id FROM provider_calls"
    ).fetchone()
    entry = _entry(config)
    expected = (Decimal(len(text)) * entry.pricing.usd) / Decimal(1000)
    expected_musd = usd_to_musd(expected)
    assert row["status"] == "completed"
    assert row["actual_cost_musd"] == expected_musd
    assert row["remote_request_id"] == "req-settle-1"
    assert isinstance(row["actual_cost_musd"], int)
    est = estimate_cost_musd(entry, _request(text=text))
    assert est == expected_musd  # worst-case estimate == actual for per_1k_chars on the same text


# ---------------------------------------------------------------- T-EL-05

@pytest.mark.asyncio
async def test_quota_exceeded_halts_run_without_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", API_KEY)
    home = make_home(tmp_path, filename="elquota")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    monkeypatch.delenv("FAL_KEY", raising=False)
    from studio.core.ids import SequentialIds

    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = SequentialIds("elquota")

    hits: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(
            429, json={"detail": {"status": "quota_exceeded", "message": "quota exhausted"}}
        )

    provider = ElevenLabsTTSProvider(
        _entry(config), api_key=API_KEY, transport=httpx.MockTransport(handler)
    )
    gateway = build_gateway(db=db, config=config, clock=clock, ids=ids,
                            providers={"mock.tts": provider})

    import json as _json

    from studio.core.clock import no_op_sleep
    from studio.db.repos import build_repos
    from studio.demo.seed import seed_demo
    from studio.demo.stages import demo_specs
    from studio.domain.enums import RunStatus
    from studio.pipeline.dag import Dag
    from studio.pipeline.engine import Engine
    from studio.pipeline.worker import Worker

    repos = build_repos(db, clock, ids)
    seeded = seed_demo(config, repos, languages=[Language.AR], aspects=["9:16"], ids=ids)
    resolved = config.registry.resolve_profile("sandbox")
    snapshot = config.registry.registry_snapshot(resolved)
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id,
        profile="sandbox",
        auto_approve=True,
        config_snapshot_json=_json.dumps(snapshot, sort_keys=True),
    )
    repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": run_row.id}, dedupe_key=f"advance:{run_row.id}"
    )
    engine = Engine(db=db, repos=repos, dag=Dag(demo_specs()), clock=clock,
                    config=config, ids=ids, gateway=gateway)
    worker = Worker(db, engine, repos, config, clock, worker_id="test-worker", sleep=no_op_sleep)
    await worker.run(until_idle=True)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.HALTED_BUDGET.value
    episode = db.connect().execute(
        "SELECT status FROM episodes WHERE id = ?", (seeded.episode_id,)
    ).fetchone()
    assert episode["status"] == "halted_budget"
    assert len(hits) == 1  # not retried
    audit = db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = 'budget_halted'"
    ).fetchall()
    reasons = [_json.loads(row["payload_json"]).get("reason") for row in audit]
    assert "provider_quota" in reasons


# ---------------------------------------------------------------- T-EL-06 (opt-in)

@pytest.mark.skipif(
    os.environ.get("STUDIO_LIVE_TTS") != "1" or not os.environ.get("ELEVENLABS_API_KEY"),
    reason=(
        "T-EL-06 is opt-in and spends real money (cap $0.50): set STUDIO_LIVE_TTS=1 "
        "and ELEVENLABS_API_KEY to run it"
    ),
)
async def test_t_el_06_live_elevenlabs(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, monkeypatch, filename="ellive")
    provider = ElevenLabsTTSProvider(
        _entry(config), api_key=os.environ["ELEVENLABS_API_KEY"], timeout_s=120.0
    )
    text = AR_SENTENCE
    result = await provider.synthesize(
        _request(text=text, previous_text=None, next_text=None)
    )
    assert result.payload.local_path is not None
    size = Path(result.payload.local_path).stat().st_size
    assert size > 0
    assert result.billed_chars >= 1
    entry = _entry(config)
    expected = usd_to_musd((Decimal(result.billed_chars) * entry.pricing.usd) / Decimal(1000))
    cap_musd = usd_to_musd(Decimal("0.50"))
    assert expected <= cap_musd, "synthetic sentence exceeded the $0.50 live cap"
    print(
        f"request-id={result.meta.get('request_id')} billed_chars={result.billed_chars} "
        f"cost={expected / 1_000_000} USD"
    )
    captured = capsys.readouterr()
    assert "request-id=" in captured.out


def test_elevenlabs_default_base_url_is_https(tmp_path, monkeypatch):
    assert DEFAULT_BASE_URL.startswith("https://")
    assert _provider(_config(tmp_path, monkeypatch, filename="elbetsurl"), _Recorder())._base_url == DEFAULT_BASE_URL
