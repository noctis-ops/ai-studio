"""T-PRE-01, T-CLP-02/03, T-ENG-15/16, T-CLI-04 (PHASE4 §P).

The production-shaped chain (seed_production + production_specs) through the
real stage handlers, mock providers, lavfi-backed ffmpeg. Fully offline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from media_env import (
    add_music_and_scene,
    approve_parked,
    audits,
    build_env,
    drain,
    pending_gates,
    provider_calls,
    run_to_g2,
    run_to_s10,
    stage_rows,
)
from typer.testing import CliRunner

from studio.config.loader import load_all
from studio.domain.enums import Aspect, Language
from studio.domain.errors import PreflightError
from studio.media.stages import MediaPreflightHandler
from studio.pipeline.context import RunContext
from studio.pipeline.stages_audio import TimingReconcilerHandler

pytestmark = pytest.mark.ffmpeg

RUNNER = CliRunner()


def _shot_id_for_shot_no(env, shot_no: int) -> str:
    row = env.db.connect().execute(
        "SELECT id FROM shots WHERE shot_no = ?", (shot_no,)
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _shot_rows(env):
    return env.db.connect().execute(
        "SELECT id, shot_no, clip_duration_ms, timing_status FROM shots ORDER BY shot_no"
    ).fetchall()


def _ctx(env, resolved_profile=None):
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None
    episode = env.repos.content.get_episode(run.episode_id) if run.episode_id else None
    languages = [Language(v) for v in json.loads(episode.languages_json)] if episode else [Language.AR]
    aspects = [Aspect(v) for v in json.loads(episode.aspects_json)] if episode else [Aspect.TALL]
    return RunContext(
        db=env.db,
        repos=env.repos,
        gateway=env.gateway,
        clock=env.clock,
        ids=env.ids,
        config=env.config,
        resolved_profile=resolved_profile or env.config.registry.resolve_profile(run.profile),
        run_id=run.id,
        episode_id=run.episode_id,
        languages=languages,
        aspects=aspects,
    )


def _accepted_keyframe_shas(env) -> dict[str, str]:
    """shot_id -> CAS sha of the G2-accepted keyframe attempt (asset_id)."""
    out: dict[str, str] = {}
    for row in _shot_rows(env):
        attempt = env.repos.content.current_shot_attempt(row["id"], kind="keyframe")
        assert attempt is not None and attempt.asset_id
        out[row["id"]] = attempt.asset_id
    return out


def _paid_media_calls(env):
    calls = provider_calls(env)
    return [
        c for c in calls
        if c["capability"] in ("tts", "image_reference_generate", "image_to_video")
    ]


# ==================================================================
# T-PRE-01 — preflight lists every problem at once
# ==================================================================

def _fal_model(endpoint: str, **overrides):
    entry = {
        "provider": "fal",
        "capability": "image_reference_generate",
        "endpoint_id": endpoint,
        "pricing": {"unit": "per_image", "usd": 0.0},
        "pricing_verified_on": None,
        "commercial_use_verified": False,
        "needs_confirmation": True,
        "media": {
            "supported_aspects": ["9:16", "16:9"],
            "aspect_values": {"9:16": "9:16", "16:9": "16:9"},
            "supported_durations_s": [],
            "max_refs": 4,
            "param_map": {"prompt": "prompt"},
            "result_map": {"asset_url": "images.0.url"},
        },
    }
    import copy

    merged = copy.deepcopy(entry)

    def _merge(dst, src):
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                _merge(dst[key], value)
            else:
                dst[key] = value

    _merge(merged, overrides)
    return merged


_FAL_WIRING = {
    "profiles": {"sandbox": {
        "image_t2i": "acme.image.t2i",
        "image_ref": "acme.image.ref",
        "video_i2v": "acme.video.i2v",
    }},
    "models": {
        # sheets: wrong-aspect contract only
        "acme.image.t2i": _fal_model(
            "fal-ai/acme/t2i",
            media={"supported_aspects": ["16:9"], "aspect_values": {"16:9": "16:9"}},
        ),
        # keyframes: not reference-capable
        "acme.image.ref": _fal_model("fal-ai/acme/ref", supports_refs=False),
        # clips: no supported durations in the media contract
        "acme.video.i2v": _fal_model(
            "fal-ai/acme/i2v",
            capability="image_to_video",
            pricing={"unit": "per_second", "usd": 0.0},
            media={"supported_aspects": ["9:16", "16:9"],
                   "aspect_values": {"9:16": "9:16", "16:9": "16:9"},
                   "supported_durations_s": []},
            supported_durations_s=[],
        ),
    },
}


@pytest.mark.asyncio
async def test_pre_01_preflight_lists_every_problem_at_once(tmp_path, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    env = build_env(tmp_path, monkeypatch, filename="pre01a", registry_overrides=_FAL_WIRING)
    await drain(env)  # text chain runs; parks at G1 (bible is pinned by then)

    handler = MediaPreflightHandler()
    ctx = _ctx(env)
    (subject,) = handler.subjects(ctx)
    with pytest.raises(PreflightError) as excinfo:
        await handler.run(ctx, subject)
    message = str(excinfo.value)

    expected = [
        # unconfirmed / free-but-unacknowledged — once per role
        "needs_confirmation is true",
        "price never verified",
        "zero price without pricing_acknowledged_free",
        # missing key — once per fal role
        "acme.image.t2i' requires FAL_KEY to be set",
        "acme.image.ref' requires FAL_KEY to be set",
        "acme.video.i2v' requires FAL_KEY to be set",
        # unsupported aspect (sheets) and unsupported duration (clips)
        "acme.image.t2i' does not support track aspect 9:16",
        "acme.video.i2v' declares no supported durations",
        # ref capability (keyframes must be reference-capable)
        "acme.image.ref' is not marked reference-capable",
    ]
    for needle in expected:
        assert needle in message, f"missing from preflight problems: {needle!r}\n{message}"
    # every role is named — this is a list, not a fail-fast on the first hit
    for role in ("sheets", "keyframes", "clips"):
        assert f"{role} model" in message

    # and the engine path fails closed: no stage run, no paid call
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    add_music_and_scene(env)
    approve_parked(env, "G3_FINAL_CUT", note="metadata approved")
    await drain(env)  # s4p runs and fails
    rows = stage_rows(env, "s4p_media_preflight")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert provider_calls(env, capability="tts") == []
    assert provider_calls(env, capability="image_reference_generate") == []
    assert provider_calls(env, capability="image_to_video") == []


def test_pre_01_unverified_price_and_commercial_flags_in_final(tmp_path, monkeypatch):
    """The final-profile-only confirmations are listed by the same helper the
    preflights use (the final text chain needs the real LLM adapters, so this
    is asserted at the unit level)."""
    from conftest import make_home

    from studio.media.registry import model_confirmation_problems

    home = make_home(tmp_path, filename="pre01final")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    for key in ("image.t2i.draft", "image.ref.final", "video.i2v.final"):
        entry = config.registry.models[key]
        draft = model_confirmation_problems(entry, final=False)
        final = model_confirmation_problems(entry, final=True)
        assert "price never verified" in final
        assert any("commercial_use_verified is false for final" in p for p in final)
        assert any("needs_confirmation is true" in p for p in final)
        assert "price never verified" in draft
        # the commercial flag only blocks `final`
        assert not any("commercial_use_verified" in p for p in draft)


@pytest.mark.asyncio
async def test_pre_01_over_budget_halts_before_any_spend(tmp_path, monkeypatch):
    # Run the (expensive but text-only) chain first, then shrink the episode
    # cap to 1 micro-USD so the media affordability guard halts at s4p —
    # before a single paid media call is made.
    env = build_env(tmp_path, monkeypatch, filename="pre01b")
    await drain(env)
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)  # text chain complete; run parked at G3

    episode_id = env.seeded.episode_id
    env.repos.budgets.ensure("episode", episode_id, 1)
    env.repos.budgets.reset_kill_switch("episode", episode_id, new_cap_musd=1)

    add_music_and_scene(env)
    approve_parked(env, "G3_FINAL_CUT", note="metadata approved")
    await drain(env)  # s4p runs; the affordability guard halts the run

    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == "halted_budget"
    halted = audits(env, "budget_halted")
    assert halted, "expected a budget_halted audit"
    assert any(
        "expected media spend" in str(audit.get("error", "")) for audit in halted
    ), halted
    # no paid call was made, no media stage was created
    assert _paid_media_calls(env) == []
    assert stage_rows(env, "s8a_character_sheets") == []
    assert stage_rows(env, "s10_clips") == []


# ==================================================================
# T-CLP-02 — unsupported locked duration fails s10p closed;
#            changing the video model stales S6
# ==================================================================


@pytest.mark.asyncio
async def test_clp_02_unsupported_locked_duration_fails_s10p_closed(
    tmp_path, monkeypatch
):
    # S6 reads the legacy top-level durations ([5] -> it locks 5 s); the
    # media wire contract declares [10] only. The preflight must refuse the
    # locked duration instead of paying for a clip that cannot conform.
    env = build_env(
        tmp_path,
        monkeypatch,
        filename="clp02a",
        registry_overrides={
            "models": {
                "mock.video": {
                    "supported_durations_s": [5],
                    "media": {"supported_durations_s": [10]},
                }
            }
        },
    )
    await run_to_g2(env)
    locked = {row["id"]: int(row["clip_duration_ms"]) for row in _shot_rows(env)}
    assert locked, "expected S6 to lock durations before G2"
    assert set(locked.values()) == {5000}

    approve_parked(env, "G2_KEYFRAMES", note="keyframes approved")
    await drain(env)  # s10p runs and fails closed

    rows = stage_rows(env, "s10p_clip_preflight")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    failure = audits(env, "stage_run_failed")
    assert any(
        "5000 ms" in str(audit.get("error", ""))
        and "supported durations" in str(audit.get("error", ""))
        for audit in failure
    ), f"expected the locked duration to be named, got {failure}"
    # no clip was paid; S6's locked values were not touched
    assert provider_calls(env, capability="image_to_video") == []
    assert {row["id"]: int(row["clip_duration_ms"]) for row in _shot_rows(env)} == locked


@pytest.mark.asyncio
async def test_clp_02_changing_video_model_stales_s6(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch, filename="clp02b")
    await drain(env)
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)  # parked at G3; S6 has not run yet
    add_music_and_scene(env)
    approve_parked(env, "G3_FINAL_CUT", note="metadata approved")
    await drain(env)  # S6 succeeded by now (run parked at G0)
    s6_row = [row for row in stage_rows(env, "s6_timing") if row["status"] == "succeeded"][-1]
    stored_hash = s6_row["input_hash"]

    handler = TimingReconcilerHandler()
    ctx = _ctx(env)
    (subject,) = handler.subjects(ctx)
    assert handler.input_hash(ctx, subject) == stored_hash

    # Simulate the operator confirming a different video endpoint: the only
    # thing the S6 hash tracks beyond the durations is the model key, and
    # that is exactly what _invalidate_stale recomputes after a registry edit.
    resolved = ctx.resolved_profile
    other_video = resolved.entries["video"].model_copy(
        update={"endpoint_id": "mock-video-next"}
    )
    moved = resolved.model_copy(update={
        "model_keys": {**resolved.model_keys, "video": "mock.video2"},
        "entries": {**resolved.entries, "video": other_video},
    })
    ctx_moved = dataclasses.replace(ctx, resolved_profile=moved)
    assert handler.input_hash(ctx_moved, subject) != stored_hash

    # And the durations feed the hash too (S6 re-locks when they change).
    slower = resolved.entries["video"].model_copy(update={"supported_durations_s": [10]})
    moved_durations = resolved.model_copy(
        update={"entries": {**resolved.entries, "video": slower}}
    )
    ctx_durations = dataclasses.replace(ctx, resolved_profile=moved_durations)
    assert handler.input_hash(ctx_durations, subject) != stored_hash


# ==================================================================
# T-CLP-03 — only the accepted keyframe conditions the clip;
#            a clip regeneration does not touch the keyframe
# ==================================================================


@pytest.mark.asyncio
async def test_clp_03_clips_use_only_the_accepted_keyframe(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch, filename="clp03a")
    await run_to_s10(env)

    accepted = _accepted_keyframe_shas(env)
    sheet_assets = {
        json.loads(row["output_json"])["asset_sha256"]
        for row in stage_rows(env, "s8a_character_sheets")
        if row["status"] == "succeeded" and row["output_json"]
    }
    assert len(env.video.requests) == len(accepted)
    used = [req.input_assets[0].sha256 for req in env.video.requests]
    # every clip conditioned on the *accepted keyframe* of a shot, never on a sheet
    assert sorted(used) == sorted(accepted.values())
    assert sheet_assets and not (set(used) & sheet_assets)
    # the ref travels as an inlined data URI (mock transport: data_uri)
    for req in env.video.requests:
        assert str(req.params.get("image", "")).startswith("data:image/jpeg;base64,")
        assert req.duration_s == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_clp_03_clip_regeneration_does_not_touch_keyframe(tmp_path, monkeypatch):
    from studio.cli import app

    env = build_env(tmp_path, monkeypatch, filename="clp03b")
    await run_to_s10(env)

    target = _shot_id_for_shot_no(env, 2)
    accepted = _accepted_keyframe_shas(env)
    keyframe_rows_before = [
        row for row in stage_rows(env, "s8_keyframes") if row["status"] == "succeeded"
    ]
    clip_rows_before = [
        row for row in stage_rows(env, "s10_clips") if row["status"] == "succeeded"
    ]
    keyframe_calls_before = [
        row for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "keyframe"
    ]
    video_calls_before = provider_calls(env, capability="image_to_video")

    run_id = env.run_row.id
    result = RUNNER.invoke(
        app, ["shot", "regenerate", run_id, target, "--stage", "clip", "--yes",
              "--reason", "operator clip rework"]
    )
    assert result.exit_code == 0, result.output
    # bridge the harness clock gap (CLI enqueues with the system clock)
    from studio.core.clock import SystemClock
    env.clock.set(SystemClock().now())
    await drain(env)

    # the keyframe side is untouched: same rows, same attempts, same call count
    keyframe_rows_after = [
        row for row in stage_rows(env, "s8_keyframes") if row["status"] == "succeeded"
    ]
    assert [row["id"] for row in keyframe_rows_after] == [
        row["id"] for row in keyframe_rows_before
    ]
    keyframe_calls_after = [
        row for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "keyframe"
    ]
    assert len(keyframe_calls_after) == len(keyframe_calls_before)
    kf_attempts = sorted(
        row["attempt_no"]
        for row in env.db.connect().execute(
            "SELECT attempt_no FROM shot_attempts WHERE kind = 'keyframe' AND shot_id = ?",
            (target,),
        ).fetchall()
    )
    assert kf_attempts == [1]

    # a fresh clip attempt exists for the target shot only
    clip_attempts = env.db.connect().execute(
        "SELECT shot_id, attempt_no, nonce FROM shot_attempts WHERE kind = 'clip' "
        "ORDER BY shot_id, attempt_no"
    ).fetchall()
    by_shot: dict[str, list] = {}
    for row in clip_attempts:
        by_shot.setdefault(row["shot_id"], []).append(row)
    target_attempts = by_shot[target]
    assert len(target_attempts) == 2
    assert target_attempts[1]["nonce"] != target_attempts[0]["nonce"]
    for shot_id, attempts in by_shot.items():
        if shot_id != target:
            assert len(attempts) == 1

    # exactly one new paid clip call, conditioned on the same accepted keyframe
    video_calls_after = provider_calls(env, capability="image_to_video")
    assert len(video_calls_after) == len(video_calls_before) + 1
    assert env.video.requests[-1].input_assets[0].sha256 == accepted[target]

    # the untouched shots' clip rows are the same rows as before
    clip_rows_after = [
        row for row in stage_rows(env, "s10_clips") if row["status"] == "succeeded"
    ]
    before_ids = {row["subject_id"]: row["id"] for row in clip_rows_before}
    other_ids = {
        row["subject_id"]: row["id"]
        for row in clip_rows_after
        if row["subject_id"] != target
    }
    assert other_ids == {s: i for s, i in before_ids.items() if s != target}


# ==================================================================
# T-ENG-15 — no paid call before its ancestor gates are approved
# ==================================================================


@pytest.mark.asyncio
async def test_eng_15_no_paid_call_before_ancestor_gates(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch, filename="eng15")

    def paid_by_purpose(purpose: str) -> list:
        return [
            row for row in provider_calls(env) if row["purpose"] == purpose
        ]

    await drain(env)
    # lines are materialized by S2/S3, which ran in the first drain
    line_count = env.db.connect().execute(
        "SELECT count(*) AS n FROM narration_lines"
    ).fetchone()["n"]
    assert line_count == 3
    assert pending_gates(env, "G1_SCRIPT")
    assert _paid_media_calls(env) == []

    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    assert pending_gates(env, "G3_FINAL_CUT")
    assert _paid_media_calls(env) == [], "no media spend before the G3 round"

    add_music_and_scene(env)
    approve_parked(env, "G3_FINAL_CUT", note="metadata approved")
    await drain(env)
    assert pending_gates(env, "G0_CHARACTERS")
    assert len(provider_calls(env, capability="tts")) == line_count
    n_roles = len(env.config.settings.character_sheets.roles)
    assert len(paid_by_purpose("character_sheet")) == 2 * n_roles  # 2 characters
    assert paid_by_purpose("keyframe") == [], "keyframes run after G0"
    assert provider_calls(env, capability="image_to_video") == []

    approve_parked(env, "G0_CHARACTERS", note="sheets approved")
    await drain(env)
    assert pending_gates(env, "G2_KEYFRAMES")
    assert len(paid_by_purpose("keyframe")) == 3
    assert provider_calls(env, capability="image_to_video") == [], (
        "no clip may be paid before G2"
    )

    approve_parked(env, "G2_KEYFRAMES", note="keyframes approved")
    await drain(env)
    assert provider_calls(env, capability="image_to_video") != []
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == "completed"
    assert len(provider_calls(env, capability="image_to_video")) == 3


# ==================================================================
# T-ENG-16 — --languages ar,en doubles only TTS and mix
# ==================================================================


def _s7b_rows(env):
    rows = [
        row for row in stage_rows(env, "s7b_audio_mix") if row["status"] == "succeeded"
    ]
    return rows


@pytest.mark.asyncio
async def test_eng_16_languages_toggle_doubles_tts_and_mix_only(tmp_path, monkeypatch):
    counts: dict[tuple[str, ...], dict[str, object]] = {}
    mixes: dict[tuple[str, ...], int] = {}
    plans: dict[tuple[str, ...], int] = {}
    for idx, languages in enumerate((("ar",), ("ar", "en"))):
        env = build_env(
            tmp_path, monkeypatch, filename=f"eng16{idx}", languages=languages
        )
        await run_to_s10(env)
        shots = env.db.connect().execute("SELECT count(*) FROM shots").fetchone()[0]
        calls: dict[str, int] = {}
        for row in provider_calls(env):
            calls[row["capability"]] = calls.get(row["capability"], 0) + 1
        counts[languages] = {"shots": shots, "calls": calls}
        mixes[languages] = len(_s7b_rows(env))
        plans[languages] = len(
            [
                row for row in stage_rows(env, "s7_audio_plan") if row["status"] == "succeeded"
            ]
        )

    ar = counts[("ar",)]
    both = counts[("ar", "en")]
    ar_calls = ar["calls"]
    both_calls = both["calls"]
    shots = ar["shots"]
    assert shots == both["shots"]
    # TTS doubles with the language count
    assert both_calls["tts"] == 2 * ar_calls["tts"]
    # image and video calls are per-track/language-neutral: unchanged
    assert ar_calls["image_reference_generate"] == both_calls["image_reference_generate"]
    assert ar_calls["image_to_video"] == both_calls["image_to_video"]
    # sheets are per series/aspect: unchanged
    n_roles = len(env.config.settings.character_sheets.roles)
    assert ar_calls["image_reference_generate"] == shots + 2 * n_roles
    assert ar_calls["image_to_video"] == shots
    # mix (and the audio plan) are deliverable-scope: they double
    assert mixes[("ar",)] == 1
    assert mixes[("ar", "en")] == 2
    assert plans[("ar",)] == 1
    assert plans[("ar", "en")] == 2


# ==================================================================
# T-CLI-04 — shot list/regenerate, media preview, media qc,
#            media audition (mock), doctor
# ==================================================================


def test_cli_04_shot_list_and_regenerate_refusal(tmp_path, monkeypatch):
    from studio.cli import app

    monkeypatch.setenv("COLUMNS", "220")
    env = build_env(tmp_path, monkeypatch, filename="cli04a")
    asyncio.run(run_to_s10(env))
    run_id = env.run_row.id
    target = _shot_id_for_shot_no(env, 1)

    result = RUNNER.invoke(app, ["shot", "list", run_id])
    assert result.exit_code == 0, result.output
    assert "shots —" in result.output
    assert "keyframe" in result.output and "clip" in result.output
    assert "1.0000" in result.output  # mock keyframe $1.00
    assert "10.0000" in result.output  # mock clip 5 s x $2.00/s

    # regenerate without --yes refuses and spends nothing
    attempts_before = env.db.connect().execute(
        "SELECT count(*) FROM shot_attempts"
    ).fetchone()[0]
    result = RUNNER.invoke(
        app, ["shot", "regenerate", run_id, target, "--stage", "clip"]
    )
    assert result.exit_code == 1
    assert "nothing spent" in result.output
    attempts_after = env.db.connect().execute(
        "SELECT count(*) FROM shot_attempts"
    ).fetchone()[0]
    assert attempts_after == attempts_before


def test_cli_04_media_preview_escapes_arabic_and_html(tmp_path, monkeypatch):
    from studio.cli import app

    env = build_env(tmp_path, monkeypatch, filename="cli04b")
    asyncio.run(run_to_s10(env))
    run_id = env.run_row.id
    target = _shot_id_for_shot_no(env, 1)

    payload = 'حَارِس <ب> "اقتباس" & نهاية'
    env.db.connect().execute(
        "UPDATE shots SET prompt = ? WHERE id = ?", (payload, target)
    )
    result = RUNNER.invoke(app, ["media", "preview", run_id])
    assert result.exit_code == 0, result.output

    home = Path(env.config.settings.paths.home)
    outdir = home / "data" / "previews" / run_id
    html_text = (outdir / "keyframes.html").read_text(encoding="utf-8")
    # dir/lang for Arabic, html-escaped payload, no raw markup injection
    assert 'dir="auto"' in html_text
    assert "&lt;ب&gt;" in html_text
    assert "&amp;" in html_text
    assert "<ب>" not in html_text
    assert "حَارِس" in html_text
    assert (outdir / "sheets.html").exists()
    clips_html = (outdir / "clips.html").read_text(encoding="utf-8")
    assert "حَارِس" in clips_html and "<ب>" not in clips_html


def test_cli_04_media_qc_command(tmp_path, monkeypatch):
    from studio.cli import app

    env = build_env(tmp_path, monkeypatch, filename="cli04c")
    asyncio.run(run_to_s10(env))

    attempt = env.db.connect().execute(
        "SELECT id FROM shot_attempts WHERE kind = 'keyframe' ORDER BY shot_id LIMIT 1"
    ).fetchone()
    result = RUNNER.invoke(app, ["media", "qc", attempt["id"]])
    assert result.exit_code == 0, result.output
    assert "overall:" in result.output
    assert "aspect" in result.output

    # a purpose-built bad input fails closed with a non-zero exit
    black = tmp_path / "black.png"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "color=c=black:size=540x960:rate=24", "-frames:v", "1", str(black)],
        check=True,
        timeout=60,
    )
    result = RUNNER.invoke(app, ["media", "qc", str(black)])
    assert result.exit_code == 1


def test_cli_04_media_audition_image_with_mock(tmp_path, monkeypatch):
    from studio.cli import app

    env = build_env(tmp_path, monkeypatch, filename="cli04d")
    _ = env  # paid mock prices make the worst-case math non-degenerate
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("حَارِسُ فانوسٍ يهمس\nقطةٌ تعبرُ الشارع\n", encoding="utf-8")

    def _audition_dirs() -> list[Path]:
        base = Path(env.config.settings.paths.home) / "data" / "previews" / "audition_media"
        return list(base.glob("*")) if base.exists() else []

    # refuses without --yes and writes nothing
    result = RUNNER.invoke(
        app, ["media", "audition", "image", "--model-keys", "mock.image",
              "--prompts-file", str(prompts), "--cap-usd", "2.00"]
    )
    assert result.exit_code == 1
    assert "nothing spent" in result.output
    assert _audition_dirs() == []

    # refuses when the worst-case total exceeds the cap
    result = RUNNER.invoke(
        app, ["media", "audition", "image", "--model-keys", "mock.image",
              "--prompts-file", str(prompts), "--cap-usd", "1.50", "--yes"]
    )
    assert result.exit_code == 1
    assert "worst-case audition cost exceeds" in result.output
    assert _audition_dirs() == []

    # proceeds under the cap with the mock transport
    result = RUNNER.invoke(
        app, ["media", "audition", "image", "--model-keys", "mock.image",
              "--prompts-file", str(prompts), "--aspect", "9:16",
              "--cap-usd", "2.00", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "audition plan: 2 call(s)" in result.output
    dirs = _audition_dirs()
    assert len(dirs) == 1
    readme = (dirs[0] / "README.txt").read_text(encoding="utf-8")
    assert "media audition (image)" in readme
    assert "mock.image" in readme
    assert "total_spent_usd" in readme


def test_cli_04_doctor_reports_media_rows(tmp_path, monkeypatch):
    from studio.cli import app

    env = build_env(tmp_path, monkeypatch, filename="cli04e")
    del env
    result = RUNNER.invoke(app, ["doctor"])
    # the shipped registry is intentionally unconfirmed for real media models;
    # doctor may fail closed, but it must report the media surface either way
    assert result.exit_code in (0, 1)
    out = result.output
    assert "FAL_KEY" in out
    assert "media registry" in out
    assert "placeholder endpoint" in out
    assert "failed_job_billing" in out
    assert "media.chain_mode" in out
    assert "ffmpeg visual filters" in out
