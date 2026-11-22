"""Shared harness for the Phase 4 media-chain tests (sheets / keyframes /
clips / preflight / engine integration).

Production-shaped chain (seed_production + production_specs), sandbox
profile, mock providers for image/video/TTS wired through the real
ProviderGateway. Paid registry prices come from conftest.PAID_MOCK_PRICES so
budget math is non-degenerate; the default episode cap is raised to
10_000 USD (settings override) because the paid mock chain otherwise halts
at the shipped $10 cap long before the media stages run.

Fully offline. The mock providers render real lavfi output (ffmpeg
required).
"""

from __future__ import annotations

import itertools
import json
import subprocess
from pathlib import Path

from conftest import PAID_MOCK_PRICES, _deep_merge, build_gateway, make_home

from studio.config.loader import load_all
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.pipeline.dag import Dag
from studio.pipeline.engine import Engine
from studio.pipeline.gates import GateService
from studio.pipeline.specs import production_specs
from studio.pipeline.worker import Worker
from studio.production import seed_production
from studio.providers.mock.image import MockImageProvider
from studio.providers.mock.tts import MockTTSProvider
from studio.providers.mock.video import MockVideoProvider

CONCEPT = "حَارِسُ فانوسٍ يتعلمُ أنَّ الضوءَ يُقتسمُ"
CHAR_PRIMARY = "نُور"
CHAR_SECOND = "العاصفة"

# Mock prices make zero a legitimate sandbox price; paid mocks keep the
# budget math honest. Deep-merged so callers can pass deltas only.
def paid_prices(extra: dict | None = None) -> dict:
    import copy

    merged = copy.deepcopy(PAID_MOCK_PRICES)
    if extra:
        _deep_merge(merged, extra)
    return merged


class _Ids:
    _counter = itertools.count(1)

    def __init__(self, prefix: str = "media") -> None:
        self._prefix = prefix

    def new(self) -> str:
        return f"{self._prefix}-{next(self._counter):06d}"


class SpyImageProvider(MockImageProvider):
    """Records every MediaRequest verbatim on submit."""

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self.requests: list = []

    async def submit(self, req):  # type: ignore[no-untyped-def]
        self.requests.append(req)
        return await super().submit(req)


class SpyVideoProvider(MockVideoProvider):
    """Records every MediaRequest verbatim on submit."""

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self.requests: list = []

    async def submit(self, req):  # type: ignore[no-untyped-def]
        self.requests.append(req)
        return await super().submit(req)


