"""T-PRICE-02 (PHASE4 §P hardening): Gate-1 media estimate + S6 drift.

S4 stores an additive media estimate (exact TTS chars per line, video
low/expected/high from the S6-style duration chooser; unconfirmed models =>
"unavailable", never $0). S6 then compares its locked projection against the
S4 baseline and emits the `estimate_drift` audit past
`timing.estimate_drift_warn_pct`.

Offline: sandbox profile, mock providers, PAID_MOCK_PRICES so budget math is
non-degenerate. ffmpeg-backed mock audio runs against lavfi sine.
"""

from __future__ import annotations

import itertools
import json
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import PAID_MOCK_PRICES, make_home

from studio.audio.voice import VoiceSettings, voice_settings_json
from studio.config.loader import load_all
from studio.config.registry import Per1kChars
from studio.core.budget import LedgerService
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.core.gateway import ProviderGateway
from studio.core.pricing import usd_to_musd
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import Language
from studio.language import normalize_for_tts
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService
from studio.pipeline.specs import production_specs
from studio.pipeline.worker import Worker
from studio.production import seed_production

CONCEPT = "حَارِسُ فانوسٍ يتعلمُ أنَّ الضوءَ يُقتسمُ"


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"price-{next(self._counter):06d}"


def _env(tmp_path, monkeypatch, *, filename="price", registry_overrides=None,
         settings_overrides=None):

    overrides = PAID_MOCK_PRICES
    if registry_overrides:
        import copy

        merged = copy.deepcopy(PAID_MOCK_PRICES)

        from conftest import _deep_merge

        _deep_merge(merged, registry_overrides)
        overrides = merged
    home = make_home(tmp_path, registry_overrides=overrides,
                     settings_overrides=settings_overrides, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = _Ids()
    repos = build_repos(db, clock, ids)
    seeded = seed_production(
        config, repos, concept=CONCEPT, series_id=None,
        languages=["ar"], aspects=["9:16"], ids=ids,
    )
    repos.content.create_character(
        series_id=seeded.series_id, role="narrator", name="المذيع", voices_json="{}",
    )
    for language in seeded.languages:
        repos.audio.upsert_character_voice(
            series_id=seeded.series_id,
            character_slug="narrator",
            language=language.value,
            voice_id=f"mock-voice-{language.value}",
            settings_json=voice_settings_json(VoiceSettings(
                model_key="mock.tts", stability=0.5, similarity_boost=0.75,
                style=0.0, speaker_boost=True, speed=1.0, mix_gain_db=0.0,
            )),
        )
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id, profile="sandbox",
        auto_approve=False, config_snapshot_json=None,
    )
    repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": run_row.id},
        dedupe_key=f"advance:{run_row.id}",
    )
    gateway = ProviderGateway(
        db=db, settings=config.settings, registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock, ids=ids, poll_interval_s=0.0, sleep=no_op_sleep,
    )
    dag = Dag(production_specs())
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config,
                    ids=ids, gateway=gateway)
    worker = Worker(db, engine, repos, config, clock,
                    worker_id="price-test", sleep=no_op_sleep)
    from types import SimpleNamespace

    return SimpleNamespace(
        config=config, db=db, clock=clock, ids=ids, repos=repos, seeded=seeded,
        run_row=run_row, dag=dag, worker=worker,
    )


async def _drain(worker) -> None:
    await worker.run(until_idle=True)


def _gates(db, gate):
    return db.connect().execute(
        "SELECT id, status FROM approval_gates WHERE gate = ? ORDER BY round", (gate,)
    ).fetchall()


def _stage_output(db, stage: str) -> dict:
    rows = db.connect().execute(
        "SELECT output_json FROM stage_runs WHERE stage = ? AND status = 'succeeded'",
        (stage,),
    ).fetchall()
    assert rows, f"stage {stage} has no succeeded run"
    return json.loads(rows[-1]["output_json"])


def _audits(db, event: str):
    rows = db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = ?", (event,)
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def _estimate_from_s4(env) -> dict:
    out = _stage_output(env.db, "metadata_safety")
    assert "media_estimate" in out, f"S4 output lost the media estimate: {sorted(out)}"
    return out


def _approve_parked(env, gate: str):
    gates = env.db.connect().execute(
        "SELECT id FROM approval_gates WHERE gate = ? AND status = 'pending' ORDER BY round",
        (gate,),
    ).fetchall()
    assert len(gates) == 1, f"expected exactly one pending {gate} round"
    service = GateService(env.db, env.repos, env.clock, env.dag, env.ids)
    service.approve(gates[0]["id"], "approved in test")


