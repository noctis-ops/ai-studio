"""Tests for the Phase 3 mix/master/QC chain (PHASE3 §H9-H12; test IDs
T-ENV-01, T-DUCK-01/02, T-STEM-01, T-MIX-03/04, T-QCA-01).

T-MIX-01/02 (stereo 24-bit master FLAC, exact length, -14 +/- 1 LUFS, true peak
<= -1.0 dBTP) live in ``test_audio_plan.py::test_sandbox_run_persists_audio_cues_and_qc``
— the full sandbox pipeline is the only meaningful subject for those.

FFmpeg-dependent tests carry @pytest.mark.ffmpeg and are skipped when
ffmpeg/ffprobe are not on PATH (see conftest).
"""

from __future__ import annotations

import asyncio
import itertools
import math
import wave
from array import array
from pathlib import Path

import pytest
from test_engine import _drain, _engine, _env, _seed_run, _worker

from studio.audio.ducking import (
    duck_envelope_points,
    merge_spans,
    ms_to_samples,
    speech_spans,
)
from studio.audio.envelope import build_gain_expr, depth_of_expression
from studio.audio.mastering import master_two_pass
from studio.audio.mixer import CueRender, DuckSpec, render_premaster
from studio.audio.stem import build_narration_stem
from studio.audio.timeline import LinePlacement, Timeline
from studio.domain.errors import ConfigError
from studio.video.ffmpeg import run_ffmpeg

SAMPLE_RATE = 48000
FPS = 30


def _wav_bytes(freq_hz: float, seconds: float, amplitude: float) -> array:
    samples = array("h")
    n = int(seconds * SAMPLE_RATE)
    for i in range(n):
        samples.append(int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE)))
    return samples


def _write_mono_wav(path: Path, freq_hz: float, seconds: float, amplitude: float) -> None:
    samples = _wav_bytes(freq_hz, seconds, amplitude)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.tobytes())


def _write_silence_wav(path: Path, seconds: float) -> None:
    n = int(seconds * SAMPLE_RATE)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * n)


def _read_s16_mono(path: Path) -> list[int]:
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    ext = path.suffix.lower().lstrip(".")
    hint = ["-f", ext] if ext else []
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [ffmpeg, "-hide_banner", "-v", "error", *hint, "-i", str(path),
         "-ac", "1", "-f", "s16le", "pipe:1"],
        check=True, capture_output=True,
    )
    samples = array("h")
    samples.frombytes(proc.stdout)
    return list(samples)


async def _rms_series(path: Path, window_ms: int = 100) -> list[float]:
    from studio.audio.analysis import rms_series_db

    samples = _read_s16_mono(path)
    return rms_series_db(samples, sample_rate=SAMPLE_RATE, window_ms=window_ms)


def _envelope_db_at(points: list[tuple[float, float]], t: float) -> float:
    """Reference piecewise-linear interpolation of the envelope (independent of
    the expression-tree code path)."""
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    for (t0, db0), (t1, db1) in itertools.pairwise(points):
        if t0 <= t <= t1:
            frac = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return db0 + (db1 - db0) * frac
    raise AssertionError("unreachable")


def _duck_spec(mode: str) -> DuckSpec:
    return DuckSpec(
        mode=mode,
        depth_db=10.0,
        pre_ms=150,
        attack_ms=100,
        release_ms=200,
        merge_gap_ms=700,
        ambience_fraction=0.5,
        sidechain_threshold=0.05,
        sidechain_ratio=8.0,
        sidechain_attack_ms=20.0,
        sidechain_release_ms=350.0,
        sidechain_makeup=1.0,
    )


# ======================================================================
# Unit: envelope tree, ducking math
# ======================================================================

def test_envelope_expression_depth_is_logarithmic():
    points = [(i * 0.01, float(-i % 17)) for i in range(2000)]
    assert depth_of_expression(points) <= 16  # ceil(log2(1999)) + 2 = 13


def test_envelope_constant_outside_point_range():
    expr = build_gain_expr([(2.0, -6.0), (4.0, 0.0)], sample_rate=SAMPLE_RATE)
    assert expr.startswith("if(lt(t,2),-6,")
    assert "if(gt(t,4),0,lerp(-6,0,(t-2)/2)))" in expr


def test_envelope_single_point_is_constant():
    expr = build_gain_expr([(1.0, -3.0), (5.0, -3.0)], sample_rate=SAMPLE_RATE)
    # consecutive-equal dB collapses to a constant gain
    assert "lerp" not in expr


def test_ms_to_samples_rounds_half_up():
    assert ms_to_samples(120.5, SAMPLE_RATE) == 5784
    assert ms_to_samples(5, 44100) == 221  # 220.5 -> half up
    assert ms_to_samples(0, SAMPLE_RATE) == 0


def test_speech_spans_merges_short_gaps():
    placements = [(0, 4800), (4800 + 2400, 4800)]  # 50 ms gap
    merged = speech_spans(placements, merge_gap_ms=200, sample_rate=SAMPLE_RATE)
    assert merged == [(0, 2 * 4800 + 2400)]