def build_env(
    tmp_path: Path,
    monkeypatch,
    *,
    filename: str = "media",
    languages=("ar",),
    aspects=("9:16",),
    settings_overrides: dict | None = None,
    registry_overrides: dict | None = None,
    auto_approve: bool = False,
    paid: bool = True,
    image_provider=None,
    video_provider=None,
    tts_provider=None,
    ids: _Ids | None = None,
):
    merged_settings = {"budgets": {"default_episode_cap_usd": 10000.0}}
    if settings_overrides:
        _deep_merge(merged_settings, settings_overrides)
    merged_registry = paid_prices(registry_overrides) if paid else registry_overrides
    home = make_home(
        tmp_path,
        settings_overrides=merged_settings,
        registry_overrides=merged_registry,
        filename=filename,
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = ids or _Ids(filename)
    repos = build_repos(db, clock, ids)
    seeded = seed_production(
        config,
        repos,
        concept=CONCEPT,
        series_id=None,
        languages=list(languages),
        aspects=list(aspects),
        ids=ids,
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
        episode_id=seeded.episode_id,
        profile="sandbox",
        auto_approve=auto_approve,
        config_snapshot_json=None,
    )
    repos.jobs.enqueue(
        kind="advance_run", payload={"run_id": run_row.id},
        dedupe_key=f"advance:{run_row.id}",
    )
    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    mock_dir = home / "data" / "mock"
    mock_dir.mkdir(parents=True, exist_ok=True)
    image = image_provider or SpyImageProvider(
        ledger_path=mock_dir / "image_jobs.json",
        width_16x9=960,
        height_16x9=540,
        ids=ids,
        poll_ticks=2,
    )
    video = video_provider or SpyVideoProvider(
        ledger_path=mock_dir / "video_jobs.json",
        width_16x9=960,
        height_16x9=540,
        fps=24,
        ids=ids,
        poll_ticks=2,
    )
    tts = tts_provider or MockTTSProvider(
        chars_per_second=int(config.settings.mock.tts_chars_per_second),
        workdir=mock_dir / "tts",
        ids=ids,
    )
    gateway = build_gateway(
        db=db,
        config=config,
        clock=clock,
        ids=ids,
        cas=cas,
        providers={"mock.image": image, "mock.video": video, "mock.tts": tts},
    )
    dag = Dag(production_specs())
    engine = Engine(db=db, repos=repos, dag=dag, clock=clock, config=config,
                    ids=ids, gateway=gateway)
    worker = Worker(db, engine, repos, config, clock,
                    worker_id=f"{filename}-worker", sleep=no_op_sleep)
    return SimpleEnv(
        home=home, config=config, db=db, clock=clock, ids=ids, repos=repos,
        seeded=seeded, run_row=run_row, gateway=gateway, dag=dag, worker=worker,
        image=image, video=video, tts=tts, cas=cas,
    )


class SimpleEnv:
    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.__dict__.update(kwargs)


async def drain(env) -> None:
    await env.worker.run(until_idle=True)


def pending_gates(env, gate: str):
    return env.db.connect().execute(
        "SELECT id, round, status FROM approval_gates "
        "WHERE gate = ? AND status = 'pending' ORDER BY round",
        (gate,),
    ).fetchall()


def approve_parked(env, gate: str, *, note: str = "approved in test") -> None:
    gates = pending_gates(env, gate)
    assert len(gates) == 1, f"expected exactly one pending {gate} round, got {len(gates)}"
    service = GateService(env.db, env.repos, env.clock, env.dag, env.ids)
    service.approve(gates[0]["id"], note)


def add_music_and_scene(env) -> str:
    """One loopable music track + one scene per track (S7 fails closed without it)."""
    import hashlib

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
        moods_json=json.dumps(["hopeful"]),
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


async def run_to_g3(env) -> None:
    """Drain to the metadata park, with music + scenes wired.

    Phase 5 (H5.7): the metadata text review is re-kinded to ``G1_SCRIPT``
    (it is a script approval, not a cut approval), so the run parks on the
    *same kind* twice — round 1 is the script, round 2 is the metadata text.
    """
    await drain(env)
    assert pending_gates(env, "G1_SCRIPT"), "expected the run to park at G1"
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    add_music_and_scene(env)
    assert pending_gates(env, "G1_SCRIPT"), "expected the run to park at the metadata review"


async def run_to_g0(env) -> None:
    """Drain through the audio chain to the G0 park (character sheets)."""
    await run_to_g3(env)
    approve_parked(env, "G1_SCRIPT", note="metadata approved")
    await drain(env)
    assert pending_gates(env, "G0_CHARACTERS"), (
        "expected the run to park at G0 after sheet generation"
    )


async def run_to_g2(env) -> None:
    """Drain through sheets (G0 approved) to the G2 park (the *animatic*, S9)."""
    await run_to_g0(env)
    approve_parked(env, "G0_CHARACTERS", note="sheets approved")
    await drain(env)
    assert pending_gates(env, "G2_KEYFRAMES"), "expected the run to park at G2"


async def run_to_s10(env) -> None:
    """Drain the full production chain to completion.

    Phase 5: the chain now ends with S11 (assemble) and S12 (render QC), and
    S12 parks at G3. The helper approves the final cut with a sandbox checklist
    (every item "auto"-marked) so callers still reach a completed run.
    """
    await run_to_g2(env)
    approve_parked(env, "G2_KEYFRAMES", note="keyframes approved")
    await drain(env)
    if pending_gates(env, "G3_FINAL_CUT"):
        approve_parked(env, "G3_FINAL_CUT", note="final cut approved")
        await drain(env)


def stage_outputs(env, stage: str) -> list[dict]:
    rows = env.db.connect().execute(
        "SELECT output_json FROM stage_runs WHERE stage = ? AND status = 'succeeded'",
        (stage,),
    ).fetchall()
    return [json.loads(row["output_json"]) for row in rows]


def stage_rows(env, stage: str):
    return env.db.connect().execute(
        "SELECT * FROM stage_runs WHERE stage = ? ORDER BY created_at, id",
        (stage,),
    ).fetchall()


def audits(env, event: str) -> list[dict]:
    rows = env.db.connect().execute(
        "SELECT payload_json FROM audit_events WHERE event = ?", (event,)
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def provider_calls(env, *, capability: str | None = None):
    sql = "SELECT * FROM provider_calls"
    args: tuple = ()
    if capability is not None:
        sql += " WHERE capability = ?"
        args = (capability,)
    return env.db.connect().execute(sql + " ORDER BY created_at, id", args).fetchall()


_PRESEED_LAVFI = {
    # Visually distinct, non-black, rich-enough patterns whose bytes differ,
    # so distinct preseeded sheets never alias onto the same CAS sha.
    "testsrc2": "testsrc2=size=540x960:rate=24",
    "testsrc": "testsrc=size=540x960:rate=24",
    "smptebars": "smptebars=size=540x960:rate=24",
    "rgbtestsrc": "rgbtestsrc=size=540x960:rate=24",
}


def preseed_approved_sheet(
    env,
    tmp_path: Path,
    *,
    character_id: str,
    role: str = "ref_full",
    name: str = "preseed",
    variant: str = "testsrc2",
) -> str:
    """Insert a real image into the CAS plus an approved character_sheets row.

    Pass distinct ``variant`` values when seeding several sheets in one test —
    identical bytes share a CAS sha, which defeats ref attribution.

    Returns the CAS sha256 of the seeded sheet asset.
    """
    source = _PRESEED_LAVFI[variant]
    out = tmp_path / f"{name}-{abs(hash((character_id, role, variant)))}.png"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", source,
            "-frames:v", "1", str(out),
        ],
        check=True,
        capture_output=True,
    )
    asset = env.cas.put_file(out, kind="image", ext="png", retention="intermediate")
    env.repos.sheets.create(
        series_id=env.seeded.series_id,
        character_id=character_id,
        role=role,
        version=1,
        asset_id=asset.sha256,
        spec_hash="preseeded",
        nonce=f"preseed-{name}",
        status="approved",
    )
    return asset.sha256


async def run_to_g2_preseeded(env) -> None:
    """Drain to the G2 park when every sheet is already approved (no G0)."""
    await drain(env)
    approve_parked(env, "G1_SCRIPT", note="script approved")
    await drain(env)
    add_music_and_scene(env)
    approve_parked(env, "G1_SCRIPT", note="metadata approved")
    await drain(env)
    assert pending_gates(env, "G0_CHARACTERS") == []
    assert pending_gates(env, "G2_KEYFRAMES"), "expected the run to park at G2"