def _no_musd_keys(node, path="$") -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("_musd") or key in ("video_expected",):
                hits.append(f"{path}.{key}={value}")
            hits.extend(_no_musd_keys(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits.extend(_no_musd_keys(value, f"{path}[{index}]"))
    return hits


@pytest.mark.asyncio
async def test_price_02_s4_media_estimate_is_exact_and_banded(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    settings = env.config.settings
    language = Language.AR

    await _drain(env.worker)  # parks at G1; S4 not reached yet
    _approve_parked(env, "G1_SCRIPT")
    await _drain(env.worker)  # S4 runs; parks at G3

    out = _estimate_from_s4(env)
    estimate = out["media_estimate"]
    assert estimate["version"] == 1
    assert estimate["available"] is True, estimate.get("reason")

    # ---- TTS: exact characters, never a band
    lines = env.repos.content.narration_lines_for_episode(env.seeded.episode_id)
    assert lines
    texts = [normalize_for_tts(language, line.approved_text, settings) for line in lines]
    total_chars = sum(len(text) for text in texts)
    assert total_chars > 0
    tts_entry = env.config.registry.models["mock.tts"]
    assert isinstance(tts_entry.pricing, Per1kChars)
    exact = sum(
        usd_to_musd((Decimal(len(text)) * tts_entry.pricing.usd) / Decimal(1000))
        for text in texts
    )
    assert estimate["tts"]["low_musd"] == exact
    assert estimate["tts"]["expected_musd"] == exact
    assert estimate["tts"]["high_musd"] == exact
    # sanity: with the paid mock price this is strictly positive, not $0
    assert exact > 0

    # ---- sheets / keyframes / video: ordered bands, all positive
    for section in ("sheets", "keyframes", "video"):
        band = estimate[section]
        assert band["low_musd"] > 0, section
        assert band["low_musd"] <= band["expected_musd"] <= band["high_musd"], section

    # ---- total ties out and additive top-level keys match the video band
    assert estimate["total"]["low_musd"] == (
        estimate["tts"]["low_musd"] + estimate["sheets"]["low_musd"]
        + estimate["keyframes"]["low_musd"] + estimate["video"]["low_musd"]
    )
    assert estimate["total"]["expected_musd"] == (
        estimate["tts"]["expected_musd"] + estimate["sheets"]["expected_musd"]
        + estimate["keyframes"]["expected_musd"] + estimate["video"]["expected_musd"]
    )
    assert out["gate1_video_estimate_musd"] == estimate["video"]["expected_musd"]
    assert out["projected_video_cost_musd"] == estimate["video"]["expected_musd"]


@pytest.mark.asyncio
async def test_price_02_unconfirmed_model_means_unavailable_never_zero(
    tmp_path, monkeypatch
):
    env = _env(
        tmp_path, monkeypatch,
        filename="price_unconfirmed",
        registry_overrides={"models": {"mock.video": {"needs_confirmation": True}}},
    )
    await _drain(env.worker)
    _approve_parked(env, "G1_SCRIPT")
    await _drain(env.worker)  # S4 runs; parked at G3

    out = _estimate_from_s4(env)
    estimate = out["media_estimate"]
    assert estimate["available"] is False
    assert "mock.video" in estimate["reason"]
    # Not one numeric cost key survives: never an implicit $0.
    assert _no_musd_keys(estimate) == []
    assert "gate1_video_estimate_musd" not in out
    assert "projected_video_cost_musd" not in out


@pytest.mark.asyncio
async def test_price_02_s6_emits_estimate_drift_past_warn_pct(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch, filename="price_drift",
               settings_overrides={"budgets": {"default_episode_cap_usd": 10000.0}})
    await _drain(env.worker)
    _approve_parked(env, "G1_SCRIPT")
    await _drain(env.worker)  # S4 done; parked at G3

    # Rewrite the S4 baseline to an absurdly low value (1 micro-USD): the S6
    # projection must trip the drift warning, not silently pass.
    row = env.db.connect().execute(
        "SELECT id, output_json FROM stage_runs "
        "WHERE stage = 'metadata_safety' AND status = 'succeeded'"
    ).fetchall()[-1]
    out = json.loads(row["output_json"])
    out["gate1_video_estimate_musd"] = 1
    out["projected_video_cost_musd"] = 1
    out["media_estimate"]["video"]["expected_musd"] = 1
    env.repos.pipeline.set_stage_run(row["id"], output_json=json.dumps(
        out, ensure_ascii=False, separators=(",", ":")))

    _approve_parked(env, "G3_FINAL_CUT")
    # The mutation invalidates the approval snapshot: the next advance
    # re-gates metadata_safety into a fresh pending round. Approve it too,
    # then let the audio chain run against the 1-musd baseline.
    await _drain(env.worker)
    _approve_parked(env, "G3_FINAL_CUT")
    await _drain(env.worker)  # audio chain runs; S6 performs the comparison

    drift_events = _audits(env.db, "estimate_drift")
    assert drift_events, "expected the S6 drift event to be recorded"
    event = drift_events[0]
    assert event["baseline_musd"] == 1
    assert event["projected_musd"] > event["baseline_musd"] * 1.25
    assert event["drift_pct"] > 25.0

    s6 = _stage_output(env.db, "s6_timing")
    assert s6["projected_video_cost_musd"] is not None
    assert s6["projected_video_cost_musd"] > 1
    assert s6["estimate_drift"]["baseline_musd"] == 1


def test_price_02_per_second_projection_uses_locked_durations(tmp_path, monkeypatch):
    """The S6 projection derives from S6-locked durations and the resolved
    video price (per_second), not from a hand-maintained constant."""
    env = _env(tmp_path, monkeypatch, filename="price_projection",
               settings_overrides={"budgets": {"default_episode_cap_usd": 10000.0}})

    async def run() -> None:
        await _drain(env.worker)
        _approve_parked(env, "G1_SCRIPT")
        await _drain(env.worker)
        _approve_parked(env, "G3_FINAL_CUT")
        await _drain(env.worker)

    import asyncio

    asyncio.run(run())
    s6 = _stage_output(env.db, "s6_timing")
    projected_seconds = float(s6["projected_video_seconds"])
    assert projected_seconds > 0
    video_entry = env.config.registry.models["mock.video"]
    assert usd_to_musd(Decimal(str(projected_seconds)) * video_entry.pricing.usd) == (
        s6["projected_video_cost_musd"]
    )
