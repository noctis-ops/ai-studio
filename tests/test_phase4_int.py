"""T-INT-01 (PHASE4 §P hardening): fake-LLM S1-S4 → approvals → S5a-S7b.

Production-shaped chain (seed_production + production_specs), sandbox profile
with mock providers. The text sent to TTS must equal the diacritized text
approved at Gate 1 (normalize_for_tts of narration_lines.approved_text), the
lines must be keyed to the right (deliverable, shot, line_no), and the audio
master must exist once S7b completes.

Fully offline; ffmpeg-backed mock TTS/mix run against lavfi-generated audio.
"""

from __future__ import annotations

import itertools
from collections import Counter
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
from studio.domain.enums import Language
from studio.language import normalize_for_tts
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService
from studio.pipeline.specs import production_specs
from studio.pipeline.worker import Worker
from studio.production import seed_production
from studio.providers.base import TTSRequest, TTSResult
from studio.providers.mock.tts import MockTTSProvider

CONCEPT = "حَارِسُ فانوسٍ يتعلمُ أنَّ الضوءَ يُقتسمُ"
AR_DIACRITICS = frozenset("ًٌٍَُِّْ")


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"int-{next(self._counter):06d}"


class _CaptureTTS:
    """Spy around the mock TTS provider: records every request verbatim."""

    def __init__(self, inner: MockTTSProvider) -> None:
        self._inner = inner
        self.requests: list[TTSRequest] = []

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        self.requests.append(req)
        return await self._inner.synthesize(req)


def _env(tmp_path, monkeypatch, *, filename="int", languages=("ar",), aspects=("9:16",),
         auto_approve=False):
    home = make_home(tmp_path, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = _Ids()
    repos = build_repos(db, clock, ids)
    seeded = seed_production(
        config, repos, concept=CONCEPT, series_id=None,
        languages=list(languages), aspects=list(aspects), ids=ids,
    )
    # The operator wires a narrator character + voice before audio can run
    # (same flow as `studio voice set`; required by the S5a preflight).
    from studio.audio.voice import VoiceSettings, voice_settings_json

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
                model_key="mock.tts",
                stability=0.5,
                similarity_boost=0.75,
                style=0.0,
                speaker_boost=True,
                speed=1.0,
                mix_gain_db=0.0,
            )),
        )
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id, profile="sandbox",
        auto_approve=auto_approve, config_snapshot_json=None,
    )
    repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": run_row.id},
        dedupe_key=f"advance:{run_row.id}",
    )
    spy = _CaptureTTS(MockTTSProvider(
        chars_per_second=int(config.settings.mock.tts_chars_per_second),
        workdir=home / "data" / "mock" / "tts",
        ids=ids,
    ))
    gateway = ProviderGateway(
        db=db, settings=config.settings, registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock, ids=ids, poll_interval_s=0.0, sleep=no_op_sleep,
        providers={"mock.tts": spy},
    )
    dag = Dag(production_specs())
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config,
                    ids=ids, gateway=gateway)
    worker = Worker(db, engine, repos, config, clock,
                    worker_id="int-test", sleep=no_op_sleep)
    return _env_ns(config, db, clock, ids, repos, seeded, run_row, spy, dag, worker)


class _ns:
    pass


def _env_ns(config, db, clock, ids, repos, seeded, run_row, spy, dag, worker):
    env = _ns()
    env.config = config
    env.db = db
    env.clock = clock
    env.ids = ids
    env.repos = repos
    env.seeded = seeded
    env.run_row = run_row
    env.spy = spy
    env.dag = dag
    env.worker = worker
    return env


async def _drain(worker) -> None:
    await worker.run(until_idle=True)


def _gates(db, gate):
    return db.connect().execute(
        "SELECT id, status FROM approval_gates WHERE gate = ? ORDER BY round", (gate,)
    ).fetchall()


