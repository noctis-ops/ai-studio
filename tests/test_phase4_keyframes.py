"""T-KF-01..05 (PHASE4 §P keyframes).

S8 keyframes through the production-shaped fake-LLM chain with mock image
providers. Offline; lavfi-rendered mock output keeps the real QC path.

T-KF-01: ref selection, max_refs truncation warning, changed sheet stales keyframes.
T-KF-02: native aspect request; unsupported aspect fails s4p; aspect QC is non-retryable.
T-KF-03: black image -> retries q1, q2, then fail. Non-retryable failures never retry.
T-KF-04: crash resume repays nothing; chain_mode: last_frame raises ConfigError.
T-KF-05: `studio shot regenerate` — one shot only, new nonce, shows cost, needs --yes.
"""

from __future__ import annotations

import pytest
from media_env import (
    CHAR_PRIMARY,
    CHAR_SECOND,
    add_music_and_scene,
    approve_parked,
    audits,
    build_env,
    drain,
    pending_gates,
    preseed_approved_sheet,
    provider_calls,
    run_to_g2,
    run_to_g2_preseeded,
    run_to_s10,
    stage_outputs,
    stage_rows,
)
from typer.testing import CliRunner

from studio.config.loader import load_all
from studio.domain.enums import Aspect
from studio.providers.mock.image import aspect_dimensions
from studio.qc.visual import qc_image

pytestmark = pytest.mark.ffmpeg

RUNNER = CliRunner()


class BlackImageProvider:
    """Mock image provider that always returns a near-uniform dark frame.

    Subclassing keeps the on-disk ledger behaviour; only the render differs.
    """

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self.requests = inner.requests

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    async def submit(self, req):  # type: ignore[no-untyped-def]
        return await self._inner.submit(req)

    async def poll(self, handle):  # type: ignore[no-untyped-def]
        return await self._inner.poll(handle)

    async def fetch(self, handle):  # type: ignore[no-untyped-def]
        import asyncio
        import subprocess
        from pathlib import Path

        from studio.providers.base import MediaPayload

        state = self._inner._load_state()
        job = state.get("jobs", {}).get(handle.remote_request_id, {})
        out = Path(self._inner._render_dir()) / f"{handle.remote_request_id}.png"
        if not out.exists():
            aspect = job.get("aspect")
            width, height = aspect_dimensions(
                aspect,
                width_16x9=self._inner.width_16x9,
                height_16x9=self._inner.height_16x9,
            )
            await asyncio.to_thread(
                subprocess.run,
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c=black:size={width}x{height}:rate=24",
                    "-vf", "noise=alls=4:allf=a",
                    "-frames:v", "1", str(out),
                ],
                check=True,
                capture_output=True,
            )
        self._inner._record("fetch", remote_request_id=handle.remote_request_id)
        return [MediaPayload(local_path=out, ext="png", mime="image/png")]


class WideImageProvider(BlackImageProvider):
    """Mock image provider that renders 16:9 bytes regardless of the request."""

    async def fetch(self, handle):  # type: ignore[no-untyped-def]
        import asyncio
        import subprocess
        from pathlib import Path

        from studio.providers.base import MediaPayload

        out = Path(self._inner._render_dir()) / f"{handle.remote_request_id}.png"
        if not out.exists():
            width, height = self._inner.width_16x9, self._inner.height_16x9
            await asyncio.to_thread(
                subprocess.run,
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=24",
                    "-frames:v", "1", str(out),
                ],
                check=True,
                capture_output=True,
            )
        self._inner._record("fetch", remote_request_id=handle.remote_request_id)
        return [MediaPayload(local_path=out, ext="png", mime="image/png")]