def test_speech_spans_keeps_long_gaps_separate():
    placements = [(0, 4800), (4800 + 48000, 4800)]
    merged = speech_spans(placements, merge_gap_ms=200, sample_rate=SAMPLE_RATE)
    assert merged == [(0, 4800), (48000 + 4800, 48000 + 4800 + 4800)]


def test_merge_spans_overlapping():
    assert merge_spans([(0, 10), (5, 20), (30, 40)], 5) == [(0, 20), (30, 40)]


def test_duck_envelope_points_shape():
    points = duck_envelope_points(
        [(96000, 96000)], depth_db=8.0, pre_ms=150, attack_ms=100,
        release_ms=200, ambience_fraction=1.0, sample_rate=SAMPLE_RATE,
    )
    times = [t for t, _ in points]
    assert times == sorted(times)
    assert (max(0.0, 96000 / SAMPLE_RATE - 0.150), 0.0) in points  # pre point
    assert (2.0, -8.0) in points  # hold point at span end


def test_duck_envelope_scales_ambience_fraction():
    points = duck_envelope_points(
        [(96000, 96000)], depth_db=8.0, pre_ms=150, attack_ms=100,
        release_ms=200, ambience_fraction=0.5, sample_rate=SAMPLE_RATE,
    )
    assert (2.0, -4.0) in points


def test_duck_envelope_rejects_attack_above_pre():
    with pytest.raises(ConfigError):
        duck_envelope_points(
            [(0, 4800)], depth_db=8.0, pre_ms=100, attack_ms=150,
            release_ms=100, sample_rate=SAMPLE_RATE,
        )


def test_duck_envelope_merges_release_attack_keeping_deeper_gain():
    # two speech spans 100 ms apart: first release meets second pre-attack
    points = duck_envelope_points(
        [(0, 9600), (9600 + 4800, 9600)],
        depth_db=10.0, pre_ms=50, attack_ms=25, release_ms=500,
        sample_rate=SAMPLE_RATE,
    )
    # the release of span 1 overlaps the pre-attack of span 2: the merged
    # envelope stays at or below the ducked gain throughout the gap
    t = 0.2
    while t <= 0.75:
        assert _envelope_db_at(points, t) <= -10.0 + 1e-9
        t += 0.05


# ======================================================================
# T-STEM-01
# ======================================================================

def test_t_stem_01_places_line_samples_byte_exact(tmp_path):
    line_a = tmp_path / "a.wav"
    line_b = tmp_path / "b.wav"
    _write_mono_wav(line_a, 440, 100 / SAMPLE_RATE, 0.5)
    _write_mono_wav(line_b, 550, 200 / SAMPLE_RATE, 0.7)
    timeline = Timeline(
        fps=FPS, sample_rate=SAMPLE_RATE,
        shots=(), scenes=(),
        lines=(
            LinePlacement("L2", "S", start_sample=5000, length_samples=200,
                          conformed_sha256="sha_b"),
            LinePlacement("L1", "S", start_sample=1000, length_samples=100,
                          conformed_sha256="sha_a"),
        ),
        total_frames=1, total_samples=9600, timeline_hash="hash",
    )
    out = tmp_path / "stem.wav"
    total = build_narration_stem(timeline, {"sha_a": str(line_a), "sha_b": str(line_b)},
                                 str(out))
    assert total == 9600
    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == SAMPLE_RATE
        frames = handle.readframes(handle.getnframes())
    assert len(frames) == 9600 * 2

    with wave.open(str(line_a), "rb") as handle:
        a_bytes = handle.readframes(handle.getnframes())
    with wave.open(str(line_b), "rb") as handle:
        b_bytes = handle.readframes(handle.getnframes())
    assert frames[1000 * 2:(1000 + 100) * 2] == a_bytes
    assert frames[5000 * 2:(5000 + 200) * 2] == b_bytes
    zeroed = (
        frames[: 1000 * 2]
        + frames[(1000 + 100) * 2: 5000 * 2]
        + frames[(5000 + 200) * 2:]
    )
    assert set(zeroed) == {0}


# ======================================================================
# T-ENV-01 (ffmpeg)
# ======================================================================

