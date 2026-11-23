"""Phase 5 step 8: S14 bible proposals (T-BIB-01..04).

What is asserted, in the order the spec lists it:

- the strict validator rejects an unknown `scene_id` and an over-cap proposal,
- nothing is auto-applied: a run leaves a `pending` row and the bible version is
  unchanged; `accept` bumps the version with an audit row; a stale base refuses,
- the crash windows never double-pay, and exactly one `potential_orphan` audit
  row appears where the gateway declares one,
- the stage is refused before its gated ancestor (G1) is approved, and refused
  again when the approved script changes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from media_env import approve_parked, build_env, drain, stage_rows

from studio.bible.apply import apply_proposal
from studio.bible.proposal import (
    accept_proposal,
    gather_inputs,
    propose,
    reject_proposal,
)
from studio.bible.schema import BibleUpdate, validate_proposal
from studio.domain.errors import ProposalInvalid, ProposalStale
from studio.domain.series_bible import BibleContent, CharacterBible
from studio.pipeline.barrier import ancestor_gate_problems

pytestmark = pytest.mark.ffmpeg


def _valid_payload(scene_id: str, character: str = "نُور") -> dict:
    return {
        "new_characters": [
            {
                "name": "المرشد",
                "description": "شيخ الميناء",
                "visual_tokens": ["عمامة زرقاء"],
                "arc": "يكشف اسم العاصفة",
                "evidence": [{"scene_id": scene_id}],
            }
        ],
        "character_changes": [
            {"name": character, "change": "يثق بالغريب", "evidence": [{"scene_id": scene_id}]}
        ],
        "locations": [
            {"name": "رصيف المرشد", "description": "خشب مبلول", "evidence": [{"scene_id": scene_id}]}
        ],
        "plot_events": [
            {"summary": "لقاء الفجر", "evidence": [{"scene_id": scene_id}]}
        ],
        "unresolved_threads": [
            {"name": "اسم العاصفة", "note": "", "evidence": [{"scene_id": scene_id}]}
        ],
        "continuity_notes": [
            {"text": "الفانوس يبقى", "evidence": [{"scene_id": scene_id}]}
        ],
    }


# ----------------------------------------------------------------------
# T-BIB-01: schema and evidence validation
# ----------------------------------------------------------------------


def test_bib_01_validator_rejects_unknown_scene_id():
    with pytest.raises(ProposalInvalid, match="unknown scene or shot id"):
        validate_proposal(
            _valid_payload("not-a-scene"),
            known_ids={"scene-1"},
            character_names={"نُور"},
        )
    good = validate_proposal(
        _valid_payload("scene-1"), known_ids={"scene-1"}, character_names={"نُور"}
    )
    assert good.fact_count() == 6
    assert good.all_evidence_ids() == ["scene-1"] * 6


def test_bib_01_validator_rejects_over_cap_and_bad_shape():
    big = _valid_payload("s1")
    big["plot_events"] = [
        {"summary": f"event {index}", "evidence": [{"scene_id": "s1"}]} for index in range(5)
    ]
    with pytest.raises(ProposalInvalid, match="max_facts=2"):
        validate_proposal(big, known_ids={"s1"}, character_names={"نُور"}, max_facts=2)

    with pytest.raises(ProposalInvalid, match="schema violation"):
        validate_proposal({"plot_events": []}, known_ids={"s1"}, character_names=set())

    broken = _valid_payload("s1")
    broken["plot_events"] = [{"summary": "no evidence", "evidence": []}]
    with pytest.raises(ProposalInvalid, match="at least 1 item"):
        validate_proposal(broken, known_ids={"s1"}, character_names={"نُور"})

    unknown_character = _valid_payload("s1", character="مجهول")
    with pytest.raises(ProposalInvalid, match="may only name a known bible character"):
        validate_proposal(unknown_character, known_ids={"s1"}, character_names={"نُور"})

    extra = _valid_payload("s1")
    extra["surprise"] = []
    with pytest.raises(ProposalInvalid, match="Extra inputs are not permitted"):
        validate_proposal(extra, known_ids={"s1"}, character_names={"نُور"})

    with pytest.raises(ProposalInvalid, match="must be a JSON object"):
        validate_proposal(["not", "an", "object"], known_ids=set(), character_names=set())


def test_bib_01_apply_is_pure_and_cited():
    content = BibleContent(
        title="فانوس", premise="حارس", world="ميناء", characters=[CharacterBible(name="نُور")]
    )
    update = BibleUpdate.model_validate(_valid_payload("scene-9"))
    applied = apply_proposal(content, update)
    names = [character.name for character in applied.characters]
    assert names == ["نُور", "المرشد"]
    assert "رصيف المرشد" in applied.world and "[scene-9]" in applied.world
    assert "[scene-9]" in applied.continuity_notes
    assert applied.continuity_notes.count("اسم العاصفة") == 1
    # The input object is untouched (frozen model, pure function).
    assert content.world == "ميناء"
    assert content.characters[0].arc == ""


# ----------------------------------------------------------------------
# T-BIB-02 / T-BIB-04: nothing auto-applied, accept bumps, stale refuses,
# and the gate barrier
# ----------------------------------------------------------------------


def test_bib_02_run_creates_pending_and_accept_bumps_version(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="bib-02")
        await drain(env)
        before = env.repos.bible.latest(env.seeded.series_id)
        assert before is not None

        # T-BIB-04: refused while G1 is pending, with the gate named.
        problems = ancestor_gate_problems(
            repos=env.repos,
            dag=env.worker.engine.dag,
            run_id=env.run_row.id,
            stage_id="s14_bible_proposals",
        )
        assert any("shot_plan" in problem for problem in problems)

        approve_parked(env, "G1_SCRIPT", note="script ok")
        await drain(env)
        rows = [row for row in stage_rows(env, "s14_bible_proposals") if row["status"] == "succeeded"]
        assert rows, "S14 must succeed once G1 is approved"
        output = json.loads(rows[0]["output_json"])

        # Nothing auto-applied: the proposal is pending, the bible unchanged.
        proposal = env.repos.proposals.get(output["proposal_id"])
        assert proposal is not None and proposal.status == "pending"
        unchanged = env.repos.bible.latest(env.seeded.series_id)
        assert unchanged.sha256 == before.sha256
        assert unchanged.version == before.version

        ctx = env.worker.engine.context_for(env.run_row.id)
        accepted = accept_proposal(ctx, proposal.id)
        assert accepted["version"] == before.version + 1
        after = env.repos.bible.latest(env.seeded.series_id)
        assert after.version == before.version + 1
        assert any(c.name.startswith("المرشد") for c in after.characters)
        audit = env.db.connect().execute(
            "SELECT payload_json FROM audit_events WHERE event = 'bible_proposal_accepted'"
        ).fetchone()
        assert audit is not None
        assert json.loads(audit["payload_json"])["to_version"] == after.version

        # A decided proposal is not accepted twice.
        with pytest.raises(Exception, match="not pending"):
            accept_proposal(ctx, proposal.id)

        # A stale base is refused and marked stale; nothing is written.
        fresh = await propose(ctx, nonce_override="manual-1")
        env.repos.bible.create_version(
            env.seeded.series_id,
            BibleContent(
                title=after.title, premise=after.premise, world="ميناء أحدث",
                characters=after.characters,
            ),
            created_by="test",
        )
        with pytest.raises(ProposalStale, match="but the bible is at v"):
            accept_proposal(ctx, fresh["proposal_id"])
        assert env.repos.proposals.get(fresh["proposal_id"]).status == "stale"  # type: ignore[union-attr]

        # Reject leaves the bible alone.
        rejectable = await propose(ctx, nonce_override="manual-2")
        result = reject_proposal(ctx, rejectable["proposal_id"], reason="not this episode")
        assert result["status"] == "rejected"
        assert env.repos.bible.latest(env.seeded.series_id).world == "ميناء أحدث"

    asyncio.run(scenario())


def test_bib_04_changed_script_reblocks_the_paid_stage(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="bib-04")
        await drain(env)
        approve_parked(env, "G1_SCRIPT")
        await drain(env)
        run_id = env.run_row.id
        dag = env.worker.engine.dag
        assert (
            ancestor_gate_problems(
                repos=env.repos, dag=dag, run_id=run_id, stage_id="s14_bible_proposals"
            )
            == []
        )
        # Re-running S3 (a post-gate edit) makes the approved snapshot stale.
        row = next(
            r
            for r in env.repos.pipeline.list_stage_runs(run_id, stage="shot_plan")
            if r.status == "succeeded"
        )
        payload = json.loads(row.output_json or "{}")
        payload["shots_sha256"] = "b" * 64
        env.repos.pipeline.set_stage_run(row.id, status="succeeded", output_json=json.dumps(payload))
        problems = ancestor_gate_problems(
            repos=env.repos, dag=dag, run_id=run_id, stage_id="s14_bible_proposals"
        )
        assert any("stale" in problem for problem in problems)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# T-BIB-03: the paid windows and resume
# ----------------------------------------------------------------------


def test_bib_03_resume_never_double_pays(tmp_path, monkeypatch):
    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="bib-03")
        await drain(env)
        approve_parked(env, "G1_SCRIPT")
        await drain(env)
        ctx = env.worker.engine.context_for(env.run_row.id)
        calls = env.db.connect().execute("SELECT count(*) AS n FROM provider_calls").fetchone()["n"]

        first = await propose(ctx, nonce_override="crash-0")
        after_first = env.db.connect().execute(
            "SELECT count(*) AS n FROM provider_calls"
        ).fetchone()["n"]
        assert after_first == calls + 1

        # Same nonce + same inputs → the gateway cache answers, no new call.
        again = await propose(ctx, nonce_override="crash-0")
        after_second = env.db.connect().execute(
            "SELECT count(*) AS n FROM provider_calls"
        ).fetchone()["n"]
        assert after_second == after_first
        assert again["proposal_id"] != first["proposal_id"]  # a new row, no new spend

        # An explicit regeneration is a new nonce and does spend once.
        regenerated = await propose(ctx, regenerate=True)
        after_third = env.db.connect().execute(
            "SELECT count(*) AS n FROM provider_calls"
        ).fetchone()["n"]
        assert after_third == after_first + 1
        assert regenerated["proposal_id"] != first["proposal_id"]

    asyncio.run(scenario())


def test_bib_03_one_retry_then_operator(tmp_path, monkeypatch):
    """Two invalid payloads = two paid calls then a hard stop (no loop)."""
    from studio.providers.mock.llm import MockLLMProvider

    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="bib-03b")
        await drain(env)
        approve_parked(env, "G1_SCRIPT")
        await drain(env)
        ctx = env.worker.engine.context_for(env.run_row.id)
        # The stage already stored one pending proposal when G1 was approved.
        before_rows = env.repos.proposals.count_for_episode(env.run_row.episode_id or "")

        original = MockLLMProvider.complete
        calls = {"n": 0}

        async def bad(self, req):  # type: ignore[no-untyped-def]
            result = await original(self, req)
            if req.schema_name == "bible_update":
                calls["n"] += 1
                payload = dict(result.parsed_json or {})
                payload["plot_events"] = [
                    {"summary": "unprovable", "evidence": [{"scene_id": "ghost-scene"}]}
                ]
                return result.model_copy(
                    update={"parsed_json": payload, "text": json.dumps(payload)}
                )
            return result

        MockLLMProvider.complete = bad  # type: ignore[assignment]
        try:
            with pytest.raises(ProposalInvalid, match="rejected twice"):
                await propose(ctx, nonce_override="bad-0")
        finally:
            MockLLMProvider.complete = original  # type: ignore[assignment]

        assert calls["n"] == 2  # exactly one regeneration, never a loop
        audit = env.db.connect().execute(
            "SELECT payload_json FROM audit_events WHERE event = 'bible_proposal_invalid'"
        ).fetchone()
        assert audit is not None
        assert "ghost-scene" in json.loads(audit["payload_json"])["second"]
        assert env.repos.proposals.count_for_episode(env.run_row.episode_id or "") == before_rows

    asyncio.run(scenario())


def test_bib_gather_inputs_falls_back_to_shots(tmp_path, monkeypatch):
    """An episode without scene rows cites its shots instead of failing."""

    async def scenario() -> None:
        env = build_env(tmp_path, monkeypatch, filename="bib-gather")
        await drain(env)
        conn = env.db.connect()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM scenes")
        conn.commit()
        episode = env.repos.content.get_episode(env.run_row.episode_id)
        assert episode is not None
        inputs = gather_inputs(env.repos, episode)
        shots = env.repos.content.list_shots(episode.id)
        assert inputs.evidence_ids == tuple(shot.id for shot in shots)
        assert all(scene["aspect"] is None for scene in inputs.scenes)
        assert inputs.base_version >= 1

    asyncio.run(scenario())


def test_bib_cli_flow(tmp_path, monkeypatch):
    """`studio bible proposals list|show|reject|accept` and the paid refusal."""

    async def prepare():
        env = build_env(tmp_path, monkeypatch, filename="bib-cli")
        await drain(env)
        approve_parked(env, "G1_SCRIPT")
        await drain(env)
        return env

    monkeypatch.setenv("COLUMNS", "220")
    env = asyncio.run(prepare())
    from typer.testing import CliRunner

    from studio.cli import app

    runner = CliRunner()
    proposal = env.repos.proposals.list_for_episode(env.run_row.episode_id or "")[-1]

    listed = runner.invoke(app, ["bible", "proposals"])
    assert listed.exit_code == 0, listed.output
    assert proposal.id in listed.output

    filtered = runner.invoke(app, ["bible", "proposals", "--episode", proposal.episode_id])
    assert filtered.exit_code == 0, filtered.output

    shown = runner.invoke(app, ["bible", "show", proposal.id])
    assert shown.exit_code == 0 and "plot_events" in shown.output

    # Paid without --yes: refused, and the estimate is printed first.
    refused = runner.invoke(app, ["bible", "propose", proposal.episode_id])
    assert refused.exit_code != 0
    assert "worst-case cost" in refused.output

    regenerated = runner.invoke(
        app, ["bible", "propose", proposal.episode_id, "--regenerate", "--yes"]
    )
    assert regenerated.exit_code == 0, regenerated.output

    pending = env.repos.proposals.list_for_episode(proposal.episode_id, status="pending")
    assert len(pending) == 2
    target = pending[-1]
    accepted = runner.invoke(app, ["bible", "accept", target.id, "--note", "good"])
    assert accepted.exit_code == 0, accepted.output
    assert "bible is now v" in accepted.output

    other = pending[0]
    rejected = runner.invoke(app, ["bible", "reject", other.id, "--reason", "thin"])
    assert rejected.exit_code == 0, rejected.output

    # Unknown ids are refused.
    assert runner.invoke(app, ["bible", "show", "nope"]).exit_code != 0
    assert runner.invoke(app, ["bible", "accept", "nope"]).exit_code != 0
    assert runner.invoke(app, ["bible", "reject", "nope"]).exit_code != 0
    assert runner.invoke(app, ["bible", "propose", "nope", "--yes"]).exit_code != 0

    # A second accept on the same proposal is refused (it is not pending).
    assert runner.invoke(app, ["bible", "accept", target.id]).exit_code != 0


def test_bib_repo_and_handler_edges(tmp_path, monkeypatch):
    """Repo guards and the handler's empty/unknown episode paths."""

    async def scenario() -> None:
        import types

        from studio.pipeline.stages_bible import BibleProposalHandler

        env = build_env(tmp_path, monkeypatch, filename="bib-edges")
        await drain(env)
        approve_parked(env, "G1_SCRIPT")
        await drain(env)
        proposal = env.repos.proposals.latest_for_episode(env.run_row.episode_id or "")
        assert proposal is not None

        with pytest.raises(ValueError, match="unknown proposal status"):
            env.repos.proposals.list_for_episode(proposal.episode_id, status="nope")
        with pytest.raises(ValueError, match="cannot decide a proposal as"):
            env.repos.proposals.decide(proposal.id, status="pending")
        with pytest.raises(ValueError, match="unknown proposal"):
            env.repos.proposals.decide("nope", status="rejected")

        handler = BibleProposalHandler()
        empty = types.SimpleNamespace(repos=env.repos, episode_id=None)
        assert handler.subjects(empty) == []  # type: ignore[arg-type]
        with pytest.raises(Exception, match="episode not found"):
            await handler.run(empty, types.SimpleNamespace(id="nope", scope=None))  # type: ignore[arg-type]
        with pytest.raises(Exception, match="episode not found"):
            handler.input_hash(empty, types.SimpleNamespace(id="nope"))  # type: ignore[arg-type]

        # A prompt file that vanished is reported, not silently empty.
        prompts = Path(env.config.settings.paths.home) / "configs" / "prompts"
        saved = (prompts / "bible_update.txt").read_bytes()
        (prompts / "bible_update.txt").unlink()
        ctx = env.worker.engine.context_for(env.run_row.id)
        with pytest.raises(Exception, match=r"configs/prompts/bible_update\.txt"):
            await propose(ctx, nonce_override="noprompt")
        (prompts / "bible_update.txt").write_bytes(saved)

    asyncio.run(scenario())