def _shot_id_for_shot_no(env, shot_no: int) -> str:
    row = env.db.connect().execute(
        "SELECT id FROM shots WHERE shot_no = ?", (shot_no,)
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _keyframe_stage_run_rows(env):
    return env.db.connect().execute(
        "SELECT * FROM stage_runs WHERE stage = 's8_keyframes' ORDER BY subject_id"
    ).fetchall()





def _preseed_candidate_sheet(env, tmp_path, *, character_id, role, name, variant, version):
    """Insert a real image into the CAS plus a *candidate* sheet row."""
    import subprocess

    from media_env import _PRESEED_LAVFI

    out = tmp_path / f"{name}.png"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", _PRESEED_LAVFI[variant],
            "-frames:v", "1", str(out),
        ],
        check=True,
        capture_output=True,
    )
    asset = env.cas.put_file(out, kind="image", ext="png", retention="intermediate")
    return env.repos.sheets.create(
        series_id=env.seeded.series_id,
        character_id=character_id,
        role=role,
        version=version,
        asset_id=asset.sha256,
        spec_hash="preseeded-candidate",
        nonce=f"preseed-{name}",
        status="candidate",
    )


# ======================================================================
# T-KF-01 — ref selection, max_refs truncation, changed sheet stales keyframes
# ======================================================================


@pytest.mark.asyncio
async def test_kf_01_refs_selected_and_prompt_clauses(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch, filename="kf01a")
    full_shas = {
        CHAR_PRIMARY: preseed_approved_sheet(
            env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
            name="kf01-full-primary", variant="testsrc"),
        CHAR_SECOND: preseed_approved_sheet(
            env, tmp_path, character_id=CHAR_SECOND, role="ref_full",
            name="kf01-full-second", variant="smptebars"),
    }

    await run_to_g2_preseeded(env)

    outputs = {out["shot_id"]: out for out in stage_outputs(env, "s8_keyframes")}
    assert len(outputs) == 3
    # The stage output reports the *original* sheet asset shas (ref selection);
    # the wire request carries the deterministic prepared JPEGs.
    from studio.domain.refs import AssetRef
    from studio.media.refs import prepare_ref

    prepared: dict[str, str] = {}
    for character, sha in full_shas.items():
        row = env.repos.assets.get(sha)
        assert row is not None
        ref = AssetRef(
            sha256=row.sha256, rel_path=row.rel_path, kind=row.kind, mime=row.mime,
            size_bytes=row.size_bytes, duration_ms=row.duration_ms,
            width=row.width, height=row.height,
        )
        prepared[character] = (await prepare_ref(env.cas, ref, env.config.settings)).sha256

    for shot_no, expected_characters in ((1, [CHAR_PRIMARY]), (2, [CHAR_PRIMARY, CHAR_SECOND]), (3, [CHAR_PRIMARY])):
        shot_id = _shot_id_for_shot_no(env, shot_no)
        out = outputs[shot_id]
        refs = out["refs"]
        assert refs == [full_shas[name] for name in expected_characters]
        assert out["refs_truncated"] is False
        assert all(not w.startswith("refs_truncated") for w in out["warnings"])

    # The two-character shot references both prepared sheets on the wire,
    # in shot-plan order, with the matching ref clauses.
    wired = [
        request for request in env.image.requests
        if len(request.input_assets or []) == 2
    ]
    assert len(wired) == 1
    request = wired[0]
    assert [asset.sha256 for asset in request.input_assets] == [
        prepared[CHAR_PRIMARY], prepared[CHAR_SECOND],
    ]
    assert "reference image 1" in request.params["prompt"]
    assert "reference image 2" in request.params["prompt"]
    assert CHAR_PRIMARY in request.params["prompt"] and CHAR_SECOND in request.params["prompt"]


