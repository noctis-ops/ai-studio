"""T-SHT-01 / T-SHT-02 (PHASE4 §P sheets).

S8a character sheets through the production-shaped fake-LLM chain with mock
image providers. Offline; lavfi-rendered mock output keeps the real QC path.

T-SHT-01: sheets are generated only for missing (character, role). If all are
approved there are zero calls and no gate. Rejecting one character
regenerates only it, with a cost check.
T-SHT-02: ref_face is conditioned on ref_full's sha.
"""

from __future__ import annotations

import asyncio

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
    run_to_g0,
    stage_rows,
)

from studio.pipeline.gates import GateService, ShotRejection

pytestmark = pytest.mark.ffmpeg


def _sheet_call_count(env) -> int:
    return sum(
        1
        for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "character_sheet"
    )


def test_sht_01_all_approved_means_zero_calls_and_no_gate(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch, filename="sht01a")
    # Operator pre-approves one sheet per (character, role) before the run.
    preseed_approved_sheet(env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
                           name="preseeded-primary", variant="testsrc")
    preseed_approved_sheet(env, tmp_path, character_id=CHAR_SECOND, role="ref_full",
                           name="preseeded-second", variant="smptebars")

    async def run_chain() -> None:
        await drain(env)
        # Script park
        approve_parked(env, "G1_SCRIPT", note="script approved")
        await drain(env)
        add_music_and_scene(env)
        approve_parked(env, "G3_FINAL_CUT", note="metadata approved")
        await drain(env)

    asyncio.run(run_chain())

    # S8a found every sheet approved: zero sheet calls, zero G0 gate,
    # and the all-approved audit row was written.
    assert _sheet_call_count(env) == 0
    g0 = env.db.connect().execute(
        "SELECT count(*) AS n FROM approval_gates WHERE gate = 'G0_CHARACTERS'"
    ).fetchone()["n"]
    assert g0 == 0
    approved_audits = audits(env, "character_sheets_all_approved")
    assert approved_audits, "expected the all-approved audit row"
    approved_pairs = {
        (entry.split(":", 1)[0], entry.split(":", 1)[1])
        for entry in approved_audits[0]["approved"]
    }
    assert (CHAR_PRIMARY, "ref_full") in approved_pairs
    assert (CHAR_SECOND, "ref_full") in approved_pairs
    # No sheet stage runs were created; the chain moved on to keyframes.
    assert stage_rows(env, "s8a_character_sheets") == []
    assert stage_rows(env, "s8_keyframes")


@pytest.mark.asyncio
async def test_sht_01_rejecting_one_character_regenerates_only_it_with_cost_check(
    tmp_path, monkeypatch
):
    env = build_env(tmp_path, monkeypatch, filename="sht01b")
    await run_to_g0(env)

    rows = env.db.connect().execute(
        "SELECT id, character_id, role, version, status "
        "FROM character_sheets ORDER BY character_id"
    ).fetchall()
    assert len(rows) == 2
    versions_before = {row["character_id"]: row["version"] for row in rows}
    ids_before = {row["character_id"]: row["id"] for row in rows}
    calls_before = _sheet_call_count(env)
    assert calls_before == 2  # one call per (character, ref_full)

    reject_row = next(row for row in rows if row["character_id"] == CHAR_PRIMARY)
    g0 = pending_gates(env, "G0_CHARACTERS")
    assert len(g0) == 1
    service = GateService(env.db, env.repos, env.clock, env.dag, env.ids)
    service.request_changes(
        g0[0]["id"],
        "primary sheet needs a straighter face",
        [ShotRejection(shot_id=reject_row["id"], edited_prompt=None)],
    )
    await drain(env)

    # Only the rejected character regenerated: exactly one new sheet call,
    # its prompt is the rejected character's, and the untouched character's
    # sheet row is exactly the same row as before.
    assert _sheet_call_count(env) - calls_before == 1
    regen_prompts = [request.params["prompt"] for request in env.image.requests][calls_before:]
    assert len(regen_prompts) == 1
    assert CHAR_PRIMARY in regen_prompts[0]
    rows_mid = env.db.connect().execute(
        "SELECT id, character_id, version, status FROM character_sheets "
        "WHERE character_id = ?",
        (CHAR_SECOND,),
    ).fetchall()
    assert [(row["id"], row["version"]) for row in rows_mid] == [
        (ids_before[CHAR_SECOND], versions_before[CHAR_SECOND])
    ]
    regen = env.db.connect().execute(
        "SELECT id, version, status FROM character_sheets "
        "WHERE character_id = ? ORDER BY version",
        (CHAR_PRIMARY,),
    ).fetchall()
    assert [row["version"] for row in regen] == [
        versions_before[CHAR_PRIMARY],
        versions_before[CHAR_PRIMARY] + 1,
    ]
    assert regen[0]["status"] == "rejected"
    # Cost check: the regeneration carried a non-zero reservation through the ledger.
    for row in provider_calls(env, capability="image_reference_generate"):
        assert int(row["est_cost_musd"]) > 0

    # Approving G0 round 2 approves exactly the newest candidates.
    approve_parked(env, "G0_CHARACTERS", note="sheets approved after rework")
    await drain(env)
    approved = {
        (row["character_id"], row["version"])
        for row in env.db.connect().execute(
            "SELECT character_id, version FROM character_sheets WHERE status = 'approved'"
        ).fetchall()
    }
    assert approved == {
        (CHAR_SECOND, versions_before[CHAR_SECOND]),
        (CHAR_PRIMARY, versions_before[CHAR_PRIMARY] + 1),
    }