@pytest.mark.ffmpeg
def test_t_env_01_envelope_2000_points_matches_reference_within_0_3db(tmp_path):
    seconds = 10.0
    points = [
        (0.5 + i * (9.0 / 1999),
         -30.0 * (0.5 + i * (9.0 / 1999)) / seconds
         + 0.5 * math.sin(2 * math.pi * 1.5 * (0.5 + i * (9.0 / 1999))))
        for i in range(2000)
    ]
    assert depth_of_expression(points) < 32

    sine = tmp_path / "tone.wav"
    out = tmp_path / "shaped.wav"
    _write_mono_wav(sine, 440, seconds, 0.8)

    expr = build_gain_expr(points, sample_rate=SAMPLE_RATE)
    graph = f"[0:a]asetnsamples=n=480:p=0,volume='pow(10,({expr})/20)':eval=frame[out]"
    graph_path = tmp_path / "graph.txt"
    graph_path.write_text(graph + ";\n", encoding="utf-8")
    import asyncio as _asyncio

    import studio.video.ffmpeg as ffmpeg_mod

    version = _asyncio.run(ffmpeg_mod.ffmpeg_version())
    if version >= (7, 0, 0):
        script_args = ["-/filter_complex", str(graph_path)]
    else:
        script_args = ["-filter_complex_script", str(graph_path)]

    async def _run():
        await run_ffmpeg(
            ["-hide_banner", "-v", "error", "-y", "-f", "wav", "-i", str(sine),
             *script_args, "-map", "[out]", "-c:a", "pcm_s16le", str(out)],
            timeout_s=300.0,
        )

    _asyncio.run(_run())
    samples = _read_s16_mono(out)
    window = SAMPLE_RATE // 10
    assert len(samples) >= int(seconds * SAMPLE_RATE) - window
    for k in range(5, 95):
        chunk = samples[k * window:(k + 1) * window]
        assert chunk, "window out of range"
        mean = sum(x * x for x in chunk) / len(chunk)
        measured = 10 * math.log10(mean) - 20 * math.log10(32768) if mean > 0 else -math.inf
        base = 10 * math.log10(sum(x * x for x in _wav_bytes(440, 0.1, 0.8)) / window) \
            - 20 * math.log10(32768)
        expected = base + _envelope_db_at(points, (k + 0.5) * 0.1)
        assert abs(measured - expected) <= 0.3


# ======================================================================
# T-DUCK-01 / T-DUCK-02 (ffmpeg)
# ======================================================================

def _write_modulated_narration(path: Path, seconds: float, line_from: float, line_to: float) -> None:
    """Silence with a 3 Hz-pulsed 440 Hz tone burst where the line sits."""
    n = int(seconds * SAMPLE_RATE)
    buf = array("h", [0] * n)
    for i in range(int(line_from * SAMPLE_RATE), int(line_to * SAMPLE_RATE)):
        t = i / SAMPLE_RATE
        env = 0.5 * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * t))
        buf[i] = int(env * 32767 * math.sin(2 * math.pi * 440 * t))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(buf.tobytes())


def _duck_render(tmp_path: Path, *, mode: str, placements: list[tuple[int, int]]):
    total_samples = 6 * SAMPLE_RATE
    stem = tmp_path / "stem.wav"
    music = tmp_path / "music.wav"
    _write_modulated_narration(stem, 6.0, 2.0, 4.0)
    _write_mono_wav(music, 220, 8.0, 0.15)

    cues = [CueRender(
        path=str(music), bus="music", loop=False,
        start_frame=0, end_frame=total_samples * FPS // SAMPLE_RATE,
        source_offset_ms=0, gain_db=0.0, envelope=None,
        fade_in_ms=0, fade_out_ms=0,
    )]

    async def _do():
        return await render_premaster(
            stem_path=stem, cues=cues, duck=_duck_spec(mode), placements=placements,
            total_samples=total_samples, fps=FPS, sample_rate=SAMPLE_RATE,
            work_dir=tmp_path / "out", emit_music_bus_stems=True,
        )

    return asyncio.run(_do())


@pytest.mark.ffmpeg
def test_t_duck_01_envelope_mode_attenuates_speech_windows(tmp_path):
    placements = [(2 * SAMPLE_RATE, 2 * SAMPLE_RATE)]
    result = _duck_render(tmp_path, mode="envelope", placements=placements)
    pre = asyncio.run(_rms_series(result.music_pre_duck))
    post = asyncio.run(_rms_series(result.music_post_duck))

    # speech windows excluding the first 150 ms of the line (onset exclusion)
    speech = [i for i in range(22, 40)]
    diffs = [pre[i] - post[i] for i in speech]
    assert sum(diffs) / len(diffs) >= 8.0

    # outside the duck (well before the pre-attack and after the release):
    # within 1 dB of the unducked bus
    for i in list(range(0, 16)) + list(range(47, 60)):
        assert abs(pre[i] - post[i]) <= 1.0


@pytest.mark.ffmpeg
def test_t_duck_01_short_gap_stays_ducked(tmp_path):
    # two lines 0.5 s apart (< merge_gap_ms 700): the gap stays ducked
    placements = [
        (2 * SAMPLE_RATE, 9600),
        (2 * SAMPLE_RATE + 9600 + int(0.5 * SAMPLE_RATE), 9600),
    ]
    result = _duck_render(tmp_path, mode="envelope", placements=placements)
    pre = asyncio.run(_rms_series(result.music_pre_duck))
    post = asyncio.run(_rms_series(result.music_post_duck))
    gap_start = (2 * SAMPLE_RATE + 9600) // (SAMPLE_RATE // 10)
    gap_end = (2 * SAMPLE_RATE + 9600 + int(0.5 * SAMPLE_RATE)) // (SAMPLE_RATE // 10)
    assert gap_end - gap_start >= 2
    diffs = [pre[i] - post[i] for i in range(gap_start, gap_end)]
    assert sum(diffs) / len(diffs) >= 8.0