@pytest.mark.asyncio
async def test_kf_01_max_refs_truncation_warns(tmp_path, monkeypatch):
    # Two characters x two roles = 4 refs; mock.image max_refs overridden to 2.
    env = build_env(
        tmp_path,
        monkeypatch,
        filename="kf01b",
        settings_overrides={"character_sheets": {"roles": ["ref_full", "ref_face"]}},
        registry_overrides={"models": {"mock.image": {"media": {"max_refs": 2}}}},
    )
    for character, variant in ((CHAR_PRIMARY, "testsrc"), (CHAR_SECOND, "smptebars")):
        for role, offset in (("ref_full", 0), ("ref_face", 1)):
            preseed_approved_sheet(
                env, tmp_path, character_id=character, role=role,
                name=f"kf01-{character}-{role}-{offset}", variant=variant,
            )

    await run_to_g2_preseeded(env)

    outputs = {out["shot_id"]: out for out in stage_outputs(env, "s8_keyframes")}
    shot2 = _shot_id_for_shot_no(env, 2)
    out = outputs[shot2]
    assert out["refs_truncated"] is True
    assert "refs_truncated" in out["warnings"]
    assert len(out["refs"]) == 2
    # Solo shots stay untruncated (2 refs == max_refs).
    for shot_no in (1, 3):
        solo = outputs[_shot_id_for_shot_no(env, shot_no)]
        assert solo["refs_truncated"] is False


@pytest.mark.asyncio
async def test_kf_01_changed_sheet_stales_keyframes(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch, filename="kf01c")
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
        name="kf01c-primary", variant="testsrc")
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_SECOND, role="ref_full",
        name="kf01c-second", variant="smptebars")

    await run_to_g2_preseeded(env)
    before = {row["id"]: row["input_hash"] for row in _keyframe_stage_run_rows(env)}
    assert all(before.values())

    # Operator approves a *new* sheet revision for the primary character.
    _preseed_candidate_sheet(
        env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
        name="kf01c-primary-v2", variant="rgbtestsrc", version=2,
    )
    env.repos.sheets.approve_latest_candidates(env.seeded.series_id)

    approve_parked(env, "G2_KEYFRAMES", note="keyframes approved round 1")
    await drain(env)
    # Approving G2 with a changed sheet: S8 is stale, re-runs before G2 round 2.
    staleness = audits(env, "staleness_invalidated")
    assert staleness, "expected the engine to invalidate stale keyframes"
    staled_ids = {row_id for audit in staleness for row_id in audit["stage_runs"]}
    assert set(before) & staled_ids, "expected the keyframe rows to be staled"

    after = {row["id"]: row for row in _keyframe_stage_run_rows(env)}
    assert set(after) == set(before)
    for row_id, row in after.items():
        assert row["input_hash"] != before[row_id]


# ======================================================================
# T-KF-02 — native aspect request; unsupported aspect fails s4p; aspect QC
# ======================================================================


@pytest.mark.asyncio
async def test_kf_02_native_aspect_requested_via_aspect_values(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch, filename="kf02a")
    await run_to_g2(env)

    assert env.image.requests, "expected keyframe image calls"
    for request in env.image.requests:
        assert request.aspect == Aspect.TALL
    # Mock renders 9:16 at the configured 16:9 box -> 540x960; QC aspect passes.
    for row in stage_rows(env, "s8_keyframes"):
        assert row["status"] == "succeeded"
    for out in stage_outputs(env, "s8_keyframes"):
        checks = {check["name"]: check for check in out["qc"]}
        assert checks["aspect"]["status"] == "pass"


@pytest.mark.asyncio
async def test_kf_02_unsupported_aspect_fails_preflight_closed(tmp_path, monkeypatch):
    env = build_env(
        tmp_path,
        monkeypatch,
        filename="kf02b",
        registry_overrides={"models": {"mock.video": {"media": {"supported_aspects": ["16:9"]}}}},
    )
    await drain(env)
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    add_music_and_scene(env)
    approve_parked(env, "G1_SCRIPT", note="metadata approved")
    await drain(env)  # s4p runs and fails closed

    rows = stage_rows(env, "s4p_media_preflight")
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    failure = audits(env, "stage_run_failed")
    assert any(
        "does not support track aspect 9:16" in str(audit.get("error", ""))
        for audit in failure
    ), f"expected the aspect problem in the failure audit, got {failure}"
    # Closed before any media/audio spend.
    capabilities = {
        row["capability"]
        for row in provider_calls(env, capability=None)
    }
    assert "image_reference_generate" not in capabilities
    assert "image_to_video" not in capabilities
    assert "tts" not in capabilities
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == "failed"



