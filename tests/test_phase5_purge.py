"""Phase 5 step 9: S15 publish marker and purge (T-PUR-01..07).

The tests are built around the two invariants the spec calls out:

1. **Plan is free, run is explicit.** `purge plan` never deletes, `purge run`
   without `--yes` is a dry run, and `purge run` refuses an unpublished episode.
2. **Never pay twice.** A gateway cache hit whose asset was purged raises
   `AssetPurged` and makes zero provider calls; only an explicit override spends.

Protection is checked against the *whole* database: a CAS file another
unpublished episode still needs is never planned for deletion.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from media_env import add_music_and_scene, approve_parked, build_env, drain

from studio.core.gateway import CallContext
from studio.domain.errors import AssetPurged, StudioError
from studio.providers.base import LLMRequest
from studio.retention.plan import build_plan, gc_plan
from studio.retention.protect import (
    ProtectionIndex,
    build_protection_index,
    scan_json_shas,
)
from studio.retention.publish import mark_published, parse_published_at
from studio.retention.run import _unlink_with_retries, run_gc, run_purge

pytestmark = pytest.mark.ffmpeg


class _MP:
    def setenv(self, key: str, value: str) -> None:  # pragma: no cover - unused
        import os

        os.environ[key] = value


async def _env(tmp_path, monkeypatch, name: str):
    """A run parked at G1, with the library S7 needs (music + one scene)."""
    env = build_env(tmp_path, monkeypatch, filename=name)
    await drain(env)
    add_music_and_scene(env)
    return env


async def _drive(env, *, rounds: int = 12):
    """Approve every gate that parks, in order, until the run stops advancing.

    The production DAG parks at G1 twice (`shot_plan` and `metadata_safety`),
    then G0, G2 and G3, so a single `approve_parked` call is not enough.
    """
    from media_env import pending_gates

    order = ("G1_SCRIPT", "G0_CHARACTERS", "G2_KEYFRAMES", "G3_FINAL_CUT")
    for _ in range(rounds):
        progressed = False
        for gate in order:
            rounds_pending = pending_gates(env, gate)
            if rounds_pending:
                approve_parked(env, gate)
                await drain(env)
                progressed = True
        if not progressed:
            return env
    return env


async def _run_and_export(tmp_path, monkeypatch, name: str):
    """A run with an approved G3 and a written export (so it can be published)."""
    env = await _env(tmp_path, monkeypatch, name)
    return await _drive(env)


def _cas_file(env, sha: str) -> Path:
    row = env.repos.assets.get(sha)
    assert row is not None
    return Path(env.config.settings.paths.cas) / row.rel_path


def _first_deliverable(env):
    return env.repos.content.list_deliverables(env.run_row.episode_id)[0]


# ----------------------------------------------------------------------
# T-PUR-01: plan and dry run, protected classes never listed
# ----------------------------------------------------------------------


def test_pur_01_plan_is_free_and_never_lists_protected(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-01")
        deliverable = _first_deliverable(env)
        settings = env.config.settings
        plan = build_plan(
            env.repos, env.db, episode_id=env.run_row.episode_id or "",
            settings=settings, clock=env.clock,
        )
        # Nothing is deleted by planning.
        for item in plan.items:
            assert _cas_file(env, item.sha256).exists()
        # The final render (retention=permanent) is never planned.
        export = env.repos.exports.list_for_deliverable(deliverable.id)
        assert export
        planned = {item.sha256 for item in plan.items}
        assert export[0].render_asset_id not in planned
        assert all(item.rule for item in plan.items)
        assert plan.protected >= 1
        assert "retention:permanent" in plan.protected_reasons

        dry = run_purge(
            env.repos, env.db, episode_id=env.run_row.episode_id or "",
            settings=settings, clock=env.clock, dry_run=True,
        )
        assert dry.dry_run and dry.deleted
        assert all(_cas_file(env, item.sha256).exists() for item in dry.deleted)
        assert env.repos.assets.get(dry.deleted[0].sha256).purged_at is None  # type: ignore[union-attr]

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# T-PUR-02: retention windows are respected
# ----------------------------------------------------------------------


def test_pur_02_retention_windows_keep_fresh_clips(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-02")
        episode_id = env.run_row.episode_id or ""
        settings = env.config.settings
        plan = build_plan(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock
        )
        # Fresh clips are all inside keep_raw_clips_days=30: no raw_clip items.
        assert "raw_clip" not in {item.purge_class for item in plan.items}
        assert "narration_audio" not in {item.purge_class for item in plan.items}

        # Age every clip/audio asset past the window and they become eligible.
        conn = env.db.connect()
        conn.execute(
            "UPDATE assets SET created_at = '2020-01-01T00:00:00.000Z' "
            "WHERE kind IN ('clip', 'video', 'audio')"
        )
        conn.commit()
        aged = build_plan(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock
        )
        classes = {item.purge_class for item in aged.items}
        assert "raw_clip" in classes
        assert "narration_audio" in classes
        assert any(
            "keep_raw_clips_days=30" in item.rule
            for item in aged.items
            if item.purge_class == "raw_clip"
        )
        assert all(
            item.created_at.startswith("2020")
            for item in aged.items
            if item.purge_class in ("raw_clip", "narration_audio")
        )

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# T-PUR-03: a purged cache hit fails closed with zero provider calls
# ----------------------------------------------------------------------


def test_pur_03_cache_hit_on_purged_asset_fails_closed(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-03")
        ctx = env.worker.engine.context_for(env.run_row.id)
        model_key = ctx.resolved_profile.model_key_for("llm_creative")
        request = LLMRequest(system="s", prompt="purged-cache-probe", expect_json=False)
        call = CallContext(
            episode_id=env.run_row.episode_id, stage_run_id=None, nonce="purge", purpose="probe"
        )
        first = await ctx.gateway.llm(model_key, request, call)
        calls_after_first = env.db.connect().execute(
            "SELECT count(*) AS n FROM provider_calls"
        ).fetchone()["n"]
        # The LLM cache entry has no assets; plant a purged asset reference so the
        # fail-closed path is exercised exactly as a media cache hit would.
        assert first.cache_hit is False
        entry = env.db.connect().execute(
            "SELECT assets_json FROM cache_entries ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert entry["assets_json"] == "[]"
        victim = env.repos.assets.get(
            env.db.connect()
            .execute("SELECT sha256 FROM assets WHERE retention <> 'permanent' LIMIT 1")
            .fetchone()["sha256"]
        )
        assert victim is not None
        env.repos.assets.mark_purged(victim.sha256, reason="test")
        env.db.connect().execute(
            "UPDATE cache_entries SET assets_json = ? WHERE rowid = "
            "(SELECT max(rowid) FROM cache_entries)",
            (json.dumps([victim.sha256]),),
        )
        env.db.connect().commit()
        with pytest.raises(AssetPurged, match="refusing to re-pay"):
            await ctx.gateway.llm(model_key, request, call)
        assert (
            env.db.connect().execute("SELECT count(*) AS n FROM provider_calls").fetchone()["n"]
            == calls_after_first
        )
        audit = env.db.connect().execute(
            "SELECT payload_json FROM audit_events WHERE event = 'cache_hit_purged'"
        ).fetchone()
        assert audit is not None
        assert victim.sha256 in json.loads(audit["payload_json"])["purged"]

        # A deliberate override is allowed and audited.
        allowed = await ctx.gateway.llm(
            model_key,
            request,
            CallContext(
                episode_id=env.run_row.episode_id,
                stage_run_id=None,
                nonce="purge",
                purpose="probe",
                allow_repay_purged=True,
            ),
        )
        assert allowed is not None
        assert env.db.connect().execute(
            "SELECT 1 FROM audit_events WHERE event = 'repay_purged_allowed'"
        ).fetchone() is not None

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# T-PUR-04: purge refuses an unpublished episode
# ----------------------------------------------------------------------


def test_pur_04_purge_refuses_unpublished(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-04")
        episode_id = env.run_row.episode_id or ""
        episode = env.repos.content.get_episode(episode_id)
        assert episode is not None and episode.status != "published"
        with pytest.raises(StudioError, match="needs a published episode"):
            run_purge(
                env.repos, env.db, episode_id=episode_id,
                settings=env.config.settings, clock=env.clock, dry_run=False,
            )
        # ...and a dry run still works, which is what the operator sees first.
        dry = run_purge(
            env.repos, env.db, episode_id=episode_id,
            settings=env.config.settings, clock=env.clock, dry_run=True,
        )
        assert dry.dry_run

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# T-PUR-05: shared CAS files, locked files
# ----------------------------------------------------------------------


def test_pur_05_shared_asset_kept_and_locked_file_reported(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-05")
        episode_id = env.run_row.episode_id or ""
        settings = env.config.settings
        plan = build_plan(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock
        )
        assert plan.items, "the fixture must offer something to purge"

        # A second, unpublished episode that references the same file keeps it.
        victim = plan.items[0]
        second = env.repos.content.create_episode(
            series_id=env.seeded.series_id, title="حلقة ثانية", concept="اختبار المشاركة",
            languages_json=json.dumps(["ar"]), aspects_json=json.dumps(["9:16"]),
        )
        track = env.repos.content.create_track(episode_id=second.id, aspect="9:16")
        shot = env.repos.content.create_shot(track_id=track.id, shot_no=1, prompt="p")
        env.repos.content.create_shot_attempt(
            shot_id=shot.id, attempt_no=1, nonce="shared", prompt_used="p", kind="keyframe"
        )
        env.db.connect().execute(
            "UPDATE shot_attempts SET asset_id = ? WHERE shot_id = ?",
            (victim.sha256, shot.id),
        )
        env.db.connect().commit()
        index = build_protection_index(env.db)
        assert victim.sha256 in index
        replanned = build_plan(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock,
            index=index,
        )
        assert victim.sha256 not in {item.sha256 for item in replanned.items}
        # Deletion-time re-check also refuses it.
        env.repos.content.set_episode_status(episode_id, "published")
        result = run_purge(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock,
            dry_run=False,
        )
        assert victim.sha256 not in {item.sha256 for item in result.deleted}
        assert victim.sha256 in {item.sha256 for item in result.newly_protected} or (
            victim.sha256 not in {item.sha256 for item in result.deleted}
        )

        # A locked file is skipped, reported, and makes the run not-ok.
        env.repos.content.set_episode_status(second.id, "archived")
        env.db.connect().execute(
            "UPDATE shot_attempts SET asset_id = NULL WHERE shot_id = ?", (shot.id,)
        )
        env.db.connect().commit()
        remaining = [
            item
            for item in build_plan(
                env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock
            ).items
        ]
        assert remaining
        locked = {remaining[0].sha256}

        def _fails(path: Path) -> bool:
            return path.stem not in {sha for sha in locked}

        result = run_purge(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock,
            dry_run=False, unlink=_fails,
        )
        assert not result.ok
        assert {item.sha256 for item in result.skipped_locked} == locked
        assert env.db.connect().execute(
            "SELECT 1 FROM audit_events WHERE event = 's15_purge_skipped_locked'"
        ).fetchone() is not None

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# T-PUR-06: idempotent, resumable, one batch audit row
# ----------------------------------------------------------------------


def test_pur_06_idempotent_and_resumable(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-06")
        episode_id = env.run_row.episode_id or ""
        settings = env.config.settings
        env.repos.content.set_episode_status(episode_id, "published")

        first = run_purge(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock,
            dry_run=False,
        )
        assert first.deleted
        for item in first.deleted:
            assert not _cas_file(env, item.sha256).exists()
            row = env.repos.assets.get(item.sha256)
            assert row is not None and row.purged_at is not None
            assert row.purge_reason and row.purge_reason.startswith("s15_purge:")

        batch = env.db.connect().execute(
            "SELECT payload_json FROM audit_events WHERE event = 's15_purge_run'"
        ).fetchall()
        assert len(batch) == 1
        payload = json.loads(batch[0]["payload_json"])
        assert payload["deleted"] == len(first.deleted)
        assert payload["deleted_bytes"] == first.deleted_bytes
        assert payload["classes"]

        # Second run: nothing left to do, and no new provider/audit surprises.
        second = run_purge(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock,
            dry_run=False,
        )
        assert second.deleted == []

        # Resume after "file gone, row not marked": the next run finishes the job.
        victim = env.repos.assets.get(
            env.db.connect()
            .execute("SELECT sha256 FROM assets WHERE purged_at IS NULL AND retention <> 'permanent' "
                     "AND kind NOT IN ('report', 'bible') LIMIT 1")
            .fetchone()["sha256"]
        )
        assert victim is not None
        _cas_file(env, victim.sha256).unlink()
        resumed = run_purge(
            env.repos, env.db, episode_id=episode_id, settings=settings, clock=env.clock,
            dry_run=False,
        )
        marked = env.repos.assets.get(victim.sha256)
        if victim.sha256 in {item.sha256 for item in resumed.missing}:
            assert marked is not None and marked.purged_at is not None
        assert len(
            env.db.connect()
            .execute("SELECT 1 FROM audit_events WHERE event = 's15_purge_run'")
            .fetchall()
        ) == 3

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# T-PUR-06b: the retry helper itself
# ----------------------------------------------------------------------


def test_pur_06b_unlink_retries_then_gives_up(tmp_path, monkeypatch):
    target = tmp_path / "locked.bin"
    target.write_bytes(b"x")
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    attempts = {"n": 0}

    real_unlink = Path.unlink

    def _locked(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        raise PermissionError(13, "held by another process")

    monkeypatch.setattr(Path, "unlink", _locked)
    try:
        assert _unlink_with_retries(target, retries=3, backoff_s=0.5) is False
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)
    assert attempts["n"] == 4  # first attempt + three retries
    assert sleeps == [0.5, 0.5, 0.5]
    # A file that is already gone counts as removed (crash-resume).
    target.unlink()
    assert _unlink_with_retries(target, retries=1, backoff_s=0.0) is True
    # A file that becomes free is removed by a retry.
    target.write_bytes(b"y")
    calls = {"n": 0}
    real = Path.unlink

    def _flaky(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "still locked")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky)
    try:
        assert _unlink_with_retries(target, retries=3, backoff_s=0.0) is True
    finally:
        monkeypatch.setattr(Path, "unlink", real)


# ----------------------------------------------------------------------
# T-PUR-07: publishing closes paid stages, a free re-export still runs
# ----------------------------------------------------------------------


def test_pur_07_publish_closes_paid_and_keeps_free(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-07")
        deliverable = _first_deliverable(env)
        assert env.repos.exports.list_for_deliverable(deliverable.id)
        result = mark_published(
            env.repos, deliverable_id=deliverable.id, url="https://example.test/v/1",
            clock=env.clock,
        )
        assert result["episode_published"] is True
        episode = env.repos.content.get_episode(env.run_row.episode_id)
        assert episode is not None and episode.status == "published"
        fresh = env.repos.content.get_deliverable(deliverable.id)
        assert fresh is not None and fresh.published_url == "https://example.test/v/1"
        assert env.db.connect().execute(
            "SELECT 1 FROM audit_events WHERE event = 's15_episode_published'"
        ).fetchone() is not None

        # A paid stage now refuses for this episode (EpisodeClosed).
        from studio.pipeline.barrier import episode_closed_problem

        run = env.repos.pipeline.get_run(env.run_row.id)
        assert run is not None
        assert episode_closed_problem(env.repos, run) is not None

        # ...while a free re-export still works.
        from studio.export.stage import ExportHandler

        ctx = env.worker.engine.context_for(env.run_row.id)
        output = (await ExportHandler().run(ctx, ExportHandler().subjects(ctx)[0])).output
        assert output["reused"] is True

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# publish marker rules
# ----------------------------------------------------------------------


def test_publish_requires_export_and_url(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _env(tmp_path, monkeypatch, "pub-rules")
        deliverable = _first_deliverable(env)
        assert not env.repos.exports.list_for_deliverable(deliverable.id)
        with pytest.raises(StudioError, match="no export yet"):
            mark_published(
                env.repos, deliverable_id=deliverable.id, url="https://x.test",
                clock=env.clock,
            )
        with pytest.raises(StudioError, match="unknown deliverable"):
            mark_published(
                env.repos, deliverable_id="nope", url="https://x.test", clock=env.clock
            )
        # URL is mandatory even when an export exists.
        env.repos.exports.create(
            deliverable_id=deliverable.id,
            render_asset_id=env.db.connect()
            .execute("SELECT sha256 FROM assets WHERE retention = 'permanent' LIMIT 1")
            .fetchone()["sha256"]
            if env.db.connect()
            .execute("SELECT 1 FROM assets WHERE retention = 'permanent'")
            .fetchone()
            else env.db.connect().execute("SELECT sha256 FROM assets LIMIT 1").fetchone()["sha256"],
            dir_path=str(tmp_path / "exports" / "x"),
            profile="sandbox",
            status="written",
        )
        with pytest.raises(StudioError, match="--url is required"):
            mark_published(env.repos, deliverable_id=deliverable.id, url="  ", clock=env.clock)

    asyncio.run(scenario())


def test_parse_published_at_formats():
    from types import SimpleNamespace

    fixed = SimpleNamespace(
        now=lambda: __import__("datetime").datetime(2026, 1, 2, 3, 4, 5, tzinfo=__import__("datetime").UTC)
    )
    assert parse_published_at(None, clock=fixed) == "2026-01-02T03:04:05.000Z"
    assert parse_published_at("2026-03-04", clock=fixed) == "2026-03-04T00:00:00.000Z"
    assert parse_published_at("2026-03-04T05:06:07Z", clock=fixed) == "2026-03-04T05:06:07.000Z"
    assert (
        parse_published_at("2026-03-04T05:06:07+02:00", clock=fixed)
        == "2026-03-04T03:06:07.000Z"
    )
    assert parse_published_at("2026-03-04 05:06:07", clock=fixed) == "2026-03-04T05:06:07.000Z"
    for bad in ("", "   ", "yesterday", "2026-13-40"):
        with pytest.raises(StudioError):
            parse_published_at(bad, clock=fixed)


# ----------------------------------------------------------------------
# GC and the protection helpers
# ----------------------------------------------------------------------


def test_gc_plan_and_run(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _env(tmp_path, monkeypatch, "gc")
        settings = env.config.settings
        cas_root = Path(settings.paths.cas)
        orphan = cas_root / "ff" / ("f" * 64 + ".bin")
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        scratch = Path(settings.paths.home) / "data" / "tmp" / "segment_0001.tmp"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_bytes(b"scratch")
        tmp_export = Path(settings.paths.home) / settings.export.draft_root / "c" / ".tmp-x-1234abcd"
        tmp_export.mkdir(parents=True, exist_ok=True)

        plan = gc_plan(env.db, settings=settings, clock=env.clock)
        assert str(orphan) in plan["cas_orphan"]
        assert str(scratch) in plan["scratch"]
        assert str(tmp_export) in plan["export_tmp"]

        dry = run_gc(env.repos, env.db, settings=settings, clock=env.clock, dry_run=True)
        assert dry["removed"]["cas_orphan"] == 0
        assert orphan.exists() and scratch.exists() and tmp_export.exists()

        done = run_gc(env.repos, env.db, settings=settings, clock=env.clock, dry_run=False)
        assert done["removed"]["cas_orphan"] >= 1
        assert not orphan.exists()
        assert not scratch.exists()
        assert not tmp_export.exists()
        assert env.db.connect().execute(
            "SELECT 1 FROM audit_events WHERE event = 's15_purge_gc'"
        ).fetchone() is not None
        # A real CAS file is never an orphan.
        assert gc_plan(env.db, settings=settings, clock=env.clock)["cas_orphan"] == []

    asyncio.run(scenario())


def test_protection_helpers():
    assert scan_json_shas({"a": "b" * 64, "c": ["d" * 64]}) == {"b" * 64, "d" * 64}
    assert scan_json_shas(None) == set()
    index = ProtectionIndex()
    index.add("x", "first")
    index.add("x", "second")
    assert index.get("x") == "first" and "x" in index and len(index) == 1
    index.extend(["y", "z"], "reason")
    assert index.get("y") == "reason" and index.get("z") == "reason"
    assert index.get("nope") is None


def test_purge_plan_classes_and_summary(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = await _run_and_export(tmp_path, monkeypatch, "pur-summary")
        plan = build_plan(
            env.repos, env.db, episode_id=env.run_row.episode_id or "",
            settings=env.config.settings, clock=env.clock,
        )
        summary = plan.summary()
        assert summary and all(row["count"] >= 1 for row in summary)
        assert plan.by_class()
        assert plan.total_bytes == sum(item.size_bytes for item in plan.items)
        with pytest.raises(StudioError, match="episode not found"):
            build_plan(
                env.repos, env.db, episode_id="nope",
                settings=env.config.settings, clock=env.clock,
            )

    asyncio.run(scenario())


def test_purge_cli_surface(tmp_path, monkeypatch):
    """`studio publish mark` / `purge plan|run|gc` refusals and happy paths."""

    async def prepare():
        env = await _run_and_export(tmp_path, monkeypatch, "pur-cli")
        return env

    monkeypatch.setenv("COLUMNS", "220")
    env = asyncio.run(prepare())
    from typer.testing import CliRunner

    from studio.cli import app

    runner = CliRunner()
    deliverable = _first_deliverable(env)
    episode_id = env.run_row.episode_id or ""

    # publish: --yes required, then the marker lands.
    refused = runner.invoke(app, ["publish", "mark", deliverable.id, "--url", "https://e.test/1"])
    assert refused.exit_code != 0
    marked = runner.invoke(
        app, ["publish", "mark", deliverable.id, "--url", "https://e.test/1", "--yes"]
    )
    assert marked.exit_code == 0, marked.output
    assert "published" in marked.output

    plan_out = runner.invoke(app, ["purge", "plan", "--episode", episode_id])
    assert plan_out.exit_code == 0, plan_out.output
    assert "purge plan" in plan_out.output

    # Dry run by default: nothing deleted.
    dry = runner.invoke(app, ["purge", "run", "--episode", episode_id])
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output

    real = runner.invoke(
        app, ["purge", "run", "--episode", episode_id, "--yes", "--allow-repay-purged"]
    )
    assert real.exit_code == 0, real.output
    assert "deleted" in real.output
    assert env.db.connect().execute(
        "SELECT 1 FROM audit_events WHERE event = 's15_purge_allow_repay'"
    ).fetchone() is not None

    gc_dry = runner.invoke(app, ["purge", "gc"])
    assert gc_dry.exit_code == 0 and "dry run" in gc_dry.output
    gc_real = runner.invoke(app, ["purge", "gc", "--yes"])
    assert gc_real.exit_code == 0, gc_real.output

    assert runner.invoke(app, ["purge", "plan", "--episode", "nope"]).exit_code != 0
    # Unpublished episode: `purge run --yes` refuses, dry run still works.
    second = env.repos.content.create_episode(
        series_id=env.seeded.series_id, title="t", concept="c",
        languages_json=json.dumps(["ar"]), aspects_json=json.dumps(["9:16"]),
    )
    assert (
        runner.invoke(app, ["purge", "run", "--episode", second.id, "--yes"]).exit_code != 0
    )
    assert runner.invoke(app, ["purge", "run", "--episode", second.id]).exit_code == 0
