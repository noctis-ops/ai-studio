"""T-S1-* .. T-S4-*, T-E2E-TEXT-01 (PHASE2_IMPLEMENTATION_PROMPT §G).

Fully offline: the sandbox profile resolves mock.llm; zero spend. Scripted
mocks are injected through the gateway's `providers=` hook where a stage
needs adversarial behavior.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from conftest import make_home

from studio.config.loader import load_all
from studio.core.budget import LedgerService
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.core.gateway import ProviderGateway
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import RunStatus
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService
from studio.pipeline.stages_text import text_specs
from studio.pipeline.worker import Worker
from studio.production import seed_production
from studio.providers.base import LLMRequest, LLMResult
from studio.providers.mock.llm import MockLLMProvider

CONCEPT = "حَارِسُ فانوسٍ يتعلمُ أنَّ الضوءَ يُقتسمُ"
ALT_CONCEPT = "رحلةُ شمسٍ أُخرى مختلفةٌ تمامًا عن كلِّ ما سبق"


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"txt-{next(self._counter):06d}"


def _env(tmp_path, monkeypatch, *, filename="text", languages=("ar",), aspects=("9:16",),
         concept=CONCEPT, series_id=None, auto_approve=True, providers=None):
    home = make_home(tmp_path, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = _Ids()
    repos = build_repos(db, clock, ids)
    seeded = seed_production(
        config, repos, concept=concept, series_id=series_id,
        languages=list(languages), aspects=list(aspects), ids=ids,
    )
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id, profile="sandbox",
        auto_approve=auto_approve, config_snapshot_json=None,
    )
    repos.jobs.enqueue(kind="advance_run", payload={"run_id": run_row.id},
                       dedupe_key=f"advance:{run_row.id}")
    gateway = ProviderGateway(
        db=db, settings=config.settings, registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock, ids=ids, poll_interval_s=0.0, sleep=no_op_sleep,
        providers=providers,
    )
    dag = Dag(text_specs())
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config,
                    ids=ids, gateway=gateway, llm_subset_only=True)
    worker = Worker(db, engine, repos, config, clock, worker_id="test-text",
                    sleep=no_op_sleep)
    from types import SimpleNamespace

    return SimpleNamespace(
        config=config, db=db, clock=clock, ids=ids, repos=repos, seeded=seeded,
        run_row=run_row, engine=engine, worker=worker, dag=dag,
    )


async def _drain(worker):
    await worker.run(until_idle=True)


def _calls(db, where="1=1"):
    return db.connect().execute(
        f"SELECT purpose, model_key, cache_key, actual_cost_musd, status "
        f"FROM provider_calls WHERE {where} ORDER BY created_at, id"
    ).fetchall()


# ======================================================================
# T-S1-01 / T-S1-02 / T-S1-03 — bible_upsert
# ======================================================================


@pytest.mark.asyncio
async def test_s1_creates_bible_v1(tmp_path, monkeypatch):
    env = _env(
        tmp_path, monkeypatch, filename="s1a"
    )
    await _drain(env.worker)
    bibles = env.repos.bible.list_versions(env.seeded.series_id)
    assert len(bibles) == 1
    assert bibles[0].version == 1
    assert len(bibles[0].sha256) == 64
    audit_rows = env.db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = 'series_bible_updated'"
    ).fetchall()
    assert audit_rows, "expected a series_bible_updated audit row"
    # zero spend: every call settled at zero micro-USD on mock providers
    paid = env.db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE actual_cost_musd > 0"
    ).fetchone()[0]
    assert paid == 0


@pytest.mark.asyncio
async def test_s1_identical_rerun_is_idempotent_and_free(tmp_path, monkeypatch):
    env = _env(
        tmp_path, monkeypatch, filename="s1b", auto_approve=True
    )
    await _drain(env.worker)
    calls_before = len(_calls(env.db))
    bibles_before = len(env.repos.bible.list_versions(env.seeded.series_id))

    # Re-seed the SAME concept into the same series; run the text pipeline again.
    seeded2 = seed_production(
        env.config, env.repos, concept=CONCEPT, series_id=env.seeded.series_id,
        languages=["ar"], aspects=["9:16"], ids=env.ids,
    )
    run2 = env.repos.pipeline.create_run(
        episode_id=seeded2.episode_id, profile="sandbox",
        auto_approve=True, config_snapshot_json=None,
    )
    env.repos.jobs.enqueue(kind="advance_run", payload={"run_id": run2.id},
                       dedupe_key=f"advance:{run2.id}")
    await _drain(env.worker)
    bibles_after = env.repos.bible.list_versions(env.seeded.series_id)
    assert len(bibles_after) == bibles_before == 1  # no new version
    assert calls_before == len(_calls(env.db))  # gateway cache hit; provider untouched


@pytest.mark.asyncio
async def test_s1_changed_concept_creates_v2_and_keeps_v1(tmp_path, monkeypatch):
    env = _env(
        tmp_path, monkeypatch, filename="s1c", auto_approve=True
    )
    await _drain(env.worker)
    seeded2 = seed_production(
        env.config, env.repos, concept=ALT_CONCEPT, series_id=env.seeded.series_id,
        languages=["ar"], aspects=["9:16"], ids=env.ids,
    )
    run2 = env.repos.pipeline.create_run(
        episode_id=seeded2.episode_id, profile="sandbox",
        auto_approve=True, config_snapshot_json=None,
    )
    env.repos.jobs.enqueue(kind="advance_run", payload={"run_id": run2.id},
                       dedupe_key=f"advance:{run2.id}")
    await _drain(env.worker)
    versions = {b.version for b in env.repos.bible.list_versions(env.seeded.series_id)}
    assert versions == {1, 2}
    rows = env.db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = 'series_bible_updated' ORDER BY ts, id"
    ).fetchall()
    payloads = [json.loads(r["payload_json"]) for r in rows]
    assert any(p["version"] == 2 for p in payloads)


# ======================================================================
# T-S2-01 — narration lines persisted with tashkeel
# ======================================================================


@pytest.mark.asyncio
async def test_s2_narration_lines_persisted_with_tashkeel(tmp_path, monkeypatch):
    env = _env(
        tmp_path, monkeypatch, filename="s2a", languages=("ar",), auto_approve=True
    )
    await _drain(env.worker)
    rows = env.db.connect().execute(
        "SELECT l.text, d.language, s.shot_no "
        "FROM narration_lines l JOIN deliverables d ON d.id = l.deliverable_id "
        "JOIN shots s ON s.id = l.shot_id ORDER BY s.shot_no"
    ).fetchall()
    assert len(rows) == 3
    assert {row["language"] for row in rows} == {"ar"}
    from studio.language.tashkeel import TASHKEEL_SET

    for row in rows:
        assert any(ch in TASHKEEL_SET for ch in row["text"])
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value


# ======================================================================
# T-S2-02 — letters-altered verify ⇒ retry ⇒ terminal StudioError
# ======================================================================


class _AlwaysBrokenVerify(MockLLMProvider):
    async def complete(self, req: LLMRequest) -> LLMResult:
        if req.schema_name == "tashkeel_verify":
            base = await super().complete(req)
            data = json.loads(base.text or "{}")
            for line in data.get("lines", []):
                line["text"] = str(line.get("text", "")).replace("ف", "ق", 1)
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            return LLMResult(text=text, parsed_json=data,
                             tokens_in=base.tokens_in, tokens_out=base.tokens_out,
                             raw_ref=base.raw_ref)
        return await super().complete(req)


class _FirstVerifyBroken(MockLLMProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.verify_calls = 0

    async def complete(self, req: LLMRequest) -> LLMResult:
        if req.schema_name == "tashkeel_verify":
            self.verify_calls += 1
            base = await super().complete(req)
            if self.verify_calls == 1:
                data = json.loads(base.text or "{}")
                for line in data.get("lines", []):
                    line["text"] = str(line.get("text", "")).replace("ف", "ق", 1)
                text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                return LLMResult(text=text, parsed_json=data,
                                 tokens_in=base.tokens_in, tokens_out=base.tokens_out,
                                 raw_ref=base.raw_ref)
        return await super().complete(req)


@pytest.mark.asyncio
async def test_s2_letters_altered_terminal_failure_and_audit(tmp_path, monkeypatch):
    broken = _AlwaysBrokenVerify()
    env = _env(
        tmp_path, monkeypatch, filename="s2b", auto_approve=True,
        providers={"mock.llm": broken},
    )
    await _drain(env.worker)
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.FAILED.value
    failed = env.db.connect().execute(
        "SELECT status FROM stage_runs WHERE stage = 'narration_script'"
    ).fetchone()
    assert failed["status"] == "failed"
    audit = env.db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = 'tashkeel_mismatch'"
    ).fetchall()
    assert len(audit) == 1
    payload = json.loads(audit[0]["payload_json"])
    assert payload["attempts"] == 2
    assert payload["lines"]


# ======================================================================
# T-S2-03 — retry succeeds; exactly two verify calls (nonces differ)
# ======================================================================


@pytest.mark.asyncio
async def test_s2_retry_succeeds_with_two_verify_calls(tmp_path, monkeypatch):
    flaky = _FirstVerifyBroken()
    env = _env(
        tmp_path, monkeypatch, filename="s2c", auto_approve=True,
        providers={"mock.llm": flaky},
    )
    await _drain(env.worker)
    assert flaky.verify_calls == 2
    verify_rows = _calls(env.db, "purpose = 'tashkeel_verify'")
    assert len(verify_rows) == 2
    assert len({row["cache_key"] for row in verify_rows}) == 2
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value


# ======================================================================
# T-S3-01 — continuity violation ⇒ PermanentProviderError, zero writes
# ======================================================================


class _UnknownCharacterShots(MockLLMProvider):
    async def complete(self, req: LLMRequest) -> LLMResult:
        if req.schema_name == "shot_plan":
            data = {
                "shots": [
                    {"no": 1, "duration_s": 5, "visual_prompt": "مشهدٌ افتراضي",
                     "characters": ["شخصيةٌ مجهولةٌ خارجَ الإنجيل"], "camera": "ثابتة"},
                ]
            }
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            return LLMResult(text=text, parsed_json=data,
                             tokens_in=100, tokens_out=50)
        return await super().complete(req)


@pytest.mark.asyncio
async def test_s3_unknown_character_writes_nothing(tmp_path, monkeypatch):
    scripted = _UnknownCharacterShots()
    env = _env(
        tmp_path, monkeypatch, filename="s3a", auto_approve=True,
        providers={"mock.llm": scripted},
    )
    await _drain(env.worker)
    shots = env.db.connect().execute("SELECT count(*) FROM shots").fetchone()[0]
    assert shots == 0
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.FAILED.value


# ======================================================================
# T-S3-02 — order/durations preserved; pack sha feeds the input hash
# ======================================================================


@pytest.mark.asyncio
async def test_s3_shots_order_and_durations(tmp_path, monkeypatch):
    env = _env(
        tmp_path, monkeypatch, filename="s3b", auto_approve=True
    )
    await _drain(env.worker)
    shots = env.db.connect().execute(
        "SELECT shot_no, prompt FROM shots ORDER BY track_id, shot_no"
    ).fetchall()
    assert [row["shot_no"] for row in shots] == [1, 2, 3]
    durations = json.loads(
        env.db.connect().execute(
            "SELECT output_json FROM stage_runs WHERE stage = 'shot_plan'"
        ).fetchone()["output_json"]
    )["shots"]
    assert [float(s["duration_s"]) for s in durations] == [5.0, 6.0, 5.0]
    # the pack hash genuinely feeds every text-stage input_hash
    source = (Path(__file__).resolve().parents[1]
              / "src" / "studio" / "pipeline" / "stages_text.py").read_text(encoding="utf-8")
    assert source.count('"pack": pack.pack_sha256') == 4


# ======================================================================
# T-S4-01 / T-S4-02 — metadata_safety
# ======================================================================


@pytest.mark.asyncio
async def test_s4_metadata_written_per_deliverable(tmp_path, monkeypatch):
    env = _env(
        tmp_path, monkeypatch, filename="s4a",
        languages=("ar", "en"), aspects=("9:16",), auto_approve=True,
    )
    await _drain(env.worker)
    rows = env.db.connect().execute(
        "SELECT language, metadata_json, safety_status FROM deliverables ORDER BY language"
    ).fetchall()
    assert len(rows) == 2
    assert {row["language"] for row in rows} == {"ar", "en"}
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        assert metadata["title"] and metadata["tags"]
        assert row["safety_status"] == "clear"
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value


class _FlaggedMetadata(MockLLMProvider):
    async def complete(self, req: LLMRequest) -> LLMResult:
        if req.schema_name == "metadata_safety":
            data = {
                "title": "عنوان", "description": "وصف", "tags": ["وسم"],
                "safety": "flagged", "issues": ["عنفٌ محتمَل"],
            }
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            return LLMResult(text=text, parsed_json=data, tokens_in=50, tokens_out=30)
        return await super().complete(req)


@pytest.mark.asyncio
async def test_s4_flagged_content_parks_at_metadata_review(tmp_path, monkeypatch):
    scripted = _FlaggedMetadata()
    env = _env(
        tmp_path, monkeypatch, filename="s4b", auto_approve=False,
        providers={"mock.llm": scripted},
    )
    await _drain(env.worker)
    # G1 parks the run; approve it so S4 executes and the run parks at G3.
    g1 = env.db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G1_SCRIPT' AND status = 'pending'"
    ).fetchall()
    assert len(g1) == 1
    gates = GateService(env.db, env.repos, env.clock, Dag(text_specs()), env.ids)
    gates.approve(g1[0]["id"], "script ok")
    await _drain(env.worker)
    deliverables = env.db.connect().execute(
        "SELECT language, safety_status FROM deliverables"
    ).fetchall()
    assert deliverables and {row["safety_status"] for row in deliverables} == {"flagged"}
    # H5.7: the metadata review is a G1_SCRIPT (text) approval; the run parks
    # on the second G1 round, not on a final-cut gate.
    gates = env.db.connect().execute(
        "SELECT gate, status FROM approval_gates WHERE stage = 'metadata_safety'"
    ).fetchall()
    assert gates and all(row["gate"] == "G1_SCRIPT" for row in gates)
    assert all(row["status"] == "pending" for row in gates)
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value


# ======================================================================
# T-E2E-TEXT-01 — sandbox end-to-end through text pipeline (mocks)
# ======================================================================


@pytest.mark.asyncio
async def test_e2e_text_pipeline_parks_at_g1_then_completes(tmp_path, monkeypatch):
    env = _env(
        tmp_path, monkeypatch, filename="e2e", auto_approve=False
    )
    await _drain(env.worker)
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value
    g1 = env.db.connect().execute(
        "SELECT id, gate, status FROM approval_gates WHERE gate = 'G1_SCRIPT'"
    ).fetchall()
    assert len(g1) == 1 and g1[0]["status"] == "pending"

    # zero paid calls, zero paid ledger reservations
    paid_calls = env.db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE actual_cost_musd > 0"
    ).fetchone()[0]
    assert paid_calls == 0
    paid_ledger = env.db.connect().execute(
        "SELECT count(*) FROM cost_ledger WHERE entry_type = 'settle' AND amount_musd > 0"
    ).fetchone()[0]
    assert paid_ledger == 0

    # approve G1 → drain → parked at the metadata review → approve → completed
    gates = GateService(env.db, env.repos, env.clock, Dag(text_specs()), env.ids)
    gates.approve(g1[0]["id"], "looks right")
    await _drain(env.worker)
    g3 = env.db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = 'G1_SCRIPT' AND status = 'pending'"
    ).fetchall()
    assert len(g3) == 1
    gates.approve(g3[0]["id"], "ship it")
    await _drain(env.worker)
    run = env.repos.pipeline.get_run(env.run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value