@pytest.mark.asyncio
async def test_kf_02_aspect_qc_failure_never_retries(tmp_path, monkeypatch):
    inner = _make_inner(tmp_path, filename="kf02c")
    env = build_env(
        tmp_path,
        monkeypatch,
        filename="kf02c",
        image_provider=WideImageProvider(inner),
    )
    # Sheets are pre-approved so the wide-output mock is only exercised by S8.
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
        name="kf02c-primary", variant="testsrc")
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_SECOND, role="ref_full",
        name="kf02c-second", variant="smptebars")

    await drain(env)
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    add_music_and_scene(env)
    approve_parked(env, "G1_SCRIPT", note="metadata approved")
    await drain(env)  # s4p + audio + s8a (zero calls) + the first keyframe

    keyframe_calls = [
        row for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "keyframe"
    ]
    assert len(keyframe_calls) == 1, (
        f"a non-retryable QC failure must not retry; got {len(keyframe_calls)} keyframe calls"
    )
    rows = stage_rows(env, "s8_keyframes")
    assert any(row["status"] == "failed" for row in rows)
    assert audits(env, "media_circuit_open"), "expected the media_circuit_open audit row"
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == "failed"


def _make_inner(tmp_path, *, filename):
    from media_env import SpyImageProvider, _Ids

    mock_dir = tmp_path / f"{filename}-mock"
    mock_dir.mkdir(exist_ok=True)
    return SpyImageProvider(
        ledger_path=mock_dir / "image_jobs.json",
        width_16x9=960,
        height_16x9=540,
        ids=_Ids(f"{filename}-inner"),
        poll_ticks=2,
    )


# ======================================================================
# T-KF-03 — black image: retries q1, q2, then fail; non-retryable never retry
# ======================================================================


@pytest.mark.asyncio
async def test_kf_03_black_image_retries_q1_q2_then_fails(tmp_path, monkeypatch):
    inner = _make_inner(tmp_path, filename="kf03a")
    env = build_env(
        tmp_path,
        monkeypatch,
        filename="kf03a",
        image_provider=BlackImageProvider(inner),
    )
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
        name="kf03a-primary", variant="testsrc")
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_SECOND, role="ref_full",
        name="kf03a-second", variant="smptebars")

    await drain(env)
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    add_music_and_scene(env)
    approve_parked(env, "G1_SCRIPT", note="metadata approved")
    await drain(env)  # stops at the first keyframe's QC exhaustion (run failed)
    assert pending_gates(env, "G0_CHARACTERS") == []

    keyframe_calls = [
        row for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "keyframe"
    ]
    # 1 initial attempt + 2 QC regenerations (qc.image.auto_retries=2) on the
    # first failing shot; the run halts before any sibling shot is paid.
    assert [row["purpose"] for row in keyframe_calls] == ["keyframe"] * 3
    rows = _keyframe_stage_run_rows(env)
    failed = [row for row in rows if row["status"] == "failed"]
    assert len(failed) == 1
    attempts = env.db.connect().execute(
        "SELECT attempt_no, qc_status, sanitize_level FROM shot_attempts "
        "WHERE kind = 'keyframe'"
    ).fetchall()
    assert len(attempts) == 1
    assert attempts[0]["qc_status"] == "fail"
    assert attempts[0]["sanitize_level"] == 0
    failure = audits(env, "media_attempt_failed")
    assert any(audit.get("error_type") == "VisualQcError" for audit in failure), failure
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == "failed"


def test_kf_03_qc_aspect_check_is_non_retryable(tmp_path, monkeypatch):
    import asyncio
    import subprocess

    from conftest import make_home


    home = make_home(tmp_path, filename="kf03b")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    settings = load_all(home).settings
    wide = tmp_path / "wide.png"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=960x540:rate=24",
         "-frames:v", "1", str(wide)],
        check=True,
    )
    checks = asyncio.run(qc_image(wide, aspect=Aspect.TALL, settings=settings))
    aspect = next(c for c in checks if c.name == "aspect")
    assert aspect.status == "fail"
    assert aspect.retryable is False