@pytest.mark.ffmpeg
def test_t_duck_02_sidechain_mode_attenuates_speech_windows(tmp_path):
    placements = [(2 * SAMPLE_RATE, 2 * SAMPLE_RATE)]
    result = _duck_render(tmp_path, mode="sidechain", placements=placements)
    pre = asyncio.run(_rms_series(result.music_pre_duck))
    post = asyncio.run(_rms_series(result.music_post_duck))
    speech = [i for i in range(22, 40)]
    diffs = [pre[i] - post[i] for i in speech
             if pre[i] != -math.inf and post[i] != -math.inf]
    assert sum(diffs) / len(diffs) >= 8.0


# ======================================================================
# T-MIX-03 (resume semantics) — full sandbox pipeline
# ======================================================================

async def _drain_pipeline(tmp_path, monkeypatch, *, filename):
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename=filename, paid=False,
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16",), auto_approve=True,
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)
    return config, db, clock, ids, repos, run_row


def _master_rows(db):
    return db.connect().execute(
        "SELECT * FROM audio_masters ORDER BY created_at"
    ).fetchall()



@pytest.mark.ffmpeg
async def test_t_mix_03_resume_with_unchanged_inputs_reuses_same_master(
    tmp_path, monkeypatch,
):
    config, db, clock, ids, repos, run_row = await _drain_pipeline(
        tmp_path, monkeypatch, filename="mix03a",
    )
    assert repos.pipeline.get_run(run_row.id).status == "completed"
    assert len(_master_rows(db)) == 1
    calls_before = db.connect().execute("SELECT count(*) FROM provider_calls").fetchone()[0]

    # resume: reopen the run and advance again — nothing may re-render
    db.connect().execute(
        "UPDATE pipeline_runs SET status = 'running' WHERE id = ?", (run_row.id,)
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    engine.advance_run(run_row.id)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)

    rows = _master_rows(db)
    assert len(rows) == 1
    calls_after = db.connect().execute("SELECT count(*) FROM provider_calls").fetchone()[0]
    assert calls_after == calls_before


@pytest.mark.ffmpeg
async def test_t_mix_03_changed_cue_supersedes_master(tmp_path, monkeypatch):
    config, db, clock, ids, repos, run_row = await _drain_pipeline(
        tmp_path, monkeypatch, filename="mix03b",
    )
    assert repos.pipeline.get_run(run_row.id).status == "completed"
    before = _master_rows(db)
    assert len(before) == 1
    original_hash = before[0]["input_hash"]

    # change one input: a cue's gain (timeline/plan untouched — S7 does not re-run)
    db.connect().execute(
        "UPDATE audio_cues SET gain_db = gain_db - 3.0 WHERE kind = 'music'",
    )
    db.connect().execute(
        "UPDATE pipeline_runs SET status = 'running' WHERE id = ?", (run_row.id,)
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    engine.advance_run(run_row.id)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)

    rows = _master_rows(db)
    assert len(rows) == 2
    ready = [row for row in rows if row["status"] == "ready"]
    superseded = [row for row in rows if row["status"] == "superseded"]
    assert len(ready) == 1 and len(superseded) == 1
    assert ready[0]["input_hash"] != original_hash
    assert superseded[0]["input_hash"] == original_hash


# ======================================================================
# T-MIX-04 (>40k-character graph renders from the filtergraph file)
# ======================================================================

@pytest.mark.ffmpeg
def test_t_mix_04_oversized_graph_renders_via_filtergraph_file(tmp_path):
    total_samples = 12 * SAMPLE_RATE

    stem = tmp_path / "stem.wav"
    _write_silence_wav(stem, 12.0)

    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir()
    sfx_paths = []
    for i in range(300):
        path = sfx_dir / f"sfx_{i:03d}.wav"
        _write_mono_wav(path, 600 + (i % 40) * 25, 0.1, 0.1)
        sfx_paths.append(str(path))

    cues = [
        CueRender(
            path=sfx_paths[i], bus="sfx", loop=False,
            start_frame=i, end_frame=i + 3,
            source_offset_ms=0, gain_db=-6.0, envelope=None,
            fade_in_ms=0, fade_out_ms=0,
        )
        for i in range(300)
    ]
    placements = [(i * 2000, 480) for i in range(150)]  # 150 duck spans

    async def _do():
        duck = _duck_spec("envelope")
        duck = DuckSpec(
            mode=duck.mode, depth_db=duck.depth_db, pre_ms=duck.pre_ms,
            attack_ms=duck.attack_ms, release_ms=duck.release_ms,
            merge_gap_ms=0.0, ambience_fraction=duck.ambience_fraction,
            sidechain_threshold=duck.sidechain_threshold,
            sidechain_ratio=duck.sidechain_ratio,
            sidechain_attack_ms=duck.sidechain_attack_ms,
            sidechain_release_ms=duck.sidechain_release_ms,
            sidechain_makeup=duck.sidechain_makeup,
        )
        return await render_premaster(
            stem_path=stem, cues=cues, duck=duck, placements=placements,
            total_samples=total_samples, fps=FPS, sample_rate=SAMPLE_RATE,
            work_dir=tmp_path / "out", emit_music_bus_stems=False,
        )

    result = asyncio.run(_do())
    graph_size = result.graph_path.stat().st_size
    assert graph_size > 40_000, f"graph unexpectedly small: {graph_size} bytes"
    samples = _read_s16_mono(result.premaster_path)
    assert len(samples) == total_samples


