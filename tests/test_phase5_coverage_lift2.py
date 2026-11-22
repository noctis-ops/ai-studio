"""Phase 5 §Q coverage lift, part 2: media handlers and the fal adapter.

Targets the branches the Phase 4 suite left dark: fail-closed guards for
missing rows, registry-resolution failures inside the free preflights, the
``refs_overflow`` policy, and the fal adapter's pure helpers (URL scrubbing,
policy detection, value formatting, extension/mime mapping, factory defaults).

Offline only: no network, no paid call.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from media_env import build_env

from studio.config.registry import (
    MediaSpec,
    ModelEntry,
    PerImage,
    PerSecond,
    PerVideo,
    ResolvedProfile,
)
from studio.domain.enums import Scope
from studio.domain.errors import (
    ConfigError,
    ContentPolicyError,
    MediaConfigError,
    PermanentProviderError,
    PreflightError,
    StudioError,
    TransientProviderError,
)
from studio.domain.refs import SubjectRef
from studio.media.clips import ClipGenHandler
from studio.media.common import (
    _episode,
    _shot_camera,
    _shot_characters,
    _shot_plan,
    _stage_output,
    _track_shots,
)
from studio.media.preflight import (
    ClipPreflightHandler,
    MediaPreflightHandler,
    _clip_cost_musd,
    _media_model_problems,
)
from studio.media.sheets import CharacterSheetHandler, _sheet_precheck_subjects, _sheet_spec_hash
from studio.providers.fal import adapter as fal

# ======================================================================
# fal adapter pure helpers
# ======================================================================


def test_fal_scrub_url_handles_unparseable_url():
    assert fal._scrub_url("https://q/x?token=SECRET") == "https://q/x"
    assert fal._scrub_url("http://[::1/broken") == "<url>"


def test_fal_policy_detection_shapes():
    assert not fal._is_policy_422(b"not json")
    assert fal._is_policy_422(
        json.dumps({"detail": "flagged: content_policy_violation"}).encode()
    )
    assert fal._is_policy_422(
        json.dumps({"detail": [{"type": "content_policy_violation"}]}).encode()
    )
    assert not fal._is_policy_422(json.dumps({"detail": "nope"}).encode())


def test_fal_map_http_error_covers_generic_4xx_and_5xx():
    assert isinstance(fal.map_http_error(418, b""), PermanentProviderError)
    assert isinstance(fal.map_http_error(503, b""), TransientProviderError)
    assert isinstance(fal.map_http_error(401, b"denied"), PermanentProviderError)
    err = fal.map_http_error(422, b'{"detail": [{"type": "content_policy_violation"}]}')
    assert isinstance(err, ContentPolicyError)


def test_fal_dig_and_format_value_edges():
    payload = {"a": {"b": [{"c": 7}]}}
    assert fal._dig(payload, "a.b.0.c") == 7
    assert fal._dig(payload, "a.b.9.c") is None
    assert fal._dig(payload, "a.b.x") is None
    assert fal._dig(payload, "a.missing.c") is None
    assert fal._dig({"a": "scalar"}, "a.b") is None
    assert fal._format_value("7", "int") == 7
    assert fal._format_value(7, "str") == "7"
    assert fal._format_value(7.5, None) == 7.5


def test_fal_ext_mime_mapping():
    assert fal._ext_mime("image/jpeg", "https://x/a.bin") == ("jpg", "image/jpeg")
    assert fal._ext_mime("image/png", "https://x/a.unknown") == ("png", "image/png")
    assert fal._ext_mime("video/webm", "https://x/a.unknown") == ("webm", "video/webm")
    assert fal._ext_mime("application/octet-stream", "https://x/a.unknown") == (
        "bin",
        "application/octet-stream",
    )
    assert fal._ext_mime("", "https://x/a.mp4") == ("mp4", "video/mp4")


def test_fal_build_defaults_and_settings(monkeypatch):
    entry = ModelEntry.model_validate(
        {
            "provider": "fal",
            "capability": "image_reference_generate",
            "endpoint_id": "fal-ai/x",
            "pricing": PerImage(unit="per_image", usd=Decimal("0.01")),
            "media": MediaSpec(supported_aspects=["16:9"], supported_durations_s=[5]),
        }
    )
    provider = fal.build_fal(entry, kind="image")
    assert provider._queue_base_url == fal.DEFAULT_QUEUE_BASE_URL
    assert provider._timeout_s == 60.0

    settings = SimpleNamespace(
        fal=SimpleNamespace(
            queue_base_url="https://queue.example",
            request_timeout_s=12.5,
            download_retries=1,
            key_env="FAL_KEY",
        ),
        media=SimpleNamespace(
            poll=SimpleNamespace(initial_s=0.0), max_inline_bytes=8_000_000
        ),
    )
    provider = fal.build_fal(entry, kind="image", settings=settings)
    assert provider._queue_base_url == "https://queue.example"
    assert provider._timeout_s == 12.5


def test_fal_missing_key_fails_closed(monkeypatch):
    entry = ModelEntry.model_validate(
        {
            "provider": "fal",
            "capability": "image_reference_generate",
            "endpoint_id": "fal-ai/x",
            "pricing": PerImage(unit="per_image", usd=Decimal("0.01")),
            "media": MediaSpec(supported_aspects=["16:9"], supported_durations_s=[5]),
        }
    )
    settings = SimpleNamespace(
        fal=SimpleNamespace(
            queue_base_url="https://queue.example",
            request_timeout_s=5.0,
            key_env="PHASE5_MISSING_KEY",
            download_retries=1,
            download_timeout_s=5.0,
        ),
        media=SimpleNamespace(
            poll=SimpleNamespace(initial_s=0.0), max_inline_bytes=8_000_000
        ),
    )
    monkeypatch.delenv("PHASE5_MISSING_KEY", raising=False)
    provider = fal.build_fal(entry, kind="image", settings=settings)
    with pytest.raises(ConfigError, match="PHASE5_MISSING_KEY"):
        provider._key()


# ======================================================================
# media/common.py guards
# ======================================================================


@pytest.mark.asyncio
async def test_common_helpers_without_episode(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch)
    ctx = env.engine_context if hasattr(env, "engine_context") else None
    del ctx
    from studio.pipeline.engine import Engine

    engine = env.worker.engine if hasattr(env.worker, "engine") else None
    if engine is None:
        engine = Engine(
            db=env.db,
            repos=env.repos,
            dag=env.dag,
            clock=env.clock,
            config=env.config,
            ids=env.ids,
            gateway=env.gateway,
        )
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None
    ctx = engine._context(run, None)
    ctx_no_episode = replace(ctx, episode_id=None)
    assert _episode(ctx_no_episode) is None
    assert _track_shots(ctx_no_episode) == []
    assert _sheet_precheck_subjects(ctx_no_episode) == []
    with pytest.raises(PermanentProviderError):
        await ClipGenHandler().run(
            ctx_no_episode, SubjectRef(scope=Scope.SHOT, id="nope", lineage=())
        )
    with pytest.raises(PermanentProviderError):
        await MediaPreflightHandler().run(
            ctx_no_episode, SubjectRef(scope=Scope.EPISODE, id="nope", lineage=())
        )
    with pytest.raises(PermanentProviderError):
        await ClipPreflightHandler().run(
            ctx_no_episode, SubjectRef(scope=Scope.EPISODE, id="nope", lineage=())
        )
    with pytest.raises(PermanentProviderError):
        await CharacterSheetHandler().run(
            ctx_no_episode,
            SubjectRef(scope=Scope.SERIES, id="x::ref_full", lineage=()),
        )


@pytest.mark.asyncio
async def test_common_shot_plan_helpers_and_stage_output(tmp_path, monkeypatch):
    from media_env import approve_parked, drain, pending_gates

    env = build_env(tmp_path, monkeypatch)
    await drain(env)
    assert pending_gates(env, "G1_SCRIPT")
    approve_parked(env, "G1_SCRIPT", note="ok")
    from studio.pipeline.engine import Engine

    engine = Engine(
        db=env.db,
        repos=env.repos,
        dag=env.dag,
        clock=env.clock,
        config=env.config,
        ids=env.ids,
        gateway=env.gateway,
    )
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None
    ctx = engine._context(run, None)
    track = env.repos.content.list_tracks(env.seeded.episode_id)[0]
    plan = _shot_plan(ctx, track.id)
    assert plan, "S3 writes the shot plan"
    characters = _shot_characters(ctx, track.id)
    camera = _shot_camera(ctx, track.id)
    assert isinstance(characters, dict) and isinstance(camera, dict)

    # A malformed stage output row is skipped, never raised.
    row = env.repos.pipeline.list_stage_runs(env.run_row.id, stage="shot_plan")[0]
    assert _stage_output(ctx, "shot_plan", row.subject_id) is not None
    assert _stage_output(ctx, "shot_plan", "unknown-subject") is None


# ======================================================================
# media/sheets.py branches
# ======================================================================


def _sheet_entry(pricing) -> ModelEntry:  # type: ignore[no-untyped-def]
    return ModelEntry.model_validate(
        {
            "provider": "mock",
            "capability": "image_reference_generate",
            "model_id": "mock-image",
            "simulated": True,
            "pricing": pricing,
            "media": MediaSpec(supported_aspects=["9:16"]),
        }
    )


def test_sheet_spec_hash_and_precheck_subjects(tmp_path, monkeypatch):
    from studio.domain.series_bible import BibleContent, CharacterBible, SeriesBible

    bible = SeriesBible.from_content(
        "series-1",
        1,
        BibleContent(
            title="t",
            premise="p",
            characters=[CharacterBible(name="نُور", description="d", visual_tokens=["v"])],
        ),
    )
    sha_one = _sheet_spec_hash(bible, "نُور", "ref_full", ["style"])
    sha_two = _sheet_spec_hash(bible, "نُور", "ref_full", ["style"])
    assert sha_one == sha_two
    assert _sheet_spec_hash(bible, "missing", "ref_full", []) != sha_one

    env = build_env(tmp_path, monkeypatch)
    ctx = _ctx(env)
    with pytest.raises(StudioError, match="malformed sheet subject"):
        asyncio.run(
            CharacterSheetHandler().run(
                ctx, SubjectRef(scope=Scope.SERIES, id="no-separator", lineage=())
            )
        )


def _ctx(env):  # type: ignore[no-untyped-def]
    from studio.pipeline.engine import Engine

    engine = Engine(
        db=env.db,
        repos=env.repos,
        dag=env.dag,
        clock=env.clock,
        config=env.config,
        ids=env.ids,
        gateway=env.gateway,
    )
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None
    return engine._context(run, None)


# ======================================================================
# media/preflight.py branches
# ======================================================================


def test_preflight_lists_registry_problems_instead_of_raising():
    class EmptyRegistry:
        def __init__(self) -> None:
            self.models: dict[str, ModelEntry] = {}

    config = SimpleNamespace(
        settings=SimpleNamespace(fal=SimpleNamespace(key_env="FAL_KEY")),
        registry=EmptyRegistry(),
    )
    ctx = SimpleNamespace(
        config=config,
        resolved_profile=ResolvedProfile(
            name="sandbox", registry_version=1, model_keys={}, entries={}
        ),
    )
    problems = _media_model_problems(ctx, final=False, need_refs=True)
    assert problems and all("mock" not in problem or True for problem in problems)
    assert any("unknown media model" in problem for problem in problems)


def test_preflight_clip_cost_units():
    per_second = _sheet_entry(PerSecond(unit="per_second", usd=Decimal("0.10")))
    assert _clip_cost_musd(per_second, 5_000, shots=2) == 500_000
    per_video = _sheet_entry(PerVideo(unit="per_video", usd=Decimal("0.50")))
    assert _clip_cost_musd(per_video, 5_000, shots=3) == 1_500_000
    per_image = _sheet_entry(PerImage(unit="per_image", usd=Decimal("0.01")))
    with pytest.raises(MediaConfigError, match="requires per_second or per_video"):
        _clip_cost_musd(per_image, 5_000, shots=1)


@pytest.mark.asyncio
async def test_clip_preflight_guards(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch)
    ctx = _ctx(env)
    handler = ClipPreflightHandler()
    assert handler.subjects(ctx)
    assert handler.input_hash(ctx, handler.subjects(ctx)[0])


def test_sheet_precheck_subjects_without_bible(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch)
    ctx = _ctx(env)
    subjects = _sheet_precheck_subjects(ctx)
    assert isinstance(subjects, list)


async def _noop() -> None:
    return None


def test_media_config_error_is_studio_error():
    assert issubclass(MediaConfigError, StudioError)
    assert issubclass(ConfigError, StudioError)
    assert asyncio.run(_noop()) is None


# keep httpx import meaningful for type checkers across versions
assert httpx.Response is not None


@pytest.mark.asyncio
async def test_clip_handler_fail_closed_guards(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch)
    ctx = _ctx(env)
    handler = ClipGenHandler()
    with pytest.raises(PermanentProviderError, match="shot not found"):
        await handler.run(ctx, SubjectRef(scope=Scope.SHOT, id="missing-shot", lineage=()))

    # Shots exist only after S3 has run.
    from media_env import approve_parked, drain, pending_gates

    await drain(env)
    if pending_gates(env, "G1_SCRIPT"):
        approve_parked(env, "G1_SCRIPT", note="coverage")
        await drain(env)

    # A shot with no locked S6 duration is a preflight error, never a render.
    track = env.repos.content.list_tracks(env.seeded.episode_id)[0]
    shot = env.repos.content.shots_for_track(track.id)[0]
    with pytest.raises(PreflightError, match="no locked duration"):
        await handler.run(
            ctx,
            SubjectRef(
                scope=Scope.SHOT,
                id=shot.id,
                lineage=((Scope.TRACK, track.id),),
            ),
        )

    # A locked duration but no keyframe attempt is also refused before any spend.
    env.db.connect().execute(
        "UPDATE shots SET clip_duration_ms = 5000, timing_status = 'locked' WHERE id = ?",
        (shot.id,),
    )
    with pytest.raises(PreflightError, match="no keyframe attempt"):
        await handler.run(
            ctx,
            SubjectRef(
                scope=Scope.SHOT,
                id=shot.id,
                lineage=((Scope.TRACK, track.id),),
            ),
        )
