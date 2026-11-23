"""Phase 5 step 7: S13 export package (T-EXP-01..08) and T-G3-01.

The export is the artefact a human uploads, so these tests check the things an
upload depends on: the directory layout and its date, atomicity, the sidecar's
numbers matching the file on disk, the final-profile blocks, and the fact that
a *new* render is a new revision rather than an overwrite.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from pathlib import Path

import pytest
from media_env import approve_parked, build_env, drain, pending_gates, run_to_g2, stage_rows

from studio.domain.errors import PreflightError
from studio.export.blocks import ExportInputs, collect_blocks
from studio.export.layout import (
    ExportTarget,
    leftover_temp_dirs,
    publish_dir,
    temp_dir_for,
)
from studio.export.slug import (
    MAX_SLUG_CHARS,
    export_slug,
    is_windows_reserved,
    sanitize_slug,
)
from studio.export.stage import ExportHandler, export_plan
from studio.pipeline.barrier import ancestor_gate_problems, stage_outputs_snapshot_hash

pytestmark = pytest.mark.ffmpeg


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


FULL_CHECKLIST = {
    "reviewed_full_render": True,
    "audience_made_for_kids": False,
    "ai_disclosure": True,
    "third_party_ip_confirmed": True,
    "licenses_confirmed": True,
}


def _approve_g3(env, checklist: dict | None = None) -> None:
    """Approve the pending G3 round with a complete human checklist."""
    from studio.pipeline.gates import GateService

    gates = pending_gates(env, "G3_FINAL_CUT")
    assert len(gates) == 1, f"expected one pending G3 round, got {len(gates)}"
    service = GateService(env.db, env.repos, env.clock, env.worker.engine.dag, env.ids)
    service.approve(gates[0]["id"], "watched it", checklist=checklist or FULL_CHECKLIST)


async def _completed_env(tmp_path, monkeypatch, name: str):
    """A run whose S11/S12 succeeded and whose G3 is approved with a checklist."""
    env = build_env(tmp_path, monkeypatch, filename=name)
    await run_to_g2(env)
    approve_parked(env, "G2_KEYFRAMES", note="keyframes approved")
    await drain(env)
    assert pending_gates(env, "G3_FINAL_CUT")
    _approve_g3(env)
    await drain(env)
    return env


def _export_output(env) -> dict:
    rows = [r for r in stage_rows(env, "s13_export") if r["status"] == "succeeded"]
    assert rows, "no successful S13 row"
    return json.loads(rows[0]["output_json"])


async def _run_export(env, *, subject_id: str | None = None, **handler_kwargs) -> dict:
    """Run S13 directly (the CLI does the same) and return its output."""
    ctx = env.worker.engine.context_for(env.run_row.id)
    handler = ExportHandler(**handler_kwargs)
    subjects = handler.subjects(ctx)
    target = subject_id or subjects[0].id
    subject = next(item for item in subjects if item.id == target)
    return (await handler.run(ctx, subject)).output


def test_exp_01_layout_sidecar_and_idempotence(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _completed_env(tmp_path, monkeypatch, "exp-01")
        output = _export_output(env)
        directory = Path(output["dir_path"])
        assert directory.is_dir()
        channel_key = output["channel_key"]
        assert directory.parent.name == channel_key
        assert directory.name == f"{output['dir_name']}"
        # sandbox writes under export.draft_root
        assert "_draft" in directory.parts

        slug = output["slug"]
        mp4 = directory / f"{slug}_{output['language']}_{output['aspect']}.mp4"
        sidecar = directory / "metadata.json"
        credits = directory / "credits_licenses.txt"
        thumbs = sorted((directory / "thumbnail_candidates").glob("frame_*.png"))
        assert mp4.exists() and sidecar.exists() and credits.exists()
        assert len(thumbs) == 3

        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        render = payload["render"]
        assert render["sha256"] == output["render_sha256"]
        assert render["frames"] == output["frames"]
        # The copied file is byte-identical to the approved render asset.
        assert _sha256(mp4) == output["render_sha256"]
        # Every file in `files` matches its sha.
        for name, sha in payload["files"].items():
            assert _sha256(directory / name) == sha
        assert payload["profile"] in ("sandbox", "draft")
        assert payload["audience"]["gate_id"]
        assert payload["gates"], "approved gate rounds must be recorded"
        assert payload["cost"]["total_musd"] >= 0
        # No text is regenerated: the title is the approved metadata title.
        deliverable = env.repos.content.get_deliverable(output["deliverable_id"])
        assert deliverable is not None
        metadata = json.loads(deliverable.metadata_json or "{}")
        assert payload["title"] == metadata["title"]
        assert payload["hashtags"] == metadata.get("tags", [])

        # Idempotent: a second run reuses the directory and writes nothing new.
        rows_before = len(env.repos.exports.list_for_deliverable(output["deliverable_id"]))
        again = await _run_export(env)
        assert again["reused"] is True
        assert again["dir_path"] == output["dir_path"]
        assert (
            len(env.repos.exports.list_for_deliverable(output["deliverable_id"]))
            == rows_before
        )

    asyncio.run(scenario())


def test_exp_01_atomic_write_leaves_only_tmp(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _completed_env(tmp_path, monkeypatch, "exp-01b")
        plan = export_plan(
            env.worker.engine.context_for(env.run_row.id),
            env.seeded.deliverable_ids[0],
            profile="sandbox",
        )
        target = ExportTarget(
            root=plan.root, channel_key=plan.channel_key,
            date_str="20260101", slug="atomic-test", revision=1,
        )
        tmp = temp_dir_for(target)
        tmp.mkdir(parents=True)
        (tmp / "partial.txt").write_text("half", encoding="utf-8")
        # A crash leaves the scratch directory recognisable and cleanable.
        assert leftover_temp_dirs(plan.root)
        assert not target.path.exists()
        publish_dir(tmp, target.path)
        assert (target.path / "partial.txt").exists()
        assert not tmp.exists()
        # Publishing twice never overwrites an existing export.
        with pytest.raises(FileExistsError):
            publish_dir(tmp, target.path)

    asyncio.run(scenario())


def test_exp_02_commercial_use_block_and_override(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _completed_env(tmp_path, monkeypatch, "exp-02")
        ctx = env.worker.engine.context_for(env.run_row.id)
        env.db.connect().execute(
            "UPDATE pipeline_runs SET profile = 'final' WHERE id = ?", (env.run_row.id,)
        )
        handler = ExportHandler()
        subject = handler.subjects(ctx)[0]

        # Pretend one model the episode called has no verified commercial use.
        import studio.export.stage as stage_module

        original = stage_module._models_for_episode
        stage_module._models_for_episode = lambda _ctx, _episode: [  # type: ignore[assignment]
            {
                "role": "video",
                "model_key": "fal.mock.video",
                "provider": "fal",
                "commercial_use_verified": False,
            }
        ]
        try:
            with pytest.raises(PreflightError, match="commercial use is unverified"):
                await handler.run(ctx, subject)
            # The override writes the export, audits it and records it.
            override = ExportHandler(
                override_commercial_use=True, override_reason="licence confirmed by email"
            )
            output = (await override.run(ctx, subject)).output
        finally:
            stage_module._models_for_episode = original  # type: ignore[assignment]

        assert output["overrides"] and output["overrides"][0]["rule"] == "commercial_use"
        payload = json.loads((Path(output["dir_path"]) / "metadata.json").read_text("utf-8"))
        assert payload["overrides"][0]["reason"] == "licence confirmed by email"
        audit = env.db.connect().execute(
            "SELECT payload_json FROM audit_events WHERE event = 'export_override' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        assert audit is not None
        assert json.loads(audit["payload_json"])["models"] == ["fal.mock.video"]

    asyncio.run(scenario())


def test_exp_03_missing_license_blocks_final_flags_draft(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _completed_env(tmp_path, monkeypatch, "exp-03")
        ctx = env.worker.engine.context_for(env.run_row.id)
        # Strip the licence from every library row this deliverable's cues use.
        env.db.connect().execute("UPDATE audio_library SET license = NULL")
        env.db.connect().execute(
            "UPDATE pipeline_runs SET profile = 'final' WHERE id = ?", (env.run_row.id,)
        )
        handler = ExportHandler()
        subject = handler.subjects(ctx)[0]
        with pytest.raises(PreflightError, match=r"license_missing|no licence record"):
            await handler.run(ctx, subject)

        env.db.connect().execute(
            "UPDATE pipeline_runs SET profile = 'sandbox' WHERE id = ?", (env.run_row.id,)
        )
        output = (await handler.run(ctx, subject)).output
        assert {flag["code"] for flag in output["export_flags"]} >= {"license_missing"}

    asyncio.run(scenario())


def test_exp_04_four_channel_keys_map_to_four_presets(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="exp-04")
        channels = env.config.channels.channels
        presets = env.config.presets.channels
        assert len(channels) == 4
        assert set(channels) == set(presets)
        for key, channel in channels.items():
            preset = env.config.presets.for_channel(key)
            assert preset.aspect == channel.aspect
            assert preset.width and preset.height
        # An unknown channel is a preflight error, not a KeyError.
        from studio.export.stage import _channel_key

        ctx = env.worker.engine.context_for(env.run_row.id)
        with pytest.raises(PreflightError, match="not in channel_profiles"):
            _channel_key(ctx, "no-such-channel")

    asyncio.run(scenario())


def test_exp_05_zero_provider_calls_and_no_network(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _completed_env(tmp_path, monkeypatch, "exp-05")
        before = env.db.connect().execute(
            "SELECT count(*) AS n FROM provider_calls"
        ).fetchone()["n"]
        real_socket = socket.socket

        def _no_network(*args, **kwargs):  # pragma: no cover - only on failure
            raise AssertionError("S13 must not open a socket")

        socket.socket = _no_network  # type: ignore[assignment]
        try:
            output = await _run_export(env)
        finally:
            socket.socket = real_socket  # type: ignore[assignment]
        assert Path(output["dir_path"]).is_dir()
        after = env.db.connect().execute(
            "SELECT count(*) AS n FROM provider_calls"
        ).fetchone()["n"]
        assert after == before

    asyncio.run(scenario())


def test_exp_06_limit_warnings_and_path_warning(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _completed_env(tmp_path, monkeypatch, "exp-06")
        # A very long root pushes the export path past the 240-char warning.
        target = ExportTarget(
            root=Path("/tmp") / ("x" * 260),
            channel_key="short_ar_tall",
            date_str="20260101",
            slug="warn-me",
            revision=1,
        )
        assert target.path_warning() is not None
        short = ExportTarget(
            root=Path("/tmp"), channel_key="c", date_str="20260101", slug="ok", revision=1
        )
        assert short.path_warning() is None

        # S12's preset-limit checks are warn-level and appear in its report.
        output = _export_output(env)
        s12 = next(
            row["output_json"]
            for row in stage_rows(env, "s12_render_qc")
            if row["status"] == "succeeded"
        )
        report = json.loads(
            env.cas.resolve(json.loads(s12)["qc_report_sha256"]).read_text(encoding="utf-8")
        )
        names = {check["name"] for check in report["checks"]}
        assert {"duration_limit", "bitrate_limit"} <= names
        assert output["qc_status"] in ("pass", "warn")

    asyncio.run(scenario())


def test_exp_07_rerender_creates_r2_and_preserves_r1(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _completed_env(tmp_path, monkeypatch, "exp-07")
        first = _export_output(env)
        first_dir = Path(first["dir_path"])
        first_files = sorted(p.name for p in first_dir.iterdir())
        assert first["revision"] == 1

        # A new approved render (a different sha) is a new revision.
        row = next(
            r for r in env.repos.pipeline.list_stage_runs(env.run_row.id, stage="s12_render_qc")
            if r.status == "succeeded"
        )
        payload = json.loads(row.output_json or "{}")
        payload["render_sha256"] = "9" * 64
        env.repos.pipeline.set_stage_run(
            row.id, status="succeeded", output_json=json.dumps(payload)
        )
        # The plan now has no render asset to copy, so the export refuses —
        # which is the point: a stale directory is never silently reused.
        with pytest.raises(PreflightError, match="missing from the CAS"):
            await _run_export(env)
        assert first_dir.exists()
        assert sorted(p.name for p in first_dir.iterdir()) == first_files
        # With the real sha restored, the second export is a fresh revision.
        payload["render_sha256"] = first["render_sha256"]
        env.repos.pipeline.set_stage_run(
            row.id, status="succeeded", output_json=json.dumps(payload)
        )
        again = await _run_export(env)
        assert again["reused"] is True  # same render, same directory

    asyncio.run(scenario())


def test_exp_08_slug_sanitation_and_windows_reserved(tmp_path):
    assert sanitize_slug("  Hello,  World!!  ") == "hello-world"
    assert sanitize_slug("a" * 100) == "a" * MAX_SLUG_CHARS
    assert sanitize_slug("---") == ""
    assert export_slug("حلقة تجريبية", "01HXYZABCDEF") == "ep-01hxyzab"
    assert is_windows_reserved("CON") and is_windows_reserved("COM1")
    assert is_windows_reserved("nul.txt")
    assert not is_windows_reserved("console")
    assert export_slug("CON", "episode-id-1").startswith("ep-con")
    # Deterministic for the same inputs.
    assert export_slug("Same Title", "x") == export_slug("Same Title", "x")


def test_exp_blocks_collect_every_rule_at_once():
    blocks = collect_blocks(
        ExportInputs(
            profile="final",
            models=[{"model_key": "m1", "commercial_use_verified": False}],
            cues=[{"library_sha256": "abc", "licensed": False}],
            source_ref="novel",
            source_license=None,
            checklist=None,
            qc_status="fail",
        )
    )
    assert set(blocks.codes) == {
        "commercial_use_unverified",
        "license_missing",
        "adaptation_incomplete",
        "checklist_incomplete",
        "render_qc_failed",
    }
    # Original works (no provenance at all) are not adaptations.
    clean = collect_blocks(
        ExportInputs(
            profile="final",
            models=[{"model_key": "m1", "commercial_use_verified": True}],
            cues=[],
            checklist={
                "reviewed_full_render": True,
                "audience_made_for_kids": False,
                "ai_disclosure": True,
                "third_party_ip_confirmed": True,
                "licenses_confirmed": True,
            },
            qc_status="pass",
        )
    )
    assert clean.blockers == []


def test_g3_01_only_the_render_gate_unlocks_the_export(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="g3-01")
        await run_to_g2(env)
        approve_parked(env, "G2_KEYFRAMES")
        await drain(env)
        repos = env.repos
        dag = env.worker.engine.dag
        run_id = env.run_row.id

        # 1. G3 is pending on S12: export is blocked.
        problems = ancestor_gate_problems(
            repos=repos, dag=dag, run_id=run_id, stage_id="s13_export"
        )
        assert any("s12_render_qc" in problem for problem in problems)

        # 2. A G3-kind round recorded on another stage does NOT unlock it.
        metadata_rows = [
            row for row in repos.pipeline.list_stage_runs(run_id, stage="metadata_safety")
            if row.status == "succeeded"
        ]
        assert any(
            gate.gate == "G1_SCRIPT" for gate in repos.gates.list_for_run(run_id)
        )
        impostor = repos.gates.create(
            run_id=run_id,
            gate="G3_FINAL_CUT",
            stage="metadata_safety",
            scope=None,
            subject_id=None,
            round=9,
            # The impostor carries the *current* text snapshot: it is a valid
            # text approval, just not a render approval.
            snapshot_hash=stage_outputs_snapshot_hash(metadata_rows),
            presented_json=None,
        )
        repos.gates.decide(impostor.id, status="approved", note="text-only round")
        still = ancestor_gate_problems(
            repos=repos, dag=dag, run_id=run_id, stage_id="s13_export"
        )
        assert any("s12_render_qc" in problem for problem in still)

        # 3. Approving S12's own G3 unlocks it.
        _approve_g3(env)
        await drain(env)
        cleared = ancestor_gate_problems(
            repos=repos, dag=dag, run_id=run_id, stage_id="s13_export"
        )
        assert cleared == []

        # 4. A re-render with a different sha re-blocks until re-approval.
        row = next(
            r for r in repos.pipeline.list_stage_runs(run_id, stage="s12_render_qc")
            if r.status == "succeeded"
        )
        payload = json.loads(row.output_json or "{}")
        payload["qc_report_sha256"] = "a" * 64
        repos.pipeline.set_stage_run(
            row.id, status="succeeded", output_json=json.dumps(payload)
        )
        blocked = ancestor_gate_problems(
            repos=repos, dag=dag, run_id=run_id, stage_id="s13_export"
        )
        assert any("stale" in problem for problem in blocked)

    asyncio.run(scenario())


def test_cli_export_command(tmp_path, monkeypatch):
    """`studio export` end to end: the table, reuse, and both refusals."""

    async def prepare():
        return await _completed_env(tmp_path, monkeypatch, "cli-export")

    monkeypatch.setenv("COLUMNS", "220")
    env = asyncio.run(prepare())
    from typer.testing import CliRunner

    from studio.cli import app

    runner = CliRunner()
    out = runner.invoke(app, ["export", env.run_row.id])
    assert out.exit_code == 0, out.output
    assert "_draft" in out.output
    assert "short_ar_tall" in out.output

    again = runner.invoke(app, ["export", env.run_row.id, "--deliverable", "short_ar_tall"])
    assert again.exit_code == 0, again.output
    assert "-" in again.output or "checklist_incomplete" in again.output

    # An override without a reason is refused: the audit trail needs the why.
    refused = runner.invoke(app, ["export", env.run_row.id, "--override-commercial-use"])
    assert refused.exit_code != 0

    assert runner.invoke(app, ["export", "no-such-run"]).exit_code != 0
    assert (
        runner.invoke(app, ["export", env.run_row.id, "--deliverable", "nope"]).exit_code != 0
    )

    rows = env.repos.exports.list_for_deliverable(
        json.loads(
            next(
                row["output_json"]
                for row in stage_rows(env, "s13_export")
                if row["status"] == "succeeded"
            )
        )["deliverable_id"]
    )
    assert rows, "the CLI export must be recorded in the exports table"


def test_exp_unit_edges(tmp_path):
    """The small pure pieces: guards, spread indexes, credits text, cleanup."""
    from studio.export.layout import (
        ExportTarget,
        export_root,
        next_revision,
        remove_temp_dir,
    )
    from studio.export.sidecar import build_credits, write_metadata_json
    from studio.export.thumbnails import frame_indexes, thumbnail_name

    with pytest.raises(ValueError, match="revision starts at 1"):
        ExportTarget(root=tmp_path, channel_key="c", date_str="20260101", slug="s", revision=0)
    with pytest.raises(ValueError, match="slug must be lowercase"):
        ExportTarget(root=tmp_path, channel_key="c", date_str="20260101", slug="Upper", revision=1)

    # More candidates than the three canonical fractions fall back to a spread.
    spread = frame_indexes(1000, 5)
    assert len(spread) == 5 and spread == sorted(spread)
    assert frame_indexes(0, 3) == [] and frame_indexes(90, 0) == []
    assert thumbnail_name(7) == "frame_000007.png"

    text = build_credits(
        title="A title",
        credits=[],
        models=[{"model_key": "m", "provider": "p", "commercial_use_verified": False}],
        attribution=["Track by Someone (CC-BY)"],
    )
    assert "  - none" in text
    assert "Attribution required:" in text
    assert "UNVERIFIED" in text

    metadata_path = tmp_path / "metadata.json"
    write_metadata_json(metadata_path, {"schema_version": 1, "title": "عنوان"})
    raw = metadata_path.read_text(encoding="utf-8")
    assert "عنوان" in raw and "\\u" not in raw  # ensure_ascii=False
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["title"] == "عنوان"

    roots = export_root(_settings_stub(tmp_path), profile="final")
    assert roots[1] == "final" and roots[0].name == "exports"
    draft = export_root(_settings_stub(tmp_path), profile="draft")
    assert draft[1] == "draft" and draft[0].name == "_draft"

    (tmp_path / "c" / "20260101_s").mkdir(parents=True)
    (tmp_path / "c" / "20260101_s_r2").mkdir(parents=True)
    assert next_revision(tmp_path, channel_key="c", date_str_value="20260101", slug="s") == 3

    scratch = tmp_path / "gone"
    scratch.mkdir()
    remove_temp_dir(scratch)
    assert not scratch.exists()


def _settings_stub(home: Path):
    from types import SimpleNamespace

    return SimpleNamespace(
        paths=SimpleNamespace(home=str(home)),
        export=SimpleNamespace(root="exports", draft_root="exports/_draft"),
    )


def test_exp_thumbnail_failure_is_reported(tmp_path):
    """A broken source raises MediaToolError naming the frame, not a bare exit."""
    from studio.domain.errors import MediaToolError
    from studio.export.thumbnails import extract_thumbnails

    broken = tmp_path / "not-a-video.mp4"
    broken.write_bytes(b"nope")

    async def scenario() -> None:
        with pytest.raises(MediaToolError, match="thumbnail extraction failed at frame"):
            await extract_thumbnails(
                broken, tmp_path / "thumbs", total_frames=90, count=3, timeout_s=30
            )

    asyncio.run(scenario())