def _tts_calls(db):
    return db.connect().execute(
        "SELECT count(*) AS n FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()["n"]


def _add_music_and_scene(env) -> str:
    """One loopable music track (file + library row) and one scene spanning
    the track's shots. Without a music track the S7 audio plan fails closed;
    without scene rows there is nothing to segment."""
    import hashlib
    import json as _json
    import subprocess

    home = Path(env.config.settings.paths.home)
    lib_dir = home / "library" / "music"
    lib_dir.mkdir(parents=True, exist_ok=True)
    music_path = lib_dir / "hopeful-dawn.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
            "-ar", "48000", "-ac", "2",
            str(music_path),
        ],
        check=True,
        capture_output=True,
    )
    sha = hashlib.sha256(music_path.read_bytes()).hexdigest()
    env.repos.audio.upsert_library_entry(
        sha256=sha,
        rel_path="music/hopeful-dawn.wav",
        kind="music",
        title="Hopeful Dawn",
        moods_json=_json.dumps(["hopeful"]),
        intensity=3,
        bpm=120,
        loopable=1,
        duration_ms=12_000,
        loudness_lufs=-24.0,
        license="CC0",
        license_url=None,
        source="test",
        attribution_required=0,
        attribution_text=None,
        active=1,
        scan_error=None,
    )
    for track_id in env.seeded.track_ids:
        shots = env.repos.content.shots_for_track(track_id)
        assert shots, "expected S3 to have created the shots"
        env.repos.content.upsert_scene(
            track_id=track_id,
            scene_no=1,
            shot_from=1,
            shot_to=len(shots),
            music_mood="hopeful",
            intensity=3,
            ambience_tags_json="[]",
        )
    return sha


@pytest.mark.asyncio
async def test_int_01_tts_receives_g1_approved_diacritized_text(
    tmp_path, monkeypatch
):
    env = _env(tmp_path, monkeypatch)
    settings = env.config.settings

    # ---- S1-S4 with fake LLM: parks at G1, nothing paid
    await _drain(env.worker)
    g1 = _gates(env.db, "G1_SCRIPT")
    assert len(g1) == 1 and g1[0]["status"] == "pending"
    assert int(_tts_calls(env.db)) == 0
    assert env.spy.requests == []

    # Snapshot the G1-approved text per line (the bytes the gate reviewed).
    lines = env.repos.content.narration_lines_for_episode(env.seeded.episode_id)
    assert lines, "expected narration lines to be persisted"
    expected = {
        line.id: normalize_for_tts(Language.AR, line.approved_text, settings)
        for line in lines
    }
    assert any(
        any(ch in AR_DIACRITICS for ch in line.approved_text) for line in lines
    ), "expected the approved text to carry tashkeel"

    # ---- Approve G1: run parks at the metadata review (a G1_SCRIPT round,
    #      H5.7), still no TTS
    gates = GateService(env.db, env.repos, env.clock, env.dag, env.ids)
    gates.approve(g1[0]["id"], "script approved")
    await _drain(env.worker)
    # The operator scans the audio library before audio stages can run
    # (S7 fails closed with zero active music tracks), and scenes exist.
    _add_music_and_scene(env)
    g3 = [row for row in _gates(env.db, "G1_SCRIPT") if row["status"] == "pending"]
    assert len(g3) == 1
    assert int(_tts_calls(env.db)) == 0
    assert env.spy.requests == []

    # ---- Approve the metadata review: s4p → s5a → s5 → s6 → s7 → s7b run
    gates.approve(g3[0]["id"], "metadata approved")
    await _drain(env.worker)

    # Every captured TTS request carries exactly the G1-approved, diacritized
    # text for its line — nothing else, nothing stripped.
    assert env.spy.requests, "expected the TTS stage to have run"
    sent = Counter(request.text for request in env.spy.requests)
    want = Counter(expected.values())
    assert sent == want
    for request in env.spy.requests:
        assert any(ch in AR_DIACRITICS for ch in request.text)

    # Provider-side accounting: one TTS call per line (mock TTS QC passes on
    # the first attempt).
    assert int(_tts_calls(env.db)) == len(lines)

    # Lines are keyed correctly: 1..N per deliverable, and each line points at
    # the shot with matching shot_no.
    for deliverable_id in env.seeded.deliverable_ids:
        dlines = env.repos.content.narration_lines(deliverable_id)
        assert [line.line_no for line in dlines] == list(range(1, len(dlines) + 1))
        deliverable = next(
            d for d in env.repos.content.list_deliverables(env.seeded.episode_id)
            if d.id == deliverable_id
        )
        shots = {
            shot.shot_no: shot.id
            for shot in env.repos.content.shots_for_track(deliverable.track_id)
        }
        for line in dlines:
            assert shots[line.line_no] == line.shot_id

    # The master exists exactly once per deliverable that went through S7b.
    masters = env.db.connect().execute(
        "SELECT count(*) AS n FROM audio_masters WHERE status = 'ready'"
    ).fetchone()["n"]
    assert int(masters) == len(env.seeded.deliverable_ids)
