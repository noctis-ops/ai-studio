"""Phase 5 coverage lift (3): the clip stage's fail-closed preflight paths.

`media/clips.py` was the only module under its per-module floor (84 % vs 85 %).
Every uncovered statement here is an operator mistake the stage must refuse
*before* spending money, so these are cheap unit-level calls on a real
`RunContext` (no provider call is ever reached).
"""

from __future__ import annotations

import asyncio

import pytest
from media_env import build_env, pending_gates, run_to_g2

from studio.domain.enums import Scope
from studio.domain.errors import PermanentProviderError, PreflightError
from studio.domain.refs import SubjectRef
from studio.media.clips import ClipGenHandler


def test_clips_unknown_shot_is_permanent(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="clips-pre1")
        ctx = env.worker.engine.context_for(env.run_row.id)
        handler = ClipGenHandler()
        with pytest.raises(PermanentProviderError, match="shot not found"):
            await handler.run(ctx, SubjectRef(scope=Scope.SHOT, id="no-such-shot"))

    asyncio.run(scenario())


def test_clips_refuses_without_locked_duration_or_keyframe(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="clips-pre2")
        await run_to_g2(env)
        assert pending_gates(env, "G2_KEYFRAMES")
        ctx = env.worker.engine.context_for(env.run_row.id)
        handler = ClipGenHandler()
        shot = env.repos.content.shots_for_track(env.seeded.track_ids[0])[0]
        subject = SubjectRef(scope=Scope.SHOT, id=shot.id)

        # 1. No locked duration → PreflightError, nothing paid.
        env.db.connect().execute(
            "UPDATE shots SET clip_duration_ms = NULL WHERE id = ?", (shot.id,)
        )
        with pytest.raises(PreflightError, match="no locked duration"):
            await handler.run(ctx, subject)

        # 2. Restore the duration but drop the accepted keyframe attempt.
        env.db.connect().execute(
            "UPDATE shots SET clip_duration_ms = 5000 WHERE id = ?", (shot.id,)
        )
        env.db.connect().execute(
            "DELETE FROM shot_attempts WHERE shot_id = ? AND kind = 'keyframe'", (shot.id,)
        )
        with pytest.raises(PreflightError, match="no keyframe attempt"):
            await handler.run(ctx, subject)

    asyncio.run(scenario())


def test_clips_missing_keyframe_asset_is_permanent(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="clips-pre3")
        await run_to_g2(env)
        ctx = env.worker.engine.context_for(env.run_row.id)
        handler = ClipGenHandler()
        shot = env.repos.content.shots_for_track(env.seeded.track_ids[0])[0]
        attempt = env.repos.content.current_shot_attempt(shot.id, kind="keyframe")
        assert attempt is not None
        # A keyframe attempt that points at an asset the CAS index never saw.
        # Foreign keys are disabled for the write: the row is deliberately
        # inconsistent, exactly like a hand-edited or partially restored DB.
        conn = env.db.connect()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE shot_attempts SET asset_id = 'deadbeef' WHERE id = ?", (attempt.id,)
        )
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(PermanentProviderError, match="missing from the CAS index"):
            await handler.run(ctx, SubjectRef(scope=Scope.SHOT, id=shot.id))

    asyncio.run(scenario())


def test_clips_unknown_track_and_episode_are_permanent(tmp_path, monkeypatch):
    async def scenario() -> None:
        import dataclasses

        from media_env import drain

        env = build_env(tmp_path, monkeypatch, filename="clips-pre4")
        await drain(env)  # parks at G1; shot_plan has created the shots
        ctx = env.worker.engine.context_for(env.run_row.id)
        handler = ClipGenHandler()
        shot = env.repos.content.shots_for_track(env.seeded.track_ids[0])[0]
        subject = SubjectRef(scope=Scope.SHOT, id=shot.id)

        # A shot whose track row is gone (hand-edited / partial restore).
        conn = env.db.connect()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE shots SET track_id = 'no-track' WHERE id = ?", (shot.id,))
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(PermanentProviderError, match="track not found"):
            await handler.run(ctx, subject)
        conn.execute("UPDATE shots SET track_id = ? WHERE id = ?", (shot.track_id, shot.id))

        # A context with no episode cannot resolve the run's series.
        orphan = dataclasses.replace(ctx, episode_id=None)
        with pytest.raises(PermanentProviderError, match="episode not found"):
            await handler.run(orphan, subject)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# §Q floor: media/preflight.py's "no row, no configured default" branch.
# ----------------------------------------------------------------------


def test_preflight_budget_row_cap_uses_the_configured_default():
    from types import SimpleNamespace

    from studio.domain.enums import BudgetScope
    from studio.media.preflight import _budget_row_cap

    ctx = SimpleNamespace(repos=SimpleNamespace(budgets=SimpleNamespace(get=lambda *a: None)))
    # explicit row wins...
    row = SimpleNamespace(cap_musd=12_345)
    ctx_row = SimpleNamespace(repos=SimpleNamespace(budgets=SimpleNamespace(get=lambda *a: row)))
    assert _budget_row_cap(ctx_row, BudgetScope.EPISODE, "e1", None) == 12_345
    # ...an absent row with no default is "no cap"...
    assert _budget_row_cap(ctx, BudgetScope.EPISODE, "e1", None) is None
    # ...and an absent row with a USD default is converted to micro-USD.
    assert _budget_row_cap(ctx, BudgetScope.EPISODE, "e1", 2.5) == 2_500_000

    # `_remaining_musd` is "unknown" without a cap and otherwise cap - held.
    from studio.media.preflight import _remaining_musd

    assert _remaining_musd(ctx, BudgetScope.EPISODE, "e1", None) is None
    held_ctx = SimpleNamespace(
        repos=SimpleNamespace(budgets=SimpleNamespace(held_musd=lambda *a: 400))
    )
    assert _remaining_musd(held_ctx, BudgetScope.EPISODE, "e1", 1_000) == 600
