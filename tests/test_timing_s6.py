"""T-S5-* / T-S6-* (PHASE3_IMPLEMENTATION_PROMPT §I): TTS preflight, per-line
TTS crash semantics, and the S6 timing reconciler.

S6 math is exercised by invoking the handler directly with a hand-built
RunContext (shots + voiced lines crafted in SQLite); T-S5-03 also invokes the
TTS handler directly (LINE-scope subject) while T-S6-09 exercises the full
worker + CLI flow (narration edit, resume, selective re-voice).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_engine import _engine, _env, _seed_run, _worker
from typer.testing import CliRunner

from studio.cli import app
from studio.core.cas import ContentStore
from studio.core.faults import SimulatedCrash
from studio.demo.seed import seed_demo
from studio.domain.enums import RunStatus, Scope
from studio.domain.errors import (
    BudgetExceeded,
    ConfigError,
    PreflightError,
    TtsQualityError,
)
from studio.domain.refs import SubjectRef
from studio.pipeline.stages_audio import TimingReconcilerHandler, TtsPreflightHandler

SAMPLE_RATE = 48000
FPS = 24
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS  # 2000
LEAD_IN_MS = 150
TAIL_MS = 200
# span = lead-in + line + tail (single line per shot)
_LEAD_IN = LEAD_IN_MS * SAMPLE_RATE // 1000      # 7200
_TAIL = TAIL_MS * SAMPLE_RATE // 1000            # 9600

runner = CliRunner()


def _voice_line(repos, cas, *, deliverable_id, shot_id, line_no, text,
                conformed_samples):
    raw_ref = cas.put_bytes(b"raw-" + text.encode("utf-8"), kind="audio", ext="mp3")
    conf_ref = cas.put_bytes(
        b"\x00\x00" * conformed_samples, kind="audio", ext="wav",
    )
    line = repos.content.upsert_narration_line(
        deliverable_id=deliverable_id, shot_id=shot_id, line_no=line_no,
        text=text, asset_sha256=raw_ref.sha256, duration_ms=1,
    )
    repos.content.mark_line_voiced(
        line.id, asset_sha256=raw_ref.sha256, duration_ms=1,
        conformed_sha256=conf_ref.sha256, conformed_samples=conformed_samples,
        voice_snapshot_json="{}",
    )
    return line


def _s6_env(tmp_path, monkeypatch, *, filename, languages=("ar",),
            settings_overrides=None, registry_overrides=None, paid=False):
    config, db, clock, ids, repos, home = _env(
        tmp_path, monkeypatch, filename=filename,
        settings_overrides=settings_overrides,
        registry_overrides=registry_overrides, paid=paid,
    )
    seeded = seed_demo(config, repos, languages=list(languages), aspects=["9:16"], ids=ids)
    track_id = seeded.track_ids[0]
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id, profile="sandbox",
        auto_approve=True, config_snapshot_json=None,
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    ctx = engine._context(run_row, None)
    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    return config, db, clock, ids, repos, home, seeded, track_id, run_row, ctx, cas


def _track_subject(seeded, track_id) -> SubjectRef:
    return SubjectRef(
        scope=Scope.TRACK,
        id=track_id,
        lineage=((Scope.SERIES, seeded.series_id), (Scope.EPISODE, seeded.episode_id)),
    )


def _episode_subject(seeded) -> SubjectRef:
    return SubjectRef(
        scope=Scope.EPISODE,
        id=seeded.episode_id,
        lineage=((Scope.SERIES, seeded.series_id),),
    )


def _shots_by_no(repos, track_id):
    return {s.shot_no: s for s in repos.content.shots_for_track(track_id)}


def _span_samples(line_samples, n_lines=1):
    gap = 120 * SAMPLE_RATE // 1000
    return _LEAD_IN + n_lines * line_samples + (n_lines - 1) * gap + _TAIL


def _locked(repos, shot_id):
    shot = repos.content.get_shot(shot_id)
    assert shot is not None
    return shot


# ======================================================================
# T-S6-01..05: clip-duration / speed / hold math
# ======================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line_seconds", "expect_clip_ms"),
    [
        (3.85, 5000),    # need 4.2 s -> 5 s clip, untouched (T-S6-01)
        (5.25, 5000),    # need 5.6 s, prefer_stretch=true -> squeezed 5 s (T-S6-02)
        (5.84, 5000),    # need 6.2 s, prefer_stretch=true -> 1/1.15 speed (T-S6-04)
        (6.2, 10000),    # need 6.4 s -> 5 s infeasible even stretched -> 10 s (T-S6-05)
    ],
)
async def test_t_s6_01_to_05_duration_selection(
    tmp_path, monkeypatch, line_seconds, expect_clip_ms,
):
    ctx_env = _s6_env(tmp_path, monkeypatch, filename=f"s6-{line_seconds}")
    (_config, _db, _clock, _ids, repos, _home, seeded,
     track_id, _run_row, ctx, cas) = ctx_env
    deliverables = repos.content.list_deliverables(seeded.episode_id)
    assert len(deliverables) == 1
    line_samples = int(line_seconds * SAMPLE_RATE)
    shot = repos.content.create_shot(
        track_id=track_id, shot_no=1, prompt="p", min_duration_ms=None,
    )
    _voice_line(
        repos, cas, deliverable_id=deliverables[0].id, shot_id=shot.id,
        line_no=1, text="klm", conformed_samples=line_samples,
    )
    handler = TimingReconcilerHandler()
    await handler.run(ctx, _track_subject(seeded, track_id))
    locked = _locked(repos, shot.id)
    assert locked.clip_duration_ms == expect_clip_ms
    assert locked.timing_status == "locked"


@pytest.mark.asyncio
async def test_t_s6_02_squeeze_uses_speed_ratio(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, cas = _s6_env(
        tmp_path, monkeypatch, filename="s6-02",
    )
    deliverables = repos.content.list_deliverables(seeded.episode_id)
    line_samples = int(5.25 * SAMPLE_RATE)   # span 5.6 s exactly
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="p")
    _voice_line(
        repos, cas, deliverable_id=deliverables[0].id, shot_id=shot.id,
        line_no=1, text="klm", conformed_samples=line_samples,
    )
    await TimingReconcilerHandler().run(ctx, _track_subject(seeded, track_id))
    locked = _locked(repos, shot.id)
    need_frames = -(-_span_samples(line_samples) // SAMPLES_PER_FRAME)
    need_ms = -(-need_frames * 1000 // FPS)
    assert locked.clip_duration_ms == 5000
    assert locked.speed_factor == pytest.approx(max(1 / 1.15, 5000 / need_ms), abs=1e-9)
    assert locked.speed_factor == pytest.approx(0.889, abs=0.005)   # spec: ~0.893
    assert locked.hold_frames == 0


@pytest.mark.asyncio
async def test_t_s6_03_prefer_stretch_false_uses_plain_duration(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, cas = _s6_env(
        tmp_path, monkeypatch, filename="s6-03",
        settings_overrides={"timing": {"prefer_stretch_to_save_cost": False}},
    )
    deliverables = repos.content.list_deliverables(seeded.episode_id)
    line_samples = int(5.25 * SAMPLE_RATE)   # need 5.6 s -> plain pick is 10 s
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="p")
    _voice_line(
        repos, cas, deliverable_id=deliverables[0].id, shot_id=shot.id,
        line_no=1, text="klm", conformed_samples=line_samples,
    )
    await TimingReconcilerHandler().run(ctx, _track_subject(seeded, track_id))
    locked = _locked(repos, shot.id)
    assert locked.clip_duration_ms == 10000
    assert locked.speed_factor == 1.0
    assert locked.hold_frames == 0


@pytest.mark.asyncio
async def test_t_s6_04_speed_floor_and_bounded_hold(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, cas = _s6_env(
        tmp_path, monkeypatch, filename="s6-04",
    )
    deliverables = repos.content.list_deliverables(seeded.episode_id)
    line_samples = int(5.84 * SAMPLE_RATE)   # span 6.19 s -> need 149 frames
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="p")
    _voice_line(
        repos, cas, deliverable_id=deliverables[0].id, shot_id=shot.id,
        line_no=1, text="klm", conformed_samples=line_samples,
    )
    await TimingReconcilerHandler().run(ctx, _track_subject(seeded, track_id))
    locked = _locked(repos, shot.id)
    assert locked.clip_duration_ms == 5000
    assert locked.speed_factor == pytest.approx(1 / 1.15, abs=1e-9)
    # hold fills the remainder and stays within max_hold_ms (500 ms at 24 fps)
    assert 0 < locked.hold_frames <= 500 * FPS // 1000


@pytest.mark.asyncio
async def test_t_s6_06_overflow_reports_all_shots(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, cas = _s6_env(
        tmp_path, monkeypatch, filename="s6-06",
    )
    deliverables = repos.content.list_deliverables(seeded.episode_id)
    long_line = int(12.0 * SAMPLE_RATE)   # span 12.35 s -> need 297 f > cap 288 f
    for shot_no in (1, 2):
        shot = repos.content.create_shot(track_id=track_id, shot_no=shot_no, prompt="p")
        _voice_line(
            repos, cas, deliverable_id=deliverables[0].id, shot_id=shot.id,
            line_no=shot_no, text=f"line {shot_no}", conformed_samples=long_line,
        )
    text_before = {line.id: line.approved_text for d in deliverables
                   for line in repos.content.narration_lines(d.id)}
    assert text_before
    from studio.domain.errors import TimingOverflowError

    with pytest.raises(TimingOverflowError) as excinfo:
        await TimingReconcilerHandler().run(ctx, _track_subject(seeded, track_id))
    message = str(excinfo.value)
    assert "2 shot(s)" in message
    for shot in repos.content.shots_for_track(track_id):
        assert shot.timing_status == "overflow"
    # spec: narration text is never modified by S6
    text_after = {line.id: line.approved_text for d in deliverables
                  for line in repos.content.narration_lines(d.id)}
    assert text_before == text_after


@pytest.mark.asyncio
async def test_t_s6_07_longer_language_drives_duration(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, cas = _s6_env(
        tmp_path, monkeypatch, filename="s6-07", languages=("ar", "en"),
        settings_overrides={"timing": {"prefer_stretch_to_save_cost": False}},
    )
    deliverables = {d.language: d for d in repos.content.list_deliverables(seeded.episode_id)}
    assert set(deliverables) == {"ar", "en"}
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="p")
    # ar is short (3.0 s -> would pick 5 s), en is long (5.6 s -> forces 10 s)
    _voice_line(
        repos, cas, deliverable_id=deliverables["ar"].id, shot_id=shot.id,
        line_no=1, text="قصير", conformed_samples=int(3.0 * SAMPLE_RATE),
    )
    _voice_line(
        repos, cas, deliverable_id=deliverables["en"].id, shot_id=shot.id,
        line_no=1, text="a much longer english narration line",
        conformed_samples=int(5.25 * SAMPLE_RATE),
    )
    await TimingReconcilerHandler().run(ctx, _track_subject(seeded, track_id))
    locked = _locked(repos, shot.id)
    assert locked.clip_duration_ms == 10000
    assert locked.timing_status == "locked"


# ======================================================================
# T-S6-08: Gate-1 baseline drift emits `estimate_drift`
# ======================================================================

@pytest.mark.asyncio
async def test_t_s6_08_estimate_drift_audit_event(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home, seeded, track_id, run_row, ctx, cas = _s6_env(
        tmp_path, monkeypatch, filename="s6-08", paid=True,
    )
    deliverables = repos.content.list_deliverables(seeded.episode_id)
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="p")
    # 6.4 s need -> 10 s clip; video at $2/s -> 20 USD projected
    _voice_line(
        repos, cas, deliverable_id=deliverables[0].id, shot_id=shot.id,
        line_no=1, text="klm", conformed_samples=int(6.2 * SAMPLE_RATE),
    )
    # fabricate the Gate-1 stage output with a tiny projection (1 musd)
    for d in deliverables:
        base = repos.pipeline.create_stage_run(
            run_id=run_row.id, stage="metadata_safety",
            subject=SubjectRef(
                scope=Scope.DELIVERABLE, id=d.id,
                lineage=((Scope.SERIES, seeded.series_id),
                         (Scope.EPISODE, seeded.episode_id)),
            ),
        )
        repos.pipeline.set_stage_run(
            base.id, status="succeeded",
            output_json=json.dumps({"projected_video_cost_musd": 1}),
        )
    out = await TimingReconcilerHandler().run(ctx, _track_subject(seeded, track_id))
    events = repos.audit.list(event="estimate_drift")
    assert len(events) == 1
    payload = events[0].payload_json
    data = json.loads(payload) if isinstance(payload, str) else payload
    assert data["baseline_musd"] == 1
    assert data["projected_musd"] == 20_000_000
    assert data["drift_pct"] > 25
    assert out.output["estimate_drift"]["projected_musd"] == 20_000_000
    assert out.output["projected_video_seconds"] == pytest.approx(10.0)


# ======================================================================
# T-S6-10: j_cut_ms != 0 -> ConfigError
# ======================================================================

def test_t_s6_10_j_cut_ms_rejected(tmp_path, monkeypatch):
    """j_cut_ms != 0 is a ConfigError — enforced at settings load time
    (Settings._check_cross_field) and again at the S6 entry point as
    defense in depth."""
    import conftest

    home = conftest.make_home(
        tmp_path, filename="s6-10", settings_overrides={"timing": {"j_cut_ms": 250}},
    )
    from studio.config.loader import load_all

    with pytest.raises(ConfigError, match="j_cut_ms"):
        load_all(home)


# ======================================================================
# T-S5-01: preflight lists every missing voice, spends nothing;
#          an over-budget worst case halts before the first call
# ======================================================================

@pytest.mark.asyncio
async def test_t_s5_01_preflight_lists_all_missing_voices(tmp_path, monkeypatch):
    config, db, clock, ids, repos, _home, seeded, *_ = _s6_env(
        tmp_path, monkeypatch, filename="s5-01", languages=("ar", "en"),
    )
    db.connect().execute("DELETE FROM character_voices")
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id, profile="sandbox",
        auto_approve=True, config_snapshot_json=None,
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    ctx = engine._context(run_row, None)
    with pytest.raises(PreflightError) as excinfo:
        await TtsPreflightHandler().run(ctx, _episode_subject(seeded))
    message = str(excinfo.value)
    assert "missing voice for (narrator, ar)" in message
    assert "missing voice for (narrator, en)" in message
    assert "TTS preflight failed before any spend" in message
    assert db.connect().execute(
        "SELECT count(*) FROM provider_calls"
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_t_s5_01_preflight_halts_over_budget_before_first_call(
    tmp_path, monkeypatch,
):
    config, db, clock, ids, repos, _home, seeded, track_id, *_ = _s6_env(
        tmp_path, monkeypatch, filename="s5-01b",
        settings_overrides={"budgets": {"default_episode_cap_usd": 0.000001}},
        paid=True,
    )
    # narration lines exist only after the demo_voice stage in the real DAG;
    # craft one so the worst-case estimate is non-zero
    deliverable = repos.content.list_deliverables(seeded.episode_id)[0]
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="p")
    repos.content.upsert_narration_line(
        deliverable_id=deliverable.id, shot_id=shot.id, line_no=1,
        text="هذه جملة عربية طويلة نسبيا لأجل حساب التكلفة القصوى المتوقعة لتوليد الكلام",
        asset_sha256=None, duration_ms=None,
    )
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id, profile="sandbox",
        auto_approve=True, config_snapshot_json=None,
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    ctx = engine._context(run_row, None)
    with pytest.raises(BudgetExceeded):
        await TtsPreflightHandler().run(ctx, _episode_subject(seeded))
    assert db.connect().execute(
        "SELECT count(*) FROM provider_calls"
    ).fetchone()[0] == 0


# ======================================================================
# T-S5-02: crash mid-S5; restart repays nothing; nonce persists
# ======================================================================

@pytest.mark.ffmpeg
async def test_t_s5_02_crash_mid_s5_restart_repays_nothing(tmp_path, monkeypatch):
    import studio.pipeline.stages_audio as stages_audio

    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="s5-02", paid=False,
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16",), auto_approve=True,
    )

    original = stages_audio.conform_line
    state = {"calls": 0}

    async def crasher(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise SimulatedCrash("crash inside s5_tts (second line)")
        return await original(*args, **kwargs)

    monkeypatch.setattr(stages_audio, "conform_line", crasher)
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    with pytest.raises(SimulatedCrash):
        await worker.run(until_idle=True)

    tts_after_crash = db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()[0]
    assert tts_after_crash == 2

    from datetime import timedelta

    clock.advance(timedelta(seconds=3600))
    engine2, _dag2 = _engine(db, repos, config, clock, ids)
    worker2 = _worker(db, engine2, repos, config, clock, ids)
    await worker2.run(until_idle=True)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == RunStatus.COMPLETED.value
    tts_after = db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()[0]
    assert tts_after == 3   # 3 lines, exactly one paid call each; line 1 not repaid
    succeeded = db.connect().execute(
        "SELECT count(*) FROM stage_runs WHERE stage = 's5_tts' AND status = 'succeeded'"
    ).fetchone()[0]
    assert succeeded == 3


# ======================================================================
# T-S5-03: TTS QC failure -> auto_retries paid regenerations -> TtsQualityError
# ======================================================================

@pytest.mark.ffmpeg
async def test_t_s5_03_quality_failure_retries_then_raises(tmp_path, monkeypatch):
    from studio.pipeline.stages_audio import TtsLineHandler

    _config, db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, _cas = _s6_env(
        tmp_path, monkeypatch, filename="s5-03",
        settings_overrides={"audio": {"qc": {"silent_line_rms_db": 100.0}}},
        paid=False,
    )
    deliverables = repos.content.list_deliverables(seeded.episode_id)
    shot = repos.content.create_shot(track_id=track_id, shot_no=1, prompt="p")
    line = repos.content.upsert_narration_line(
        deliverable_id=deliverables[0].id, shot_id=shot.id, line_no=1,
        text="تجربة", asset_sha256=None, duration_ms=None,
    )
    subject = SubjectRef(
        scope=Scope.LINE, id=line.id,
        lineage=((Scope.SERIES, seeded.series_id), (Scope.EPISODE, seeded.episode_id)),
    )
    with pytest.raises(TtsQualityError, match="failed TTS QC"):
        await TtsLineHandler().run(ctx, subject)
    tts_calls = db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()[0]
    assert tts_calls == 2   # initial attempt + exactly auto_retries (=1) regeneration
    row = db.connect().execute(
        "SELECT tts_nonce FROM narration_lines WHERE id = ?", (line.id,)
    ).fetchone()
    assert row[0].endswith(".q1")   # one regeneration suffix applied


# ======================================================================
# T-S5-04: context truncation + diacritics toggle reach the wire payload
# ======================================================================

_AR_LINES = [
    "السَّلَامُ عَلَيْكُمْ وَرَحْمَةُ اللهِ",
    "أَهْلًا بِكُمْ فِي هَذَا الْمَشْهَدِ الْجَمِيلِ",
    "نَتَمَنَّى لَكُمْ مُشَاهَدَةً طَيِّبَةً",
]


def _three_arabic_lines(repos, seeded, track_id):
    deliverable = repos.content.list_deliverables(seeded.episode_id)[0]
    lines = []
    for shot_no, text in enumerate(_AR_LINES, start=1):
        shot = repos.content.create_shot(track_id=track_id, shot_no=shot_no, prompt="p")
        line = repos.content.upsert_narration_line(
            deliverable_id=deliverable.id, shot_id=shot.id, line_no=shot_no,
            text=text, asset_sha256=None, duration_ms=None,
        )
        lines.append(line)
    return deliverable, lines


async def _run_lines(ctx, seeded, lines):
    from studio.pipeline.stages_audio import TtsLineHandler

    handler = TtsLineHandler()
    for line in lines:
        subject = SubjectRef(
            scope=Scope.LINE, id=line.id,
            lineage=(
                (Scope.SERIES, seeded.series_id),
                (Scope.EPISODE, seeded.episode_id),
            ),
        )
        await handler.run(ctx, subject)


def _tts_requests(db):
    rows = db.connect().execute(
        "SELECT request_json, cache_key FROM provider_calls "
        "WHERE capability = 'tts' ORDER BY created_at, id"
    ).fetchall()
    return [(json.loads(r[0]), r[1]) for r in rows]


@pytest.mark.ffmpeg
async def test_t_s5_04_context_truncated_and_neighbor_keys(tmp_path, monkeypatch):
    registry_overrides = {
        "models": {
            "mock.tts": {
                "tts_params": {
                    "output_format": "mp3_44100_128",
                    "supports_context": True,
                    "supports_language_code": False,
                },
            },
        },
    }
    _config, db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, _cas = _s6_env(
        tmp_path, monkeypatch, filename="s5-04ctx",
        settings_overrides={"tts": {"context_max_chars": 8, "use_context": True}},
        registry_overrides=registry_overrides, paid=False,
    )
    _deliverable, lines = _three_arabic_lines(repos, seeded, track_id)
    await _run_lines(ctx, seeded, lines)

    calls = _tts_requests(db)
    assert len(calls) == 3
    (req0, _), (req1, _), (req2, _) = calls
    assert req0["previous_text"] is None
    assert req0["next_text"] is not None and len(req0["next_text"]) == 8
    assert req1["previous_text"] is not None and len(req1["previous_text"]) == 8
    assert req1["next_text"] is not None and len(req1["next_text"]) == 8
    assert req2["previous_text"] is not None and len(req2["previous_text"]) == 8
    assert req2["next_text"] is None
    # context is truncated from the neighbor, not from the line text itself
    assert req0["next_text"] == lines[1].text[:8]
    assert req2["previous_text"] == lines[1].text[-8:]


@pytest.mark.ffmpeg
async def test_t_s5_04_keep_diacritics_false_strips_and_changes_key(tmp_path, monkeypatch):
    import re

    diacritics = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
    kept: list[list[tuple]] = []
    for keep in (True, False):
        _config, db, _clock, _ids, repos, _home, seeded, track_id, _run, ctx, _cas = _s6_env(
            tmp_path, monkeypatch, filename=f"s5-04b-{int(keep)}",
            settings_overrides={"tts": {"keep_diacritics": keep}},
            paid=False,
        )
        _deliverable, lines = _three_arabic_lines(repos, seeded, track_id)
        await _run_lines(ctx, seeded, lines)
        kept.append(_tts_requests(db))

    kept_true, kept_false = kept
    texts_true = [req["text"] for req, _ in kept_true]
    texts_false = [req["text"] for req, _ in kept_false]
    assert all(diacritics.search(t) for t in texts_true)
    assert not any(diacritics.search(t) for t in texts_false)
    assert texts_true != texts_false
    assert [key for _req, key in kept_true] != [key for _req, key in kept_false]


# ======================================================================
# T-S6-09: `narration edit --file` — text_final, audit row with hashes,
# and resume re-voices only that line (UTF-8 Arabic round-trip)
# ======================================================================


def _gates_walk(home: Path, db_path: Path) -> None:
    """Approve every pending gate and drain the worker (three gate rounds)."""
    import sqlite3

    for _ in range(3):
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id FROM approval_gates WHERE status='pending'"
        ).fetchall()
        conn.close()
        if not rows:
            return
        for (gate_id,) in rows:
            result = runner.invoke(app, ["gates", "approve", gate_id])
            assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["worker", "--until-idle"])
        assert result.exit_code == 0, result.output


@pytest.mark.ffmpeg
def test_t_s6_09_narration_edit_and_resume_revoices_one_line(tmp_path, monkeypatch):
    import shutil
    import sqlite3

    home = tmp_path / "s6-09"
    home.mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    shutil.copytree(repo_root / "configs", home / "configs")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    db_path = home / "data" / "studio.db"

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["run", "--demo", "--profile", "sandbox", "--languages", "ar"])
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip()
        for line in result.output.splitlines()
        if line.strip().startswith("run id:")
    )

    # advance to the point where narration lines exist but S5 has not run yet:
    # worker pauses at G1_SCRIPT; approving G1 + G2 unlocks the pre-TTS stages
    result = runner.invoke(app, ["worker", "--until-idle"])
    assert result.exit_code == 0, result.output

    def _pending_gate_ids():
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id FROM approval_gates WHERE status='pending'"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    pending = _pending_gate_ids()
    assert len(pending) == 1
    result = runner.invoke(app, ["gates", "approve", pending[0]])   # G1_SCRIPT
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["worker", "--until-idle"])          # pauses at G2_KEYFRAMES
    assert result.exit_code == 0, result.output
    pending = _pending_gate_ids()
    assert len(pending) == 1
    result = runner.invoke(app, ["gates", "approve", pending[0]])   # G2_KEYFRAMES
    assert result.exit_code == 0, result.output

    # replace the middle line with a single giant "word": the mock TTS renders
    # chars/15 seconds of audio while the estimator expects ~0.4 s -> QC fails.
    # The injected text is picked up by S5 as soon as the next worker pass runs.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    lines = conn.execute(
        "SELECT id, text FROM narration_lines ORDER BY shot_id, line_no"
    ).fetchall()
    assert len(lines) >= 2
    original_text_by_id = {str(row["id"]): str(row["text"]) for row in lines}
    victim_id = str(lines[1]["id"])
    giant_text = " x" * 0 + "x" * 2000
    conn.execute("UPDATE narration_lines SET text = ? WHERE id = ?", (giant_text, victim_id))
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["worker", "--until-idle"])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run_status = conn.execute(
        "SELECT status FROM pipeline_runs WHERE id = ?", (run_id,)
    ).fetchone()["status"]
    assert run_status == "failed"   # TtsQualityError on the giant line
    victim = conn.execute(
        "SELECT id, text, text_final, tts_nonce FROM narration_lines WHERE id = ?",
        (victim_id,),
    ).fetchone()
    assert str(victim["text"]) == giant_text
    tts_before = conn.execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()[0]
    assert tts_before >= 2   # the giant line was paid (initial + at least one retry)
    conn.close()

    # operator edits the failing line with a UTF-8 Arabic file (post-Gate-1 edit)
    edit_file = tmp_path / "edit.txt"
    edit_file.write_text("نص عربي قصير معدّل بعد البوابة الأولى", encoding="utf-8")
    result = runner.invoke(app, ["narration", "edit", victim_id, "--file", str(edit_file)])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT text_final FROM narration_lines WHERE id = ?", (victim_id,)
    ).fetchone()
    assert row["text_final"] == "نص عربي قصير معدّل بعد البوابة الأولى"
    audit = conn.execute(
        "SELECT payload_json FROM audit_events WHERE event = 'post_gate_edit' "
        "AND entity = ? ORDER BY ts DESC LIMIT 1",
        (f"narration_line:{victim_id}",),
    ).fetchone()
    assert audit is not None
    payload = json.loads(audit["payload_json"])
    import hashlib

    assert payload["old_text_sha256"] == hashlib.sha256(giant_text.encode("utf-8")).hexdigest()
    assert payload["new_text_sha256"] == hashlib.sha256(
        "نص عربي قصير معدّل بعد البوابة الأولى".encode()
    ).hexdigest()
    conn.close()

    # resume: re-voices the edited line; lines never voiced yet run for the
    # first time; line 1 is NOT re-paid
    result = runner.invoke(app, ["resume", run_id])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["worker", "--until-idle"])
    assert result.exit_code == 0, result.output
    _gates_walk(home, db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run_status = conn.execute(
        "SELECT status FROM pipeline_runs WHERE id = ?", (run_id,)
    ).fetchone()["status"]
    assert run_status == "completed"
    tts_after = conn.execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()[0]
    narration_calls = [
        json.loads(r[0])["text"]
        for r in conn.execute(
            "SELECT request_json FROM provider_calls WHERE capability = 'tts'"
        ).fetchall()
    ]
    conn.close()
    new_text = "نص عربي قصير معدّل بعد البوابة الأولى"
    assert narration_calls.count(new_text) == 1               # edited line re-voiced once
    for line_id, text in original_text_by_id.items():
        if line_id == victim_id:
            continue
        assert narration_calls.count(text) == 1               # unchanged lines never re-paid
    assert tts_after == tts_before + 1   # resume re-voices only the edited line
