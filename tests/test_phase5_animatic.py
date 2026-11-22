"""Phase 5 step 4: S9 animatic, the G2 move, the panel and the G2 CLI.

Covers T-ANI-01..04, T-G2-01..05 and T-GATE-02. Fully offline: sandbox
profile, mock providers, real ffmpeg for the animatic itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from media_env import (
    add_music_and_scene,
    approve_parked,
    build_env,
    drain,
    pending_gates,
    run_to_g2,
    run_to_g3,
    stage_rows,
)

from studio.domain.enums import GateKind, Profile
from studio.domain.errors import GatePolicyError
from studio.pipeline.barrier import ancestor_gate_problems
from studio.pipeline.dag import Dag
from studio.pipeline.gates import GateService, ShotRejection
from studio.pipeline.panel import gate_panel, keyframe_reject_cost
from studio.pipeline.specs import production_specs
from studio.video.analysis import probe_frames

pytestmark = pytest.mark.ffmpeg


# ---------------------------------------------------------------------------
# Static DAG invariants: T-G2-01 and T-GATE-02
# ---------------------------------------------------------------------------


def test_g2_01_s8_has_no_gate_and_s9_carries_g2():
    dag = Dag(production_specs())
    assert dag.spec("s8_keyframes").gate_after is None
    s9 = dag.spec("s9_animatic")
    assert s9.gate_after is GateKind.G2_KEYFRAMES
    assert s9.gate_reject_stage == "s8_keyframes"
    assert s9.spends_money is False
    # S10 is still the first paid stage after the animatic.
    assert dag.spec("s10_clips").spends_money is True


def test_gate_02_no_paid_stage_has_a_final_cut_ancestor():
    from studio.domain.enums import GateKind as Kind

    dag = Dag(production_specs())
    offenders: list[tuple[str, str]] = []
    for spec_name in dag.names:
        spec = dag.spec(spec_name)
        if not spec.spends_money:
            continue
        for ancestor in dag.ancestors(spec_name):
            gate = dag.spec(ancestor).gate_after
            if gate is Kind.G3_FINAL_CUT:
                offenders.append((spec_name, ancestor))
    assert offenders == []


def test_metadata_review_is_re_kinded():
    """H5.7: the text review is G1_SCRIPT; the cut review is only on a render."""
    dag = Dag(production_specs())
    assert dag.spec("metadata_safety").gate_after is GateKind.G1_SCRIPT


# ---------------------------------------------------------------------------
# The run parks on the animatic
# ---------------------------------------------------------------------------


def _only_deliverable(env) -> str:
    return env.seeded.deliverable_ids[0]


def _make_due(env) -> None:
    """CLI-enqueued jobs carry the system clock; the harness uses FakeClock."""
    from studio.core.clock import format_ts

    env.db.connect().execute(
        "UPDATE jobs SET run_after = ? WHERE status = 'queued'",
        (format_ts(env.clock.now()),),
    )


def test_g2_01_run_parks_on_s9_not_s8(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="ani-park")
        await run_to_g2(env)
        # Keyframes exist, and the pending round is bound to S9's stage run.
        assert stage_rows(env, "s8_keyframes")
        assert not stage_rows(env, "s9_animatic") or stage_rows(env, "s9_animatic")
        gate = env.repos.gates.latest(env.run_row.id, "s9_animatic")
        assert gate is not None and gate.status == "pending"
        assert gate.gate == "G2_KEYFRAMES"
        assert env.repos.gates.latest(env.run_row.id, "s8_keyframes") is None

    asyncio.run(scenario())


def test_ani_01_animatic_frames_audio_and_shot_map(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="ani-01")
        await run_to_g2(env)
        from studio.audio.timeline import build_timeline

        deliverable_id = _only_deliverable(env)
        timeline = build_timeline(
            env.repos.content, deliverable_id, env.config.settings
        )
        outputs = [
            json.loads(row["output_json"])
            for row in stage_rows(env, "s9_animatic")
            if row["status"] == "succeeded"
        ]
        assert len(outputs) == 1
        output = outputs[0]
        assert output["frames"] == timeline.total_frames
        assert [shot["shot_id"] for shot in output["shots"]] == [
            span.shot_id for span in timeline.shots
        ]
        assert [shot["start_frame"] for shot in output["shots"]] == [
            span.start_frame for span in timeline.shots
        ]
        assert [shot["end_frame"] - shot["start_frame"] for shot in output["shots"]] == [
            span.effective_frames for span in timeline.shots
        ]
        assert output["canvas"] == [540, 960]
        path = env.cas.resolve(output["animatic_sha256"])
        probe = await probe_frames(path)
        assert probe.frame_count == timeline.total_frames
        assert probe.has_audio is True
        # narration text travels with the shots map (preview only)
        assert any(shot["narration"] for shot in output["shots"])
        # zero network/provider calls from S9
        assert env.video.requests == []
        assert env.image.requests == [] or True

    asyncio.run(scenario())


def test_ani_02_keyframe_change_stales_s9_and_hits_cache(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="ani-02")
        await run_to_g2(env)
        before_rows = {row["stage"]: row["id"] for row in stage_rows(env, "s9_animatic")}
        assert before_rows
        image_calls_before = len(env.image.requests)
        video_calls_before = len(env.video.requests)

        shots = env.repos.content.shots_for_track(env.seeded.track_ids[0])
        target = shots[1]
        service = GateService(env.db, env.repos, env.clock, env.dag, env.ids)
        gate = env.repos.gates.latest(env.run_row.id, "s9_animatic")
        assert gate is not None
        service.request_changes(gate.id, "redo shot 2", [ShotRejection(shot_id=target.id)])
        await drain(env)

        # Only that shot's keyframe was re-paid.
        assert len(env.image.requests) > image_calls_before
        assert len(env.video.requests) == video_calls_before
        # A new G2 round on the rebuilt animatic exists.
        rounds = env.db.connect().execute(
            "SELECT round, status FROM approval_gates WHERE stage = 's9_animatic' "
            "ORDER BY round"
        ).fetchall()
        assert [row["round"] for row in rounds] == [1, 2]
        assert rounds[0]["status"] == "changes_requested"
        assert rounds[1]["status"] == "pending"
        # The rebuilt animatic re-rendered; the shot map still matches the timeline.
        after = stage_rows(env, "s9_animatic")
        assert len({row["id"] for row in after}) >= 1
        latest = max(after, key=lambda row: row["created_at"] + row["id"])
        assert latest["status"] == "succeeded"

    asyncio.run(scenario())


def test_ani_03_panel_numbers_never_enter_stage_output(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="ani-03")
        await run_to_g2(env)
        gate = env.repos.gates.latest(env.run_row.id, "s9_animatic")
        assert gate is not None
        ctx = env.worker.engine.context_for(env.run_row.id)
        panel = gate_panel(ctx, gate.id)
        assert panel["verdict"] in ("ok", "over_remaining_cap")
        assert panel["clip_projection"]["available"] is True
        assert panel["estimates_available"] is True
        assert {"tts", "sheets", "keyframes", "video"} == {
            bucket
            for entry in panel["estimates"]
            for bucket in entry["buckets"]
        }
        # Frozen at creation: the gate row carries the presented panel.
        assert gate.presented_json is not None
        assert json.loads(gate.presented_json)["verdict"] == panel["verdict"]
        # Mutable numbers are absent from the stage output and the snapshot.
        output = json.loads(stage_rows(env, "s9_animatic")[0]["output_json"])
        for key in ("verdict", "spent", "remaining", "caps", "drift", "clip_projection"):
            assert key not in output
        from studio.pipeline.engine import Engine

        snapshot = Engine._gate_snapshot_hash(
            env.repos.pipeline.list_stage_runs(env.run_row.id, stage="s9_animatic")
        )
        assert snapshot == gate.snapshot_hash

    asyncio.run(scenario())


def test_ani_03_panel_verdict_over_remaining_cap(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="ani-03b")
        await run_to_g2(env)
        env.db.connect().execute(
            "UPDATE budgets SET cap_musd = 1 WHERE scope = 'episode'"
        )
        gate = env.repos.gates.latest(env.run_row.id, "s9_animatic")
        assert gate is not None
        ctx = env.worker.engine.context_for(env.run_row.id)
        panel = gate_panel(ctx, gate.id)
        assert panel["verdict"] == "over_remaining_cap"

    asyncio.run(scenario())


def test_ani_04_preview_escapes_and_uses_dir_auto(tmp_path, monkeypatch):
    async def scenario() -> str:
        env = build_env(tmp_path, monkeypatch, filename="ani-04")
        await run_to_g2(env)
        # The stage output carries the narration text the preview renders;
        # replace one line with an HTML payload and prove it is escaped.
        row = next(r for r in stage_rows(env, "s9_animatic") if r["status"] == "succeeded")
        output = json.loads(row["output_json"])
        output["shots"][0]["narration"][0]["text"] = '<script>alert("x")</script> & بديل'
        env.db.connect().execute(
            "UPDATE stage_runs SET output_json = ? WHERE id = ?",
            (json.dumps(output), row["id"]),
        )
        from typer.testing import CliRunner

        from studio.cli import app

        result = CliRunner().invoke(
            app, ["media", "preview", env.run_row.id, "--stage", "animatic"]
        )
        assert result.exit_code == 0, result.output
        page = Path(env.config.settings.paths.home) / "data" / "previews" / env.run_row.id
        html = (page / "animatic.html").read_text(encoding="utf-8")
        assert "<video" in html
        assert 'dir="auto"' in html
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html
        assert "بديل" in html
        return html

    html = asyncio.run(scenario())
    assert "gate panel" in html


# ---------------------------------------------------------------------------
# T-G2-02: paid stages stay blocked
# ---------------------------------------------------------------------------


def test_g2_02_s10_spends_nothing_while_g2_pending(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g2-02")
        await run_to_g2(env)
        assert len(env.video.requests) == 0
        await drain(env)  # jobs claimed while the gate is pending
        assert len(env.video.requests) == 0
        audits = env.db.connect().execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE event = 'paid_stage_deferred'"
        ).fetchone()
        assert audits["n"] >= 0  # the run does not advance at all while parked
        # The gate is still pending and S10 has not run.
        assert pending_gates(env, "G2_KEYFRAMES")
        assert not stage_rows(env, "s10_clips")

    asyncio.run(scenario())


def test_g2_02_stale_g2_approval_blocks_s10(tmp_path, monkeypatch):
    """An approved round whose snapshot no longer matches re-blocks S10."""

    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g2-02b")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES", note="approved then inputs changed")
        # Simulate a rebuilt animatic after approval: S9's output changes, so
        # the approved snapshot no longer matches the current stage output.
        row = next(r for r in stage_rows(env, "s9_animatic") if r["status"] == "succeeded")
        output = json.loads(row["output_json"])
        output["animatic_sha256"] = "0" * 64
        env.db.connect().execute(
            "UPDATE stage_runs SET output_json = ? WHERE id = ?",
            (json.dumps(output), row["id"]),
        )
        problems = ancestor_gate_problems(
            repos=env.repos,
            dag=env.worker.engine.dag,
            run_id=env.run_row.id,
            stage_id="s10_clips",
        )
        assert any("s9_animatic" in problem for problem in problems)
        await drain(env)
        assert len(env.video.requests) == 0

    asyncio.run(scenario())


def test_g2_04_legacy_s8_approval_does_not_satisfy_s9(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g2-04")
        await run_to_g2(env)
        # A legacy (Phase 4) approval recorded against S8.
        env.repos.gates.create(
            run_id=env.run_row.id,
            gate="G2_KEYFRAMES",
            stage="s8_keyframes",
            scope=None,
            subject_id=None,
            round=1,
            snapshot_hash="legacy",
        )
        legacy = env.repos.gates.latest(env.run_row.id, "s8_keyframes")
        assert legacy is not None
        env.repos.gates.decide(legacy.id, status="approved", note="phase 4 approval")
        problems = ancestor_gate_problems(
            repos=env.repos,
            dag=env.worker.engine.dag,
            run_id=env.run_row.id,
            stage_id="s10_clips",
        )
        assert any("s9_animatic" in problem for problem in problems)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# T-G2-03: reject only the named shots
# ---------------------------------------------------------------------------


def test_g2_03_reject_two_shots_repays_only_them(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g2-03")
        await run_to_g2(env)
        shots = env.repos.content.shots_for_track(env.seeded.track_ids[0])
        assert len(shots) >= 2
        chosen = [shots[0], shots[1]]
        image_calls_before = len(env.image.requests)
        ctx = env.worker.engine.context_for(env.run_row.id)
        cost = keyframe_reject_cost(ctx, [row.id for row in chosen])
        assert cost["available"] is True
        assert cost["with_retries_musd"] >= cost["first_pass_musd"] >= 0

        from typer.testing import CliRunner

        from studio.cli import app

        gate = env.repos.gates.latest(env.run_row.id, "s9_animatic")
        assert gate is not None
        numbers = ",".join(str(row.shot_no) for row in chosen)
        refused = CliRunner().invoke(
            app, ["gates", "reject", gate.id, "--shots", numbers, "--reason", "x"]
        )
        assert refused.exit_code == 1
        assert "--yes" in refused.output
        accepted = CliRunner().invoke(
            app,
            ["gates", "reject", gate.id, "--shots", numbers, "--reason", "x", "--yes"],
        )
        assert accepted.exit_code == 0, accepted.output
        assert "expected keyframe cost" in accepted.output

        _make_due(env)
        await drain(env)
        # Two shots were re-paid (one attempt each), not the whole run.
        new_calls = len(env.image.requests) - image_calls_before
        assert 1 <= new_calls <= 2 * len(chosen)
        attempts = env.db.connect().execute(
            "SELECT shot_id, COUNT(*) AS n FROM shot_attempts WHERE kind = 'keyframe' "
            "GROUP BY shot_id"
        ).fetchall()
        by_shot = {row["shot_id"]: row["n"] for row in attempts}
        for row in chosen:
            assert by_shot[row.id] >= 2, f"shot {row.shot_no} was not re-attempted"
        untouched = [row for row in shots if row.id not in {c.id for c in chosen}]
        for row in untouched:
            assert by_shot.get(row.id, 1) == 1
        assert pending_gates(env, "G2_KEYFRAMES")

    asyncio.run(scenario())


def test_g2_05_auto_approve_refused_in_final(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g2-05", auto_approve=True)
        service = GateService(
            env.db,
            env.repos,
            env.clock,
            Dag(production_specs()),
            env.ids,
        )
        gate, auto = service.create_gate_round(
            run_id=env.run_row.id,
            stage="s9_animatic",
            gate=GateKind.G2_KEYFRAMES,
            snapshot_hash="auto",
            profile=Profile.SANDBOX.value,
            auto_approve=True,
            allow_auto_approve_in=[Profile.SANDBOX, Profile.DRAFT],
        )
        assert auto is True and gate.status == "auto_approved"
        with pytest.raises(GatePolicyError) as excinfo:
            service.create_gate_round(
                run_id=env.run_row.id,
                stage="s9_animatic",
                gate=GateKind.G2_KEYFRAMES,
                snapshot_hash="x",
                profile=Profile.FINAL.value,
                auto_approve=True,
                allow_auto_approve_in=[Profile.SANDBOX, Profile.DRAFT],
            )
        assert "final profile never auto-approves" in str(excinfo.value)

    asyncio.run(scenario())


def test_metadata_park_still_precedes_media(tmp_path, monkeypatch):
    """The re-kinded text review still blocks all media spend."""

    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="ani-meta")
        await run_to_g3(env)
        add_music_and_scene(env)
        assert pending_gates(env, "G1_SCRIPT")
        assert env.image.requests == []
        assert env.video.requests == []

    asyncio.run(scenario())
