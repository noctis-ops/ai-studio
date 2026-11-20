"""T-FAL-01..06, T-FAL-LIVE-01/02 (PHASE4 §P fal adapter tests).

Offline only: httpx.MockTransport + mock providers. No live fal call ever
happens here; the live smokes are opt-in via STUDIO_LIVE_FAL=1 and skipped
by default.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest
from conftest import make_home

from studio.config.loader import load_all
from studio.config.registry import (
    MediaSpec,
    ModelEntry,
    PerImage,
    PerMegapixel,
    PerSecond,
    PerVideo,
)
from studio.core.budget import LedgerService
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.core.gateway import CallContext, ProviderGateway
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import Aspect
from studio.domain.errors import (
    ContentPolicyError,
    MediaJobFailedError,
    PermanentProviderError,
    ProviderQuotaExceeded,
    TransientProviderError,
    ZeroByteAssetError,
)
from studio.providers.base import JobHandle, JobState, MediaRequest
from studio.providers.fal.adapter import FalJobProvider, map_http_error

SECRET = "falkey-SECRETMARKER-0123456789abcdef"
SIGNED_QUERY = "token=SIGNEDMARKER0123456789"


def _spec(**overrides) -> MediaSpec:
    data = {
        "supported_aspects": ["9:16", "16:9"],
        "aspect_values": {"9:16": "portrait_9_16", "16:9": "landscape_16_9"},
        "aspect_extra_params": {"9:16": {"guidance_scale": 3.5}},
        "supported_durations_s": [5, 10],
        "max_refs": 4,
        "ref_style": "list",
        "param_map": {
            "prompt": "prompt",
            "refs": "image_urls",
            "image": "image_url",
            "aspect": "aspect_ratio",
            "duration": "duration",
            "seed": "seed",
            "negative": "negative_prompt",
        },
        "param_formats": {"duration": "str_s"},
        "fixed_params": {"safety_checker": True},
        "result_map": {"asset_url": "images.0.url", "nsfw_flags": "has_nsfw_concepts"},
        "input_transport": "data_uri",
        "prompt_templates": {"ref_clause": "match reference {n}"},
    }
    data.update(overrides)
    return MediaSpec(**data)


def _entry(*, kind: str = "image", spec: MediaSpec | None = None, pricing=None) -> ModelEntry:
    if kind == "image":
        capability = "image_reference_generate"
        default_pricing: PerImage | PerMegapixel = PerImage(unit="per_image", usd="0.05")
    else:
        capability = "image_to_video"
        default_pricing = PerSecond(unit="per_second", usd="0.40")
    return ModelEntry(
        provider="fal",
        capability=capability,
        endpoint_id="test-vendor/test-model",
        pricing=pricing or default_pricing,
        pricing_verified_on=date(2026, 11, 1),
        commercial_use_verified=True,
        supports_refs=True,
        media=spec or _spec(),
    )


def _request(**overrides) -> MediaRequest:
    data = {
        "capability": "image_reference_generate",
        "params": {"prompt": "a lighthouse at dawn"},
        "aspect": Aspect.TALL,
        "seed": 42,
    }
    data.update(overrides)
    return MediaRequest(**data)


def _provider(entry, transport, **kwargs) -> FalJobProvider:
    return FalJobProvider(
        entry,
        kind="image" if entry.capability == "image_reference_generate" else "video",
        api_key=SECRET,
        transport=transport,
        **kwargs,
    )


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _ok_transport(*, status_payload=None, response_payload=None, asset_bytes=PNG_BYTES,
                  asset_content_type="image/png", submit_capture=None, poll_sequence=None):
    polls = list(poll_sequence or ["COMPLETED"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/test-vendor/test-model":
            if submit_capture is not None:
                submit_capture.append(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "request_id": "req-123",
                    "status_url": "https://queue.fal.run/test-vendor/test-model/requests/req-123/status",
                    "response_url": "https://queue.fal.run/test-vendor/test-model/requests/req-123?"
                                    + SIGNED_QUERY,
                    "cancel_url": "https://queue.fal.run/test-vendor/test-model/requests/req-123/cancel",
                },
            )
        if request.method == "GET" and path.endswith("/status"):
            status = polls.pop(0) if polls else "COMPLETED"
            if status == "COMPLETED":
                return httpx.Response(200, json=status_payload or {"status": "COMPLETED"})
            return httpx.Response(200, json={"status": status})
        if request.method == "GET" and path.endswith("/requests/req-123"):
            payload = response_payload or {"images": [{"url": "https://cdn.fal.run/out.png?" + SIGNED_QUERY}]}
            return httpx.Response(200, json=payload)
        if request.method == "GET" and path == "/out.png":
            return httpx.Response(200, content=asset_bytes,
                                  headers={"content-type": asset_content_type})
        return httpx.Response(404, json={"detail": f"unhandled {request.method} {path}"})

    return httpx.MockTransport(handler)


# ======================================================================
# T-FAL-01 — request building
# ======================================================================


def test_fal_01_request_building_maps_params_and_fixes_pricing_knobs():
    entry = _entry()
    captured: list[dict] = []

    async def run():
        provider = _provider(entry, _ok_transport(submit_capture=captured))
        req = _request(params={
            "prompt": "a lighthouse at dawn",
            "negative": "nope",
            "duration": 5,
            "refs": ["https://data.invalid/a.jpg"],
            "safety_checker": False,  # attempt to disable: must be ignored
        })
        await provider.submit(req)

    import asyncio
    asyncio.run(run())

    assert captured, "submit was never called"
    body = captured[0]
    # param_map + aspect_values + aspect_extra_params applied
    assert body["prompt"] == "a lighthouse at dawn"
    assert body["negative_prompt"] == "nope"
    assert body["aspect_ratio"] == "portrait_9_16"
    assert body["guidance_scale"] == 3.5
    # duration formatted per param_formats (str_s)
    assert body["duration"] == "5s"
    # refs passed through untouched (https URLs, no upload)
    assert body["image_urls"] == ["https://data.invalid/a.jpg"]
    # fixed_params win over request params — safety checker is never disabled
    assert body["safety_checker"] is True
    assert body["seed"] == 42


def test_fal_01_fixed_params_cannot_be_overridden_by_request():
    entry = _entry()
    provider = _provider(entry, httpx.MockTransport(lambda r: httpx.Response(500)))
    req = _request(params={"prompt": "x", "safety_checker": False, "seed": 1})
    body = provider.build_body(req)
    assert body["safety_checker"] is True


def test_fal_01_unconfirmed_aspect_value_fails_closed():
    spec = _spec(aspect_values={"9:16": "<CONFIRM>", "16:9": "landscape_16_9"})
    entry = _entry(spec=spec)
    provider = _provider(entry, httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(Exception, match="aspect value"):
        provider.build_body(_request())


# ======================================================================
# T-FAL-02 — poll lifecycle, result extraction, CAS store, timeout/resume
# ======================================================================


def test_fal_02_submit_poll_fetch_downloads_into_payload():
    import asyncio

    async def run():
        provider = _provider(_entry(), _ok_transport(poll_sequence=["IN_QUEUE", "IN_PROGRESS", "COMPLETED"]))
        handle = await provider.submit(_request())
        assert handle.remote_request_id == "req-123"
        assert handle.meta["status_url"].endswith("/status")
        for _ in range(2):
            state = await provider.poll(handle)
            assert state.state in ("queued", "running")
        state = await provider.poll(handle)
        assert state.state == "succeeded"
        payloads = await provider.fetch(handle)
        assert payloads[0].mime == "image/png"
        assert payloads[0].local_path is not None
        assert payloads[0].local_path.read_bytes().startswith(b"\x89PNG")

    asyncio.run(run())


def test_fal_02_poll_timeout_raises_transient_and_resume_keeps_request_id(
    tmp_path, monkeypatch
):
    import asyncio

    home = make_home(
        tmp_path,
        registry_overrides={
            "models": {
                "fal.img.test": {
                    "provider": "fal",
                    "capability": "image_reference_generate",
                    "endpoint_id": "test-vendor/test-model",
                    "pricing": {"unit": "per_image", "usd": 0.05},
                    "pricing_verified_on": "2026-11-01",
                    "commercial_use_verified": True,
                    "supports_refs": True,
                    "media": _spec().model_dump(mode="json"),
                },
            }
        },
        filename="fal02",
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    monkeypatch.setenv("FAL_KEY", SECRET)
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    from studio.core.ids import SequentialIds

    ids = SequentialIds("fal02")
    _repos = build_repos(db, clock, ids)
    gateway = ProviderGateway(
        db=db,
        settings=config.settings,
        registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock,
        ids=ids,
        providers={
            "fal.img.test": _provider(
                config.registry.models["fal.img.test"],
                _ok_transport(poll_sequence=["IN_QUEUE"] * 50),
            ),
        },
        poll_interval_s=0.0,
        max_polls=3,
        sleep=no_op_sleep,
    )

    req = _request()
    ctx = CallContext(episode_id=None, stage_run_id=None, purpose="test", nonce="n1")

    async def run():
        with pytest.raises(TransientProviderError, match="did not finish after 3 polls"):
            await gateway.image("fal.img.test", req, ctx)
        # resume: same cache key adopts the submitted call; submit runs once
        with pytest.raises(TransientProviderError, match="did not finish after 3 polls"):
            await gateway.image("fal.img.test", req, ctx)

    asyncio.run(run())

    rows = db.connect().execute(
        "SELECT status, remote_request_id, remote_handle_json, attempt FROM provider_calls"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "submitted"
    assert row["remote_request_id"] == "req-123"
    # the whole fal handle (status/response/cancel URLs) persisted in ONE statement
    meta = json.loads(row["remote_handle_json"])
    assert meta["status_url"].endswith("/status")
    assert meta["response_url"].startswith("https://queue.fal.run/")
    assert meta["cancel_url"].endswith("/cancel")


# ======================================================================
# T-FAL-03 — error table
# ======================================================================


@pytest.mark.parametrize(
    ("status_code", "body", "exc_type"),
    [
        (429, b'{"detail": "slow down"}', TransientProviderError),
        (429, b'{"detail": "rate limited"}', TransientProviderError),
        (500, b"internal error", TransientProviderError),
        (503, b"unavailable", TransientProviderError),
        (403, b'{"detail": "account balance exhausted"}', ProviderQuotaExceeded),
        (403, b'{"detail": "insufficient credit balance"}', ProviderQuotaExceeded),
        (401, b'{"detail": "bad key"}', PermanentProviderError),
        (403, b'{"detail": "not authorized"}', PermanentProviderError),
        (422, b'{"detail": [{"type": "content_policy_violation", "msg": "nsfw"}]}',
         ContentPolicyError),
        (400, b'{"detail": "bad request"}', PermanentProviderError),
        (404, b'{"detail": "no such model"}', PermanentProviderError),
    ],
)
def test_fal_03_http_error_taxonomy(status_code, body, exc_type):
    err = map_http_error(status_code, body)
    assert isinstance(err, exc_type)


def test_fal_03_422_policy_is_not_billed():
    err = map_http_error(
        422, b'{"detail": [{"type": "content_policy_violation"}]}'
    )
    assert isinstance(err, ContentPolicyError)
    assert err.billed is False


def test_fal_03_timeout_maps_to_transient():
    class _TimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.TimeoutException("timed out", request=request)

    import asyncio

    async def run():
        provider = _provider(_entry(), _TimeoutTransport())
        with pytest.raises(TransientProviderError):
            await provider.submit(_request())

    asyncio.run(run())


def test_fal_03_completed_with_error_raises_job_failed():
    import asyncio

    transport = _ok_transport(status_payload={"status": "COMPLETED", "error": "moderation refused"})

    async def run():
        provider = _provider(_entry(), transport)
        handle = await provider.submit(_request())
        with pytest.raises(MediaJobFailedError, match="moderation refused"):
            await provider.poll(handle)

    asyncio.run(run())


def test_fal_03_nsfw_flag_billed_and_nothing_downloaded():
    import asyncio

    transport = _ok_transport(
        response_payload={
            "images": [{"url": "https://cdn.fal.run/out.png"}],
            "has_nsfw_concepts": [True],
        }
    )

    async def run():
        provider = _provider(_entry(), transport)
        handle = await provider.submit(_request())
        with pytest.raises(ContentPolicyError) as excinfo:
            await provider.fetch(handle)
        assert excinfo.value.billed is True

    asyncio.run(run())


def test_fal_03_empty_download_raises_zero_byte():
    import asyncio

    transport = _ok_transport(asset_bytes=b"")

    async def run():
        provider = _provider(_entry(), transport)
        handle = await provider.submit(_request())
        with pytest.raises(ZeroByteAssetError):
            await provider.fetch(handle)

    asyncio.run(run())


def test_fal_03_wrong_content_type_rejected():
    import asyncio

    transport = _ok_transport(asset_bytes=b"plain text", asset_content_type="text/plain")

    async def run():
        provider = _provider(_entry(), transport)
        handle = await provider.submit(_request())
        with pytest.raises(PermanentProviderError, match="content-type"):
            await provider.fetch(handle)

    asyncio.run(run())


# ======================================================================
# T-FAL-04 — secrets never leak (request_json, audit, caplog, exceptions)
# ======================================================================


def test_fal_04_key_and_signed_urls_never_persisted_or_logged(
    tmp_path, monkeypatch, caplog
):
    import asyncio

    home = make_home(
        tmp_path,
        registry_overrides={
            "models": {
                "fal.img.test": {
                    "provider": "fal",
                    "capability": "image_reference_generate",
                    "endpoint_id": "test-vendor/test-model",
                    "pricing": {"unit": "per_image", "usd": 0.05},
                    "pricing_verified_on": "2026-11-01",
                    "commercial_use_verified": True,
                    "supports_refs": True,
                    "media": _spec().model_dump(mode="json"),
                },
            }
        },
        filename="fal04",
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    monkeypatch.setenv("FAL_KEY", SECRET)
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    from studio.core.ids import SequentialIds

    ids = SequentialIds("fal04")
    gateway = ProviderGateway(
        db=db,
        settings=config.settings,
        registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock,
        ids=ids,
        providers={"fal.img.test": _provider(config.registry.models["fal.img.test"],
                                             _ok_transport())},
        poll_interval_s=0.0,
        sleep=no_op_sleep,
    )
    req = _request(params={"prompt": "a lighthouse", "negative": "blurry"})
    ctx = CallContext(episode_id=None, stage_run_id=None, purpose="audition",
                      nonce="n", gate_exempt="audition")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(gateway.image("fal.img.test", req, ctx))

    raw_db = (home / "data" / "studio.db").read_bytes()
    assert SECRET.encode() not in raw_db
    assert b"SIGNEDMARKER" not in raw_db
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert "SIGNEDMARKER" not in record.getMessage()


def test_fal_04_error_messages_scrub_signed_url_query():
    err = map_http_error(
        403, f'{{"detail": "denied {SIGNED_QUERY} for https://cdn.fal.run/x.png?token=abc123"}}'.encode()
    )
    text = str(err)
    assert "SIGNEDMARKER" not in text
    assert "abc123" not in text


def test_fal_04_job_failed_message_scrubs_signed_urls():
    import asyncio

    transport = _ok_transport(
        status_payload={"status": "COMPLETED",
                        "error": f"fetch failed at https://cdn.fal.run/x.png?{SIGNED_QUERY}"}
    )

    async def run():
        provider = _provider(_entry(), transport)
        handle = await provider.submit(_request())
        with pytest.raises(MediaJobFailedError) as excinfo:
            await provider.poll(handle)
        assert "SIGNEDMARKER" not in str(excinfo.value)

    asyncio.run(run())


# ======================================================================
# T-FAL-05 — data URI guard, upload path, deterministic ref prep
# ======================================================================


def test_fal_05_inline_data_uri_size_guard(tmp_path, monkeypatch):
    home = make_home(tmp_path, settings_overrides={"media": {"max_inline_bytes": 64}},
                     filename="fal05guard")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    entry = _entry()
    provider = FalJobProvider(entry, kind="image", settings=config.settings,
                              api_key=SECRET,
                              transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    big = "data:image/jpeg;base64," + "A" * 256
    with pytest.raises(Exception, match="max_inline_bytes"):
        provider.build_body(_request(params={"prompt": "x", "image": big}))


def test_fal_05_upload_transport_uploads_then_references(tmp_path):
    import asyncio

    calls: list[tuple[str, str]] = []
    local_file = tmp_path / "ref.jpg"
    local_file.write_bytes(b"\xff\xd8\xff" + b"1" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/upload":
            calls.append(("upload", request.url.params.get("file_name", "")))
            return httpx.Response(200, json={"url": "https://files.fal.run/uploaded-ref.jpg"})
        if request.method == "POST" and path == "/test-vendor/test-model":
            calls.append(("submit", request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "request_id": "req-u",
                    "status_url": "https://queue.fal.run/test-vendor/test-model/requests/req-u/status",
                    "response_url": "https://queue.fal.run/test-vendor/test-model/requests/req-u/response",
                    "cancel_url": "https://queue.fal.run/test-vendor/test-model/requests/req-u/cancel",
                },
            )
        return httpx.Response(404, json={})

    entry = _entry(spec=_spec(input_transport="upload"))
    provider = _provider(entry, httpx.MockTransport(handler))

    async def run():
        await provider.submit(_request(params={"prompt": "x", "refs": [str(local_file)]}))

    asyncio.run(run())
    assert calls[0] == ("upload", "ref.jpg")
    submit_body = json.loads(calls[1][1])
    assert submit_body["image_urls"] == ["https://files.fal.run/uploaded-ref.jpg"]


@pytest.mark.ffmpeg
def test_fal_05_prepare_refs_is_deterministic(tmp_path, monkeypatch):
    import asyncio

    from studio.media.refs import prepare_refs
    from studio.video.ffmpeg import run_ffmpeg

    home = make_home(tmp_path, filename="fal05refs")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    cas = ContentStore(Path(config.settings.paths.cas), db, clock)

    src = tmp_path / "src.png"

    async def make_and_prepare():
        await run_ffmpeg(
            ["-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=1",
             "-frames:v", "1", str(src)],
            timeout_s=60.0,
        )
        ref = cas.put_file(src, kind="image", ext="png", retention="intermediate")
        first = await prepare_refs(cas, [ref], config.settings)
        second = await prepare_refs(cas, [ref], config.settings)
        return first[0], second[0]

    a, b = asyncio.run(make_and_prepare())
    assert a.sha256 == b.sha256
    assert a.data_uri.startswith("data:image/jpeg;base64,")
    assert a.data_uri == b.data_uri


# ======================================================================
# T-FAL-06 — cost per unit, failed_job_billing charge vs free
# ======================================================================


def test_fal_06_cost_per_unit_pricing_math():
    from studio.core.pricing import estimate_cost_musd

    image = _entry(pricing=PerImage(unit="per_image", usd="0.05"))
    req = _request(params={"prompt": "x", "n_images": 3})
    assert estimate_cost_musd(image, req) == 150_000

    video = _entry(kind="video", pricing=PerSecond(unit="per_second", usd="0.40"))
    vreq = MediaRequest(capability="image_to_video", params={"prompt": "x"},
                        aspect=Aspect.TALL, duration_s=5.0, seed=1)
    assert estimate_cost_musd(video, vreq) == 2_000_000

    mp = _entry(pricing=PerMegapixel(unit="per_megapixel", usd="0.01"))
    mreq = _request(params={"prompt": "x", "width": 1080, "height": 1920})
    # 2.0736 MP * $0.01 = $0.020736 = 20736 micro-USD
    assert estimate_cost_musd(mp, mreq) == 20_736

    flat = _entry(pricing=PerVideo(unit="per_video", usd="0.75"))
    assert estimate_cost_musd(flat, vreq) == 750_000


def _failed_job_provider():
    class _FailedJob(FalJobProvider):
        async def poll(self, handle: JobHandle) -> JobState:
            raise MediaJobFailedError("fal job failed: moderation refused")

    return _FailedJob(_entry(), kind="image", api_key=SECRET,
                      transport=_ok_transport())


def _ledger_settles(db, call_id):
    rows = db.connect().execute(
        "SELECT entry_type, amount_musd FROM cost_ledger WHERE call_id = ?", (call_id,)
    ).fetchall()
    return [(r["entry_type"], r["amount_musd"]) for r in rows]


@pytest.mark.parametrize(
    ("billing", "expect_settle"),
    [("charge", 50_000), ("free", 0)],
)
def test_fal_06_failed_job_billing_charge_vs_free(
    tmp_path, monkeypatch, billing, expect_settle
):
    import asyncio

    home = make_home(
        tmp_path,
        settings_overrides={"media": {"failed_job_billing": billing}},
        registry_overrides={
            "models": {
                "fal.img.test": {
                    "provider": "fal",
                    "capability": "image_reference_generate",
                    "endpoint_id": "test-vendor/test-model",
                    "pricing": {"unit": "per_image", "usd": 0.05},
                    "pricing_verified_on": "2026-11-01",
                    "commercial_use_verified": True,
                    "supports_refs": True,
                    "media": _spec().model_dump(mode="json"),
                },
            }
        },
        filename=f"fal06_{billing}",
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    monkeypatch.setenv("FAL_KEY", SECRET)
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    from studio.core.ids import SequentialIds

    ids = SequentialIds(f"fal06{billing}")
    gateway = ProviderGateway(
        db=db,
        settings=config.settings,
        registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock,
        ids=ids,
        providers={"fal.img.test": _failed_job_provider()},
        poll_interval_s=0.0,
        sleep=no_op_sleep,
    )
    req = _request()
    ctx = CallContext(episode_id=None, stage_run_id=None, purpose="audition",
                      nonce="n", gate_exempt="audition")

    with pytest.raises(MediaJobFailedError):
        asyncio.run(gateway.image("fal.img.test", req, ctx))

    call = db.connect().execute(
        "SELECT id, status, actual_cost_musd FROM provider_calls"
    ).fetchone()
    assert call["status"] == "failed"
    settles = [amount for kind, amount in _ledger_settles(db, call["id"]) if kind == "settle"]
    if expect_settle:
        assert call["actual_cost_musd"] == 50_000
        assert settles and all(amount == 50_000 for amount in settles)
    else:
        assert call["actual_cost_musd"] == 0
        assert settles == []


# ======================================================================
# T-FAL-LIVE-01/02 — opt-in live smoke (never runs by default)
# ======================================================================


@pytest.mark.skipif(
    __import__("os").environ.get("STUDIO_LIVE_FAL") != "1",
    reason="opt-in live fal smoke (STUDIO_LIVE_FAL=1)",
)
def test_fal_live_01_one_image_under_quarter_dollar():
    """Opt-in: one image via fal.ai, under $0.25. Requires FAL_KEY and a
    confirmed registry entry; never executed in CI."""
    import os

    assert os.environ.get("FAL_KEY"), "FAL_KEY required for the live smoke"
    config = load_all(Path(os.environ["STUDIO_HOME"]))
    from studio.media.registry import media_model_entry, model_confirmation_problems

    entry = media_model_entry(config, config.registry.resolve_profile("draft"), "keyframes")
    assert not model_confirmation_problems(entry), "registry entries must be confirmed first"


@pytest.mark.skipif(
    __import__("os").environ.get("STUDIO_LIVE_FAL") != "1",
    reason="opt-in live fal smoke (STUDIO_LIVE_FAL=1)",
)
def test_fal_live_02_shortest_clip_under_dollar_fifty():
    """Opt-in: one shortest clip via fal.ai, under $1.50. Requires FAL_KEY
    and a confirmed registry entry; never executed in CI."""
    import os

    assert os.environ.get("FAL_KEY"), "FAL_KEY required for the live smoke"
    config = load_all(Path(os.environ["STUDIO_HOME"]))
    from studio.media.registry import media_model_entry, model_confirmation_problems

    entry = media_model_entry(config, config.registry.resolve_profile("draft"), "clips")
    assert not model_confirmation_problems(entry), "registry entries must be confirmed first"