# ======================================================================
# T-KF-04 — crash resume repays nothing; chain_mode last_frame raises
# ======================================================================


@pytest.mark.asyncio
async def test_kf_04_crash_resume_repays_nothing(tmp_path, monkeypatch):
    from datetime import timedelta

    import studio.media.stages as media_stages
    from studio.core.faults import SimulatedCrash

    env = build_env(tmp_path, monkeypatch, filename="kf04a")
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
        name="kf04a-primary", variant="testsrc")
    preseed_approved_sheet(
        env, tmp_path, character_id=CHAR_SECOND, role="ref_full",
        name="kf04a-second", variant="smptebars")

    original = media_stages.qc_image
    state = {"crashed": False}

    async def crasher(*args, **kwargs):
        if not state["crashed"]:
            state["crashed"] = True
            raise SimulatedCrash("crash inside keyframe QC")
        return await original(*args, **kwargs)

    monkeypatch.setattr(media_stages, "qc_image", crasher)

    await drain(env)
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    add_music_and_scene(env)
    approve_parked(env, "G1_SCRIPT", note="metadata approved")
    with pytest.raises(SimulatedCrash):
        await drain(env)  # mid-S8 crash: one keyframe call was paid

    submits_at_crash = env.image.submit_count
    assert submits_at_crash == 1
    paid_calls_at_crash = len(provider_calls(env, capability="image_reference_generate"))
    assert paid_calls_at_crash == 1

    # Restart past the staleness window on a fresh engine, then resume.
    from studio.core.clock import no_op_sleep
    from studio.pipeline.dag import Dag
    from studio.pipeline.engine import Engine
    from studio.pipeline.specs import production_specs
    from studio.pipeline.worker import Worker

    env.clock.advance(timedelta(seconds=3600))
    engine2 = Engine(
        db=env.db, repos=env.repos, dag=Dag(production_specs()),
        clock=env.clock, config=env.config, ids=env.ids, gateway=env.gateway,
    )
    worker2 = Worker(
        env.db, engine2, env.repos, env.config, env.clock,
        worker_id="kf04a-worker-2", sleep=no_op_sleep,
    )
    await worker2.run(until_idle=True)

    assert env.image.submit_count == 3, (
        f"crash resume must not re-pay: submit_count={env.image.submit_count}"
    )
    keyframe_calls = [
        row for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "keyframe"
    ]
    assert len(keyframe_calls) == 3
    recovered = audits(env, "job_recovery_requeued")
    assert recovered, "expected the crashed job to be recovered and requeued"
    assert pending_gates(env, "G2_KEYFRAMES"), "expected the keyframes gate after resume"


