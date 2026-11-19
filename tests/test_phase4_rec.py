"""T-REC-03: crash-recovery matrix for the fal job adapters (PHASE4 §P).

7 crash points x {fal image, fal video} through ``FalJobProvider`` +
httpx.MockTransport (never a live fal call). Asserts the zero-repay
invariants from BEGIN_PROMPT §H6/§I on the fal path specifically:

- only ``after_submit_before_persist`` re-pays (provider-side orphan), and logs
  exactly one ``potential_orphan`` audit;
- a resume after ``after_persist_id`` reuses the persisted
  ``remote_handle_json`` (verified with sentinel URLs, not fallback URLs);
- every reservation is released (settled exactly once per scope).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import httpx
import pytest
from conftest import PAID_MOCK_PRICES, build_gateway, make_home  # noqa: F401 (PAID_MOCK_PRICES re-exported for consistency)

from studio.config.loader import load_all
from studio.config.registry import MediaSpec, ModelEntry, PerImage, PerSecond
from studio.core.clock import FakeClock
from studio.core.faults import CRASH_POINTS, FaultInjector, SimulatedCrash
from studio.db import Database, migrate
from studio.domain.enums import Aspect
from studio.providers.base import MediaRequest
from studio.providers.fal.adapter import FalJobProvider

SECRET = "falkey-SECRETMARKER-0123456789abcdef"

# Registry entries the gateway itself looks up. The injected providers below
# carry their own ModelEntry; these exist so the gateway can price + cache-key
# the call. Prices are deliberately the T-FAL-01 values, not the sandbox zeros.
FAL_PRICES = {
    "models": {
        "fal.test.image": {
            "provider": "fal",
            "capability": "image_reference_generate",
            "endpoint_id": "test-vendor/test-model",
            "pricing": {"unit": "per_image", "usd": 0.05},
            "pricing_verified_on": "2026-11-01",
            "commercial_use_verified": True,
            "supports_refs": True,
            "needs_confirmation": False,
        },
        "fal.test.video": {
            "provider": "fal",
            "capability": "image_to_video",
            "endpoint_id": "test-vendor/test-model",
            "pricing": {"unit": "per_second", "usd": 0.40},
            "pricing_verified_on": "2026-11-01",
            "commercial_use_verified": True,
            "supports_refs": True,
            "needs_confirmation": False,
        },
    }
}

# expected provider submit_count after crash + resume, per crash point
_EXPECTED_SUBMITS = {
    "after_reserve": 1,
    "before_submit": 1,
    "after_submit_before_persist": 2,  # one provider-side orphan
    "after_persist_id": 1,
    "during_poll": 1,
    "after_fetch_before_store": 1,
    "after_store_before_complete": 1,
}

IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"rec3-{next(self._counter):06d}"


def _spec(*, kind: str) -> MediaSpec:
    if kind == "image":
        result_map = {"asset_url": "images.0.url", "nsfw_flags": "has_nsfw_concepts"}
    else:
        result_map = {"asset_url": "video.url", "nsfw_flags": "has_nsfw_concepts"}
    return MediaSpec(
        supported_aspects=["9:16", "16:9"],
        aspect_values={"9:16": "portrait_9_16", "16:9": "landscape_16_9"},
        supported_durations_s=[5, 10],
        max_refs=4,
        ref_style="list",
        param_map={
            "prompt": "prompt",
            "refs": "image_urls",
            "aspect": "aspect_ratio",
            "duration": "duration",
            "seed": "seed",
        },
        result_map=result_map,
        input_transport="data_uri",
    )


async def _video_asset_bytes(tmp_path: Path) -> bytes:
    from studio.video.ffmpeg import run_ffmpeg

    out = tmp_path / "fal-crash-video.mp4"
    await run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
         "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        timeout_s=60.0,
    )
    return out.read_bytes()


def _provider_entry(*, kind: str) -> ModelEntry:
    from datetime import date

    if kind == "image":
        return ModelEntry(
            provider="fal",
            capability="image_reference_generate",
            endpoint_id="test-vendor/test-model",
            pricing=PerImage(unit="per_image", usd="0.05"),
            pricing_verified_on=date(2026, 11, 1),
            commercial_use_verified=True,
            supports_refs=True,
            media=_spec(kind="image"),
        )
    return ModelEntry(
        provider="fal",
        capability="image_to_video",
        endpoint_id="test-vendor/test-model",
        pricing=PerSecond(unit="per_second", usd="0.40"),
        pricing_verified_on=date(2026, 11, 1),
        commercial_use_verified=True,
        supports_refs=True,
        media=_spec(kind="video"),
    )


def _request(*, kind: str) -> MediaRequest:
    if kind == "image":
        return MediaRequest(
            capability="image_reference_generate",
            params={"prompt": "crash matrix fal image"},
            aspect=Aspect.WIDE,
            seed=17,
        )
    return MediaRequest(
        capability="image_to_video",
        params={"prompt": "crash matrix fal video"},
        aspect=Aspect.WIDE,
        duration_s=5.0,
        seed=17,
    )


class _FalHarness:
    """One MockTransport per run: counts submits, serves queue + asset."""

    def __init__(self, *, kind: str, asset_bytes: bytes | None = None) -> None:
        self.kind = kind
        self._asset_bytes = asset_bytes
        self.submit_count = 0
        self.status_urls_hit: list[str] = []
        self.response_urls_hit: list[str] = []
        self.asset_urls_hit: list[str] = []

    def transport(self) -> httpx.MockTransport:
        if self.kind == "image":
            asset = self._asset_bytes or IMAGE_BYTES
            content_type = "image/png"
        else:
            assert self._asset_bytes is not None
            asset = self._asset_bytes
            content_type = "video/mp4"
        asset_name = "out.png" if self.kind == "image" else "out.mp4"

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/test-vendor/test-model":
                self.submit_count += 1
                return httpx.Response(
                    200,
                    json={
                        "request_id": "req-123",
                        "status_url": "https://queue.fal.run/test-vendor/test-model/requests/req-123/status",
                        "response_url": "https://queue.fal.run/test-vendor/test-model/requests/req-123/response",
                        "cancel_url": "https://queue.fal.run/test-vendor/test-model/requests/req-123/cancel",
                    },
                )
            if request.method == "GET" and path == "/test-vendor/test-model/requests/req-123/status":
                self.status_urls_hit.append(str(request.url))
                return httpx.Response(200, json={"status": "COMPLETED"})
            if request.method == "GET" and path == "/sentinel/req-123/status":
                self.status_urls_hit.append(str(request.url))
                return httpx.Response(200, json={"status": "COMPLETED"})
            if request.method == "GET" and path == "/test-vendor/test-model/requests/req-123/response":
                self.response_urls_hit.append(str(request.url))
                return httpx.Response(200, json=self._response_payload(asset_name))
            if request.method == "GET" and path == "/sentinel/req-123/response":
                self.response_urls_hit.append(str(request.url))
                return httpx.Response(200, json=self._response_payload(asset_name))
            if request.method == "GET" and path == f"/{asset_name}":
                self.asset_urls_hit.append(str(request.url))
                return httpx.Response(
                    200, content=asset, headers={"content-type": content_type}
                )
            return httpx.Response(404, json={"detail": f"unhandled {request.method} {path}"})

        return httpx.MockTransport(handler)

    def _response_payload(self, asset_name: str) -> dict:
        url = f"https://cdn.fal.run/{asset_name}?token=SIGNEDMARKER0123456789"
        if self.kind == "image":
            return {"images": [{"url": url}], "has_nsfw_concepts": False}
        return {"video": {"url": url}, "has_nsfw_concepts": False}

    def provider(self) -> FalJobProvider:
        return FalJobProvider(
            _provider_entry(kind=self.kind),
            kind=self.kind,
            api_key=SECRET,
            transport=self.transport(),
        )


def _ctx():
    from studio.core.gateway import CallContext

    return CallContext(
        episode_id=None,
        stage_run_id=None,
        purpose="t-rec-03",
        gate_exempt="t-rec-03",
    )


def _registry_key(*, kind: str) -> str:
    return "fal.test.image" if kind == "image" else "fal.test.video"


JOB_CRASH_POINTS = tuple(p for p in CRASH_POINTS if p != "during_single_call")


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", JOB_CRASH_POINTS)
@pytest.mark.parametrize(
    "kind",
    [pytest.param("image"), pytest.param("video", marks=pytest.mark.ffmpeg)],
)
async def test_rec_03_fal_crash_matrix(tmp_path, monkeypatch, kind, crash_point):
    home = make_home(tmp_path, registry_overrides=FAL_PRICES, filename="rec3")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db_path = home / "data" / "studio.db"
    db = Database(db_path)
    migrate(db)

    asset_bytes = await _video_asset_bytes(tmp_path) if kind == "video" else IMAGE_BYTES
    harness = _FalHarness(kind=kind, asset_bytes=asset_bytes)
    injector = FaultInjector()
    injector.arm(crash_point)

    gateway1 = build_gateway(
        db=db,
        config=config,
        clock=FakeClock(),
        ids=_Ids(),
        faults=injector,
        providers={_registry_key(kind=kind): harness.provider()},
        seed=1,
    )

    with pytest.raises(SimulatedCrash):
        if kind == "image":
            await gateway1.image(_registry_key(kind=kind), _request(kind=kind), _ctx())
        else:
            await gateway1.video(_registry_key(kind=kind), _request(kind=kind), _ctx())

    # --- resume: brand-new Gateway + DB handles on the same files ----------
    db2 = Database(db_path)
    gateway2 = build_gateway(
        db=db2,
        config=config,
        clock=FakeClock(),
        ids=_Ids(),
        providers={_registry_key(kind=kind): harness.provider()},
        seed=1,
    )
    if kind == "image":
        result = await gateway2.image(_registry_key(kind=kind), _request(kind=kind), _ctx())
    else:
        result = await gateway2.video(_registry_key(kind=kind), _request(kind=kind), _ctx())

    # --- assertions ---------------------------------------------------------
    assert harness.submit_count == _EXPECTED_SUBMITS[crash_point]

    call_row = db2.connect().execute(
        "SELECT status, est_cost_musd, remote_request_id, remote_handle_json "
        "FROM provider_calls"
    ).fetchone()
    expected_est = 50_000 if kind == "image" else 2_000_000  # 0.05 USD / 5s @ 0.40 USD
    assert call_row["est_cost_musd"] == expected_est
    assert call_row["status"] == "completed"
    assert call_row["remote_request_id"] == "req-123"

    # The handle (all four fal URLs) is persisted as JSON in one statement.
    handle = json.loads(call_row["remote_handle_json"])
    assert handle["status_url"].endswith("/requests/req-123/status")
    assert handle["response_url"].endswith("/requests/req-123/response")
    assert handle["cancel_url"].endswith("/requests/req-123/cancel")

    # no dangling reservation: reserve is released/settled per scope
    for scope_ref in ("2026-01", "global"):
        held = db2.connect().execute(
            "SELECT COALESCE(SUM(amount_musd), 0) AS h FROM cost_ledger "
            "WHERE scope_ref = ?",
            (scope_ref,),
        ).fetchone()["h"]
        assert held == expected_est

    settle = db2.connect().execute(
        "SELECT COALESCE(SUM(amount_musd), 0) AS s FROM cost_ledger "
        "WHERE entry_type = 'settle'"
    ).fetchone()["s"]
    assert settle == 2 * expected_est  # settled exactly 1x per scope

    reserve_rows = db2.connect().execute(
        "SELECT count(*) AS n FROM cost_ledger WHERE entry_type = 'reserve'"
    ).fetchone()["n"]
    release_rows = db2.connect().execute(
        "SELECT count(*) AS n FROM cost_ledger WHERE entry_type = 'release'"
    ).fetchone()["n"]
    assert reserve_rows == release_rows

    # exactly one potential_orphan audit when (and only when) the provider may
    # have accepted work before we lost the remote id
    orphan_audits = db2.connect().execute(
        "SELECT count(*) AS n FROM audit_events WHERE event = 'potential_orphan'"
    ).fetchone()["n"]
    if crash_point == "after_submit_before_persist":
        assert orphan_audits == 1
    else:
        assert orphan_audits == 0

    cached = db2.connect().execute("SELECT count(*) AS n FROM cache_entries").fetchone()["n"]
    assert cached == 1
    assert len(result.assets) == 1


@pytest.mark.asyncio
async def test_rec_03_resume_reuses_persisted_handle_not_fallback(
    tmp_path, monkeypatch
):
    """After a crash at ``during_poll``, resume must drive the persisted
    remote_handle_json — proven by sentinel URLs: the poll/fetch must hit the
    sentinel paths, never the fallback URLs derived from the request id."""
    home = make_home(tmp_path, registry_overrides=FAL_PRICES, filename="rec3s")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db_path = home / "data" / "studio.db"
    db = Database(db_path)
    migrate(db)

    harness = _FalHarness(kind="image")
    injector = FaultInjector()
    injector.arm("during_poll")

    gateway1 = build_gateway(
        db=db,
        config=config,
        clock=FakeClock(),
        ids=_Ids(),
        faults=injector,
        providers={"fal.test.image": harness.provider()},
        seed=1,
    )
    with pytest.raises(SimulatedCrash):
        await gateway1.image("fal.test.image", _request(kind="image"), _ctx())

    # Rewrite the persisted handle with sentinel status/response URLs. A
    # resume that ignores remote_handle_json (and falls back to URLs derived
    # from remote_request_id) would never hit these paths.
    row = db.connect().execute(
        "SELECT remote_handle_json FROM provider_calls"
    ).fetchone()
    handle = json.loads(row["remote_handle_json"])
    sentinel = {
        **handle,
        "status_url": "https://queue.fal.run/sentinel/req-123/status",
        "response_url": "https://queue.fal.run/sentinel/req-123/response",
    }
    db.connect().execute(
        "UPDATE provider_calls SET remote_handle_json = ?",
        (json.dumps(sentinel),),
    )
    db.connect().commit()

    db2 = Database(db_path)
    gateway2 = build_gateway(
        db=db2,
        config=config,
        clock=FakeClock(),
        ids=_Ids(),
        providers={"fal.test.image": harness.provider()},
        seed=1,
    )
    await gateway2.image("fal.test.image", _request(kind="image"), _ctx())

    assert harness.submit_count == 1  # no re-submit
    assert harness.status_urls_hit == ["https://queue.fal.run/sentinel/req-123/status"]
    assert harness.response_urls_hit == ["https://queue.fal.run/sentinel/req-123/response"]
    assert all("/test-vendor/test-model/requests/req-123/status" not in url
               for url in harness.status_urls_hit)