# ======================================================================
# T-QCA-01 (each QC check fails on a purpose-built bad input)
# ======================================================================

async def _lavfi_audio(tmp_path: Path, name: str, args: list[str]) -> Path:
    out = tmp_path / name
    await run_ffmpeg(
        ["-hide_banner", "-v", "error", "-y", *args, str(out)],
        timeout_s=120.0,
    )
    return out


def _seeded_deliverable(repos, *, slug: str) -> str:
    import json as _json

    series = repos.content.create_series(name="QC", slug=slug)
    episode = repos.content.create_episode(
        series_id=series.id, title="QC", concept="qc",
        languages_json=_json.dumps(["ar"]), aspects_json=_json.dumps(["9:16"]),
    )
    track = repos.content.create_track(episode_id=episode.id, aspect="9:16")
    return repos.content.create_deliverable(
        track_id=track.id, language="ar", channel_id="yt",
    ).id


def _qc_inputs(tmp_path: Path, **overrides):
    stem = tmp_path / "stem.wav"
    if not stem.exists():
        _write_mono_wav(stem, 440, 2.0, 0.3)
    music_pre = tmp_path / "pre.flac"
    music_post = tmp_path / "post.flac"
    base = dict(
        deliverable_id="qc-fixture",
        master_path=str(tmp_path / "master.flac"),
        narration_stem_path=str(stem),
        total_samples=2 * SAMPLE_RATE,
        sample_rate=SAMPLE_RATE,
        placements=[(0, 2 * SAMPLE_RATE)],
        cues=[{"kind": "music", "library_sha256": "sha-music",
               "start_frame": 0, "end_frame": 60}],
        library={"sha-music": {"title": "Tone", "license": "CC0",
                               "attribution_required": False}},
        fps=FPS,
        music_pre_path=str(music_pre),
        music_post_path=str(music_post),
        no_mood_match_warn=False,
    )
    base.update(overrides)
    from studio.qc.audio import AudioQcInputs

    return AudioQcInputs(**base)   # type: ignore[arg-type]


def _qc_spec():
    from studio.qc.audio import QcSpec

    return QcSpec(
        min_duck_db=8.0, min_speech_to_music_db=10.0,
        silent_line_rms_db=-45.0, window_ms=100, exclude_onset_ms=150,
        target_lufs=-14.0, lufs_tolerance=1.0,
        tp_ceiling_db=-1.0, clip_peak_db=-0.1,
    )


async def _run_and_persist(repos, inputs):
    from studio.qc.audio import qc_rows, run_audio_qc

    results = await run_audio_qc(inputs, _qc_spec())
    for row in qc_rows(inputs.deliverable_id, results):
        repos.audio.add_qc_result(**row)
    persisted = repos.audio.list_qc_results(deliverable_id=inputs.deliverable_id)
    return results, {row.check_name: row.status for row in persisted}