def test_kf_04_chain_mode_last_frame_raises_config_error(tmp_path, monkeypatch):
    from conftest import make_home

    from studio.domain.errors import ConfigError

    home = make_home(
        tmp_path,
        filename="kf04b",
        settings_overrides={"media": {"chain_mode": "last_frame"}},
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    with pytest.raises(ConfigError, match="last_frame"):
        load_all(home)


# ======================================================================
# T-KF-05 — studio shot regenerate: one shot, new nonce, cost shown, --yes
# ======================================================================


@pytest.mark.asyncio
async def test_kf_05_shot_regenerate_only_that_shot(tmp_path, monkeypatch):
    from studio.cli import app

    env = build_env(tmp_path, monkeypatch, filename="kf05")
    await run_to_s10(env)

    keyframes_before = env.db.connect().execute(
        "SELECT shot_id, attempt_no, nonce FROM shot_attempts WHERE kind = 'keyframe'"
    ).fetchall()
    clips_before = env.db.connect().execute(
        "SELECT shot_id, attempt_no, nonce, conformed_asset_id FROM shot_attempts WHERE kind = 'clip'"
    ).fetchall()
    assert len(keyframes_before) == 3
    assert len(clips_before) == 3
    raw_shas = {
        row["shot_id"]: row["conformed_asset_id"]
        for row in clips_before
    }
    run_id = env.run_row.id
    target = _shot_id_for_shot_no(env, 2)
    other_shots = {row["id"] for row in env.db.connect().execute("SELECT id FROM shots").fetchall()}
    other_shots.discard(target)

    # Refusal without --yes: nothing is spent and no attempt is created.
    result = RUNNER.invoke(app, ["shot", "regenerate", run_id, target, "--stage", "keyframe"])
    assert "nothing spent" in result.output
    attempts_after_refused = env.db.connect().execute(
        "SELECT count(*) AS n FROM shot_attempts WHERE kind = 'keyframe' AND shot_id = ?",
        (target,),
    ).fetchone()["n"]
    assert attempts_after_refused == 1

    # Proceed with --yes.
    result = RUNNER.invoke(app, ["shot", "regenerate", run_id, target,
                                 "--stage", "keyframe", "--yes",
                                 "--reason", "operator rework"])
    assert result.exit_code == 0, result.output
    assert "worst-case reserve" in result.output

    rows = env.db.connect().execute(
        "SELECT shot_id, attempt_no, nonce FROM shot_attempts WHERE kind = 'keyframe'"
    ).fetchall()
    by_shot = {}
    for row in rows:
        by_shot.setdefault(row["shot_id"], []).append((row["attempt_no"], row["nonce"]))
    # attempt_no is a per-shot counter shared across kinds (keyframe=1, clip=2,
    # the CLI regenerate=3); what matters is that exactly one new attempt was
    # created and it carries a fresh nonce.
    target_attempts = sorted(a for a, _ in by_shot[target])
    assert len(target_attempts) == 2
    assert target_attempts[1] > target_attempts[0]
    nonces = [n for _, n in sorted(by_shot[target])]
    assert nonces[0] != nonces[1]
    for shot_id in other_shots:
        assert [n for n, _ in sorted(by_shot[shot_id])] == [1]

    # Bridge the harness clock gap: the CLI enqueues with the system clock,
    # the test worker claims against the fake clock.
    from studio.core.clock import SystemClock
    env.clock.set(SystemClock().now())
    await drain(env)
    clip_rows = env.db.connect().execute(
        "SELECT status FROM stage_runs WHERE stage = 's10_clips' AND subject_id = ?",
        (target,),
    ).fetchall()
    assert clip_rows, "expected a clip stage run for the target shot"
    # The CLI marked it stale; the engine flipped it back to pending for the
    # next round. Either way it must NOT be 'succeeded' without a re-drive.
    assert {row["status"] for row in clip_rows} <= {"stale", "pending"}
    # Untouched shots keep succeeded clip rows.
    other_clip_rows = env.db.connect().execute(
        "SELECT subject_id, status FROM stage_runs WHERE stage = 's10_clips'"
    ).fetchall()
    other_statuses = {
        row["subject_id"]: row["status"] for row in other_clip_rows
    }
    for shot_id in other_shots:
        assert other_statuses[shot_id] == "succeeded"
    # The drain regenerates exactly the target shot's keyframe (one paid
    # mock call) and re-parks at G2; the clip never re-runs before the
    # operator re-approves.
    keyframe_calls = [
        row for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "keyframe"
    ]
    assert len(keyframe_calls) == 4  # 3 initial + 1 regenerate
    clip_calls = [
        row for row in provider_calls(env, capability="image_to_video")
        if row["purpose"] == "clip"
    ]
    assert len(clip_calls) == 3  # clips staled, never re-paid pre-approval
    gate = pending_gates(env, "G2_KEYFRAMES")
    assert len(gate) == 1 and gate[0]["round"] == 2
    raw_sha = raw_shas[target]
    assert env.cas.resolve(raw_sha).exists()
    asset = env.repos.assets.get(raw_sha)
    assert asset is not None and asset.retention == "keep_n_days"

    regenerate = audits(env, "shot_regenerate_requested")
    assert any(audit.get("shot_id") == target for audit in regenerate)