@pytest.mark.asyncio
async def test_sht_02_ref_face_is_conditioned_on_ref_full_sha(tmp_path, monkeypatch):
    """With roles [ref_full, ref_face] and ref_full already approved, S8a
    generates only ref_face — and every ref_face call carries the ref_full
    image as a reference (same prepared sha, one ref, ref clause in prompt)."""
    env = build_env(
        tmp_path,
        monkeypatch,
        filename="sht02",
        settings_overrides={"character_sheets": {"roles": ["ref_full", "ref_face"]}},
    )
    full_shas = {
        CHAR_PRIMARY: preseed_approved_sheet(
            env, tmp_path, character_id=CHAR_PRIMARY, role="ref_full",
            name="full-primary", variant="testsrc"),
        CHAR_SECOND: preseed_approved_sheet(
            env, tmp_path, character_id=CHAR_SECOND, role="ref_full",
            name="full-second", variant="smptebars"),
    }

    await run_to_g0(env)

    # Only ref_face was generated this pass (ref_full was approved).
    sheet_calls = [
        row for row in provider_calls(env, capability="image_reference_generate")
        if row["purpose"] == "character_sheet"
    ]
    assert len(sheet_calls) == 2  # one per character

    # Compute the expected prepared ref shas from the approved ref_full assets.
    from studio.domain.refs import AssetRef
    from studio.media.refs import prepare_ref

    expected_prepared: dict[str, str] = {}
    for character, full_sha in full_shas.items():
        asset_row = env.repos.assets.get(full_sha)
        assert asset_row is not None
        asset_ref = AssetRef(
            sha256=asset_row.sha256,
            rel_path=asset_row.rel_path,
            kind=asset_row.kind,
            mime=asset_row.mime,
            size_bytes=asset_row.size_bytes,
            duration_ms=asset_row.duration_ms,
            width=asset_row.width,
            height=asset_row.height,
        )
        prepared = await prepare_ref(env.cas, asset_ref, env.config.settings)
        expected_prepared[character] = prepared.sha256

    seen_characters = set()
    for request in env.image.requests:
        data_uris = request.params.get("refs") or []
        if len(data_uris) != 1:
            continue  # not a ref_face call (pure t2i shape)
        assert data_uris[0].startswith("data:image/jpeg;base64,")
        assert len(request.input_assets or []) == 1
        ref_sha = request.input_assets[0].sha256
        for character, expected in expected_prepared.items():
            if expected == ref_sha:
                seen_characters.add(character)
                assert character in request.params["prompt"]
                assert "reference image 1" in request.params["prompt"]
                break
        else:  # pragma: no cover - defensive
            raise AssertionError(f"ref_face request carried unknown ref sha {ref_sha}")
    assert seen_characters == {CHAR_PRIMARY, CHAR_SECOND}