@pytest.mark.ffmpeg
async def test_t_qca_01_unreadable_or_empty(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc1")
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc1"),
        master_path=str(tmp_path / "does-not-exist.flac"),
    )
    results, persisted = await _run_and_persist(repos, inputs)
    assert results[0].name == "unreadable_or_empty"
    assert results[0].status == "fail"
    assert persisted["unreadable_or_empty"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_exact_length(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc2")
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=1.0", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc2"),
        master_path=str(master),
        placements=[],
        cues=[],
        music_pre_path=None,
        music_post_path=None,
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["exact_length"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_narration_presence(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc3")
    # master below the silent_line_rms_db threshold across the placement window
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0",
         "-af", "volume=0.002", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc3"),
        master_path=str(master),
        music_pre_path=None,
        music_post_path=None,
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["narration_presence"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_loudness(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc4")
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0",
         "-af", "volume=0.002", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc4"),
        master_path=str(master),
        placements=[],
        cues=[],
        music_pre_path=None,
        music_post_path=None,
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["loudness"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_true_peak_and_clip_peak(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc5")
    # over-driven sine: flat-tops near 0 dBFS
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0",
         "-af", "volume=20dB", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc5"),
        master_path=str(master),
        placements=[],
        cues=[],
        music_pre_path=None,
        music_post_path=None,
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["true_peak"] == "fail"
    assert persisted["clip_peak"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_duck_depth(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc6")
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0", "-c:a", "flac"],
    )
    pre = await _lavfi_audio(
        tmp_path, "pre.flac",
        ["-f", "lavfi", "-i", "sine=frequency=220:duration=2.0",
         "-af", "volume=0.3", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc6"),
        master_path=str(master),
        music_pre_path=str(pre),
        music_post_path=str(pre),  # identical: no ducking at all
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["duck_depth"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_speech_to_music(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc7")
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0", "-c:a", "flac"],
    )
    pre = await _lavfi_audio(
        tmp_path, "pre.flac",
        ["-f", "lavfi", "-i", "sine=frequency=220:duration=2.0",
         "-af", "volume=0.3", "-c:a", "flac"],
    )
    # post-duck music exactly -10 dB under pre: duck passes, ratio fails (stem is quiet)
    post = await _lavfi_audio(
        tmp_path, "post.flac",
        ["-f", "lavfi", "-i", "sine=frequency=220:duration=2.0",
         "-af", "volume=0.0948", "-c:a", "flac"],
    )
    quiet_stem = tmp_path / "quiet_stem.wav"
    _write_mono_wav(quiet_stem, 440, 2.0, 0.001)
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc7"),
        master_path=str(master),
        narration_stem_path=str(quiet_stem),
        music_pre_path=str(pre),
        music_post_path=str(post),
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["duck_depth"] == "pass"
    assert persisted["speech_to_music"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_plan_coverage_beyond_timeline(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc8")
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc8"),
        master_path=str(master),
        cues=[{"kind": "music", "library_sha256": "sha-music",
               "start_frame": 0, "end_frame": 99999}],
        music_pre_path=None,
        music_post_path=None,
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["plan_coverage"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_plan_coverage_empty_plan(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc9")
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc9"),
        master_path=str(master),
        cues=[],
        music_pre_path=None,
        music_post_path=None,
        no_mood_match_warn=False,
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["plan_coverage"] == "fail"


@pytest.mark.ffmpeg
async def test_t_qca_01_license_completeness(tmp_path, monkeypatch):
    _config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="qc10")
    master = await _lavfi_audio(
        tmp_path, "master.flac",
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2.0", "-c:a", "flac"],
    )
    inputs = _qc_inputs(
        tmp_path,
        deliverable_id=_seeded_deliverable(repos, slug="qc10"),
        master_path=str(master),
        library={},  # the cue's library row is gone
        music_pre_path=None,
        music_post_path=None,
    )
    _results, persisted = await _run_and_persist(repos, inputs)
    assert persisted["license_completeness"] == "fail"


# ======================================================================
# T-ENG-13 (crash in S7b resumes with zero TTS repaid)
# ======================================================================

@pytest.mark.ffmpeg
async def test_t_eng_13_crash_in_s7b_resumes_with_zero_tts_repaid(tmp_path, monkeypatch):
    from datetime import timedelta

    import studio.pipeline.stages_audio as stages_audio
    from studio.core.faults import SimulatedCrash

    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="eng13", paid=False,
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16",), auto_approve=True,
    )

    original = stages_audio.render_premaster
    state = {"crashed": False}

    async def crasher(*args, **kwargs):
        if not state["crashed"]:
            state["crashed"] = True
            raise SimulatedCrash("crash inside s7b_audio_mix")
        return await original(*args, **kwargs)

    monkeypatch.setattr(stages_audio, "render_premaster", crasher)

    engine, _dag = _engine(db, repos, config, clock, ids)
    worker1 = _worker(db, engine, repos, config, clock, ids)
    with pytest.raises(SimulatedCrash):
        await worker1.run(until_idle=True)

    tts_before = db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()[0]
    tts_stages_before = db.connect().execute(
        "SELECT count(*) FROM stage_runs WHERE stage = 's5_tts' AND status = 'succeeded'"
    ).fetchone()[0]
    llm_before = db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'llm'"
    ).fetchone()[0]
    assert tts_before > 0  # sandbox paid for its narration before the crash

    # worker restarts past the staleness window on fresh handles
    clock.advance(timedelta(seconds=3600))
    engine2, _dag2 = _engine(db, repos, config, clock, ids)
    worker2 = _worker(db, engine2, repos, config, clock, ids)
    await worker2.run(until_idle=True)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == "completed"

    tts_after = db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'tts'"
    ).fetchone()[0]
    llm_after = db.connect().execute(
        "SELECT count(*) FROM provider_calls WHERE capability = 'llm'"
    ).fetchone()[0]
    assert tts_after == tts_before  # zero TTS re-paid
    assert llm_after == llm_before  # zero LLM re-paid

    succeeded_tts_stages = db.connect().execute(
        "SELECT count(*) FROM stage_runs WHERE stage = 's5_tts' AND status = 'succeeded'"
    ).fetchone()[0]
    assert succeeded_tts_stages == tts_stages_before  # zero line stages re-executed
    assert tts_stages_before > 0


@pytest.mark.ffmpeg
async def test_t_mix_01_and_02_sandbox_master_row_flacs_and_loudness(tmp_path, monkeypatch):
    """T-MIX-01/02: exactly total_samples, stereo master FLAC, audio_masters row,
    -14 +/- 1 LUFS integrated, true peak <= -1.0 dBTP, no clipping."""
    from studio.audio.analysis import astats_levels
    from studio.audio.mastering import integrated_loudness
    from studio.core.cas import ContentStore
    from studio.video.probe import probe

    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="mix0102", paid=False,
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16",), auto_approve=True,
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)
    assert repos.pipeline.get_run(run_row.id).status == "completed"

    rows = db.connect().execute("SELECT * FROM audio_masters").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ready"
    total = int(row["total_samples"])
    assert total > 0

    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    master_path = Path(cas.resolve(row["master_sha256"]))
    assert master_path.suffix == ".flac"

    # stereo, and exactly total_samples frames (mono downmix keeps frame count)
    info = probe(master_path)
    assert info.audio_channels == 2
    assert len(_read_s16_mono(master_path)) == total

    summary = await integrated_loudness(str(master_path))
    assert abs(summary.integrated_lufs - (-14.0)) <= 1.0
    assert summary.true_peak_dbtp is not None and summary.true_peak_dbtp <= -1.0

    levels = await astats_levels(str(master_path))
    assert levels.get("peak_db") is not None and levels["peak_db"] <= -0.1


# ======================================================================
# T-ENG-12 (sandbox ar vs ar+en: TTS and mix counts double, images/videos
# unchanged, video timelines identical)
# ======================================================================


def _render_sandbox(tmp_path, monkeypatch, *, languages, aspects, filename):
    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename=filename, paid=False,
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=languages, aspects=aspects, auto_approve=True,
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)

    import asyncio

    async def _go():
        await worker.run(until_idle=True)
        while True:
            pending = db.connect().execute(
                "SELECT id FROM approval_gates WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            if not pending:
                break
            for gate in pending:
                repos.pipeline.approve_gate(gate[0], decided_by="test")
            await worker.run(until_idle=True)

    asyncio.run(_go())
    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == "completed"

    def one(capability):
        return db.connect().execute(
            "SELECT count(*) FROM provider_calls WHERE capability = ?", (capability,),
        ).fetchone()[0]

    masters = db.connect().execute(
        "SELECT count(*) FROM audio_masters WHERE status = 'ready'"
    ).fetchone()[0]
    shots = [
        r[0]
        for r in db.connect().execute(
            "SELECT shot_from || ':' || shot_to FROM scenes ORDER BY scene_no"
        ).fetchall()
    ]
    lines = db.connect().execute(
        "SELECT count(*) FROM narration_lines WHERE duration_ms IS NOT NULL"
    ).fetchone()[0]
    return {
        "tts": one("tts"),
        "image": one("image_reference_generate"),
        "video": one("image_to_video"),
        "llm": one("llm"),
        "masters": masters,
        "voiced_lines": lines,
        "shots": shots,
    }


@pytest.mark.ffmpeg
def test_t_eng_12_sandbox_ar_vs_ar_en_counts(tmp_path, monkeypatch):
    single = _render_sandbox(
        tmp_path, monkeypatch, languages=("ar",), aspects=("9:16",), filename="eng12a",
    )
    double = _render_sandbox(
        tmp_path, monkeypatch, languages=("ar", "en"), aspects=("9:16",), filename="eng12b",
    )
    assert single["voiced_lines"] > 0
    assert double["tts"] == 2 * single["tts"]
    assert double["voiced_lines"] == 2 * single["voiced_lines"]
    assert double["masters"] == 2 * single["masters"]
    assert double["image"] == single["image"]
    assert double["video"] == single["video"]
    assert double["shots"] == single["shots"]
    assert double["llm"] == single["llm"]


# ======================================================================
# T-MIX-05 (slow): a synthetic 20-minute timeline (300 lines, 40 scenes)
# mixes and passes QC. Excluded from default CI (-m "not slow"); run on
# demand with `pytest -m slow`.
# ======================================================================


@pytest.mark.slow
@pytest.mark.ffmpeg
async def test_t_mix_05_synthetic_20_minute_timeline_mixes_and_passes_qc(
    tmp_path,
):
    import json
    import uuid

    from studio.qc.audio import AudioQcInputs, QcSpec, run_audio_qc

    fps = 24
    rate = SAMPLE_RATE
    total_samples = 20 * 60 * rate            # 57,600,000
    total_frames = total_samples // (rate // fps)
    scene_frames = total_frames // 40         # 30 s each

    # ---- 300 line WAVs, one every 4 s, each 2 s long
    line_dir = tmp_path / "lines"
    line_dir.mkdir()
    placements = []
    timeline_lines = []
    cas_paths: dict[str, str] = {}
    for i in range(300):
        freq = 220.0 + (i % 12) * 30.0
        path = line_dir / f"line_{i:03d}.wav"
        _write_mono_wav(path, freq, 2.0, 0.35)
        start = i * 4 * rate
        placements.append((start, 2 * rate))
        sha = f"sha{i:03d}"
        cas_paths[sha] = str(path)
        timeline_lines.append(
            LinePlacement(
                f"L{i + 1}", "S", start_sample=start, length_samples=2 * rate,
                conformed_sha256=sha,
            )
        )

    timeline = Timeline(
        fps=fps, sample_rate=rate,
        shots=(), scenes=(),
        lines=tuple(timeline_lines),
        total_frames=total_frames, total_samples=total_samples,
        timeline_hash=uuid.uuid4().hex,
    )

    # ---- stem (stdlib wave; silences between lines)
    stem = tmp_path / "stem.wav"
    build_narration_stem(timeline, cas_paths, str(stem))
    assert stem.stat().st_size == 44 + total_samples * 2

    # ---- library assets: loopable music + ambience, shared paths
    media = tmp_path / "media"
    media.mkdir()
    music = media / "music.flac"
    music.write_bytes(b"")  # placeholder overwritten by ffmpeg below
    music_args = ["-f", "lavfi", "-i", "sine=frequency=196:duration=4",
                  "-c:a", "flac", "-y", str(music)]
    proc = await run_ffmpeg(["-hide_banner", "-v", "error", *music_args], timeout_s=60)
    del proc
    assert music.is_file() and music.stat().st_size > 0

    ambience = media / "ambience.flac"
    proc = await run_ffmpeg(
        ["-hide_banner", "-v", "error",
         "-f", "lavfi", "-i", "anoisesrc=d=4:c=brown:a=0.25",
         "-c:a", "flac", "-y", str(ambience)],
        timeout_s=60,
    )
    del proc
    sfx = media / "sfx.flac"
    proc = await run_ffmpeg(
        ["-hide_banner", "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=0.2",
         "-c:a", "flac", "-y", str(sfx)],
        timeout_s=60,
    )
    del proc

    # ---- 40 music cues (one per scene), 2 ambience beds, 40 sfx sprinkled
    cues = []
    library: dict[str, dict] = {}
    for i in range(40):
        sha = f"music{i:02d}"
        library[sha] = {"title": f"Scene {i + 1} tone", "license": "CC0",
                        "attribution_required": False}
        cues.append(CueRender(
            path=str(music), bus="music", loop=True,
            start_frame=i * scene_frames, end_frame=(i + 1) * scene_frames,
            source_offset_ms=0, gain_db=-16.0, envelope=None,
            fade_in_ms=200, fade_out_ms=200,
        ))
    library["amb00"] = {"title": "Room tone A", "license": "CC0",
                        "attribution_required": False}
    library["amb01"] = {"title": "Room tone B", "license": "CC0",
                        "attribution_required": False}
    cues.append(CueRender(
        path=str(ambience), bus="ambience", loop=True,
        start_frame=0, end_frame=total_frames,
        source_offset_ms=0, gain_db=-24.0, envelope=None,
        fade_in_ms=0, fade_out_ms=0,
    ))
    cues.append(CueRender(
        path=str(ambience), bus="ambience", loop=True,
        start_frame=0, end_frame=total_frames,
        source_offset_ms=1500, gain_db=-30.0, envelope=None,
        fade_in_ms=0, fade_out_ms=0,
    ))
    for i in range(40):
        sha = f"sfx{i:02d}"
        library[sha] = {"title": f"Tick {i + 1}", "license": "CC0",
                        "attribution_required": False}
        start = int((i + 1) * 28.5 * fps)
        cues.append(CueRender(
            path=str(sfx), bus="sfx", loop=False,
            start_frame=start, end_frame=start + 5,
            source_offset_ms=0, gain_db=-12.0, envelope=None,
            fade_in_ms=0, fade_out_ms=0,
        ))

    cue_dicts = []

    for i in range(40):
        cue_dicts.append({"kind": "music", "library_sha256": f"music{i:02d}",
                          "start_frame": i * scene_frames,
                          "end_frame": (i + 1) * scene_frames})
    cue_dicts.append({"kind": "ambience", "library_sha256": "amb00",
                      "start_frame": 0, "end_frame": total_frames})
    cue_dicts.append({"kind": "ambience", "library_sha256": "amb01",
                      "start_frame": 0, "end_frame": total_frames})
    for i in range(40):
        start = int((i + 1) * 28.5 * fps)
        cue_dicts.append({"kind": "sfx", "library_sha256": f"sfx{i:02d}",
                          "start_frame": start, "end_frame": start + 5})

    # ---- mix (premaster) then master (two-pass loudnorm)
    duck = _duck_spec("envelope")
    mix_dir = tmp_path / "mix"
    result = await render_premaster(
        stem_path=stem, cues=cues, duck=duck, placements=placements,
        total_samples=total_samples, fps=fps, sample_rate=rate,
        work_dir=mix_dir, emit_music_bus_stems=True,
    )
    master = tmp_path / "master.flac"
    await master_two_pass(
        result.premaster_path, master,
        target_lufs=-14.0, true_peak_db=-1.0, lra=11.0,
        lufs_tolerance=1.0, tp_ceiling_db=-1.0, clip_peak_db=-0.1,
        sample_rate=rate, total_samples=total_samples, timeout_s=1800.0,
    )

    # ---- QC over the 20-minute master
    inputs = AudioQcInputs(
        deliverable_id="t-mix-05",
        master_path=str(master),
        narration_stem_path=str(stem),
        total_samples=total_samples,
        sample_rate=rate,
        placements=placements,
        cues=cue_dicts,
        library=library,
        fps=fps,
        music_pre_path=str(mix_dir / "music_pre_duck.wav"),
        music_post_path=str(mix_dir / "music_post_duck.wav"),
    )
    spec = QcSpec(
        min_duck_db=8.0, min_speech_to_music_db=10.0,
        silent_line_rms_db=-45.0, window_ms=100, exclude_onset_ms=150,
        target_lufs=-14.0, lufs_tolerance=1.0,
        tp_ceiling_db=-1.0, clip_peak_db=-0.1,
    )
    results = await run_audio_qc(inputs, spec)
    failures = {r.name: r.detail for r in results if r.status == "fail"}
    assert not failures, f"QC failures: {json.dumps(failures, default=str)}"
