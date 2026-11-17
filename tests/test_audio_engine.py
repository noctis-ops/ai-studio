"""Tests for the Phase 3 audio engine modules: analysis, library, timeline,
conform and stem (PHASE3 §H3-H5, §H9; test IDs T-LIB-*, T-TL-*, T-CONF-01,
T-STEM-01).

FFmpeg-dependent tests carry @pytest.mark.ffmpeg and are skipped when
ffmpeg/ffprobe are not on PATH (see conftest).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from array import array
from pathlib import Path

import pytest
import yaml
from conftest import make_home

from studio.audio.analysis import (
    parse_astats,
    parse_ebur128_summary,
    parse_loudnorm_json,
    rms_dbfs,
    rms_series_db,
    s16_le_samples,
    sample_peak_dbfs,
)
from studio.audio.library import scan_library
from studio.audio.stem import build_narration_stem
from studio.audio.timeline import build_timeline
from studio.config.loader import load_all
from studio.core.clock import FakeClock
from studio.core.ids import SequentialIds
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.errors import ConfigError, MediaToolError, TimelineOverflowError


def _env(tmp_path, monkeypatch, *, filename="audio"):
    home = make_home(tmp_path, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = SequentialIds("audio")
    repos = build_repos(db, clock, ids)
    return config, db, clock, ids, repos, home


def _make_track(repos, *, aspect="9:16", languages=("ar",)):
    series = repos.content.create_series(name="S", slug="s")
    episode = repos.content.create_episode(
        series_id=series.id,
        title="E",
        concept="concept",
        languages_json=json.dumps(list(languages)),
        aspects_json=json.dumps([aspect]),
    )
    track = repos.content.create_track(episode_id=episode.id, aspect=aspect)
    return series, episode, track


def _cas(config, db, clock):
    from studio.core.cas import ContentStore

    return ContentStore(Path(config.settings.paths.cas), db, clock)


def _make_line(repos, deliverable_id, shot_id, *, line_no=1, conformed_samples=48000,
               cas=None, payload: bytes = b"conformed-audio"):
    assert cas is not None
    raw_ref = cas.put_bytes(b"raw-audio-" + payload, kind="audio", ext="mp3")
    conf_ref = cas.put_bytes(payload, kind="audio", ext="wav")
    line = repos.content.upsert_narration_line(
        deliverable_id=deliverable_id, shot_id=shot_id, line_no=line_no,
        text=f"line {line_no}", asset_sha256=raw_ref.sha256, duration_ms=1,
    )
    repos.content.mark_line_voiced(
        line.id, asset_sha256=raw_ref.sha256, duration_ms=1,
        conformed_sha256=conf_ref.sha256, conformed_samples=conformed_samples,
        voice_snapshot_json="{}",
    )
    return line, conf_ref.sha256


def _locked_shot(repos, track_id, shot_no, *, clip_duration_ms=5000, speed_factor=1.0,
                 hold_frames=0, min_duration_ms=None):
    shot = repos.content.create_shot(
        track_id=track_id, shot_no=shot_no, prompt=f"prompt {shot_no}",
        min_duration_ms=min_duration_ms,
    )
    repos.content.set_shot_timing(
        shot.id, clip_duration_ms=clip_duration_ms, speed_factor=speed_factor,
        hold_frames=hold_frames, timing_status="locked",
    )
    return shot


# ============================================================ analysis

def test_rms_dbfs_sine_and_silence():
    samples = array("h", (int(16000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(4800)))
    assert abs(rms_dbfs(samples) - (-9.25)) < 0.2  # peak -6.24 dBFS, RMS -9.25
    assert rms_dbfs(array("h", [0] * 1000)) == -math.inf
    assert sample_peak_dbfs(array("h", [0, 12345, -12345])) > -9.0
    assert sample_peak_dbfs(array("h", [0, 0])) == -math.inf


def test_rms_series_db_windowing():
    samples = array("h", [10000] * 100 + [0] * 100)
    series = rms_series_db(samples, sample_rate=1000, window_ms=100)
    assert len(series) == 2
    assert series[0] > series[1]


def test_parse_ebur128_summary_uses_last_block():
    stderr = (
        "[x] Integrated loudness:\n    I:         -99.0 LUFS\n"
        " some other noise\n"
        "  Integrated loudness:\n    I:         -14.2 LUFS\n    Threshold: -24.3 LUFS\n"
        "  Loudness range:\n    LRA:        7.1 LU\n"
        "  True peak:\n    Peak:       -1.5 dBTP\n"
    )
    summary = parse_ebur128_summary(stderr)
    assert summary.integrated_lufs == pytest.approx(-14.2)
    assert summary.lra_lu == pytest.approx(7.1)
    assert summary.true_peak_dbtp == pytest.approx(-1.5)


def test_parse_ebur128_summary_missing_raises():
    with pytest.raises(MediaToolError):
        parse_ebur128_summary("nothing useful here")


def test_parse_loudnorm_json_takes_last_block():
    stderr = 'garbage {"not": "it"} more {"input_i": "-20.0", "measured_LRA": "5.0"} trailing'
    block = parse_loudnorm_json(stderr)
    assert block["input_i"] == "-20.0"


def test_parse_loudnorm_json_missing_raises():
    with pytest.raises(MediaToolError):
        parse_loudnorm_json("no json at all")


def test_parse_astats():
    stderr = "[x] Peak level dB: -1.2\n[x] RMS level dB: -20.5\n"
    out = parse_astats(stderr)
    assert out["peak_db"] == pytest.approx(-1.2)
    assert out["rms_db"] == pytest.approx(-20.5)


def test_s16_le_samples_roundtrip():
    raw = array("h", [1, -2, 32767, -32768]).tobytes()
    assert list(s16_le_samples(raw)) == [1, -2, 32767, -32768]
    with pytest.raises(MediaToolError):
        s16_le_samples(b"\x01")


# ============================================================ library

def _sine(path: Path, *, duration=2.0, freq=1000, volume: float | None = None) -> None:
    graph = f"sine=frequency={freq}:duration={duration}"
    if volume is not None:
        graph += f",volume={volume}"
    subprocess.run(  # noqa: S603
        [shutil.which("ffmpeg") or "ffmpeg", "-nostats", "-hide_banner", "-y",
         "-f", "lavfi", "-i", graph,
         "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True,
    )


def _sidecar(kind_dir: Path, tracks: list[dict], defaults: dict | None = None) -> None:
    data: dict[str, object] = {"tracks": tracks}
    if defaults:
        data["defaults"] = defaults
    (kind_dir / "index.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_library_scan_registers_and_validates(tmp_path, monkeypatch):
    config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="lib1")
    root = tmp_path / "library"
    music = root / "music"
    music.mkdir(parents=True)
    ok = music / "ok.wav"
    no_license = music / "no_license.wav"
    bad_tag = music / "bad_tag.wav"
    _sine(ok, duration=2.0, freq=1000)
    _sine(no_license, duration=2.0, freq=1100)
    _sine(bad_tag, duration=2.0, freq=1200)
    _sidecar(music, [
        {"file": "ok.wav", "title": "OK", "moods": ["adventurous"], "intensity": 3,
         "bpm": 100, "loopable": True, "license": "CC-BY 4.0"},
        {"file": "no_license.wav", "title": "NoLicense", "moods": ["adventurous"],
         "intensity": 3, "license": ""},
        {"file": "bad_tag.wav", "title": "BadTag", "moods": ["not-a-mood"],
         "intensity": 3, "license": "CC-BY 4.0"},
    ])
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab,
        scan_extensions=tuple(config.settings.audio.scan_extensions),
    )
    active = repos.audio.list_library(include_inactive=False)
    assert [row.title for row in active] == ["OK"]
    inactive = {row.title: row for row in repos.audio.list_library(include_inactive=True)}
    assert inactive["NoLicense"].scan_error == "license missing"
    assert "unknown moods" in (inactive["BadTag"].scan_error or "")
    assert report.scanned >= 3


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_library_scan_idempotent_rename_and_delete(tmp_path, monkeypatch):
    config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="lib2")
    root = tmp_path / "library"
    music = root / "music"
    music.mkdir(parents=True)
    track = music / "song.wav"
    _sine(track, duration=2.0)
    _sidecar(music, [
        {"file": "song.wav", "title": "Song", "moods": ["hopeful"], "intensity": 2,
         "license": "CC0"},
    ])
    await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab,
        scan_extensions=tuple(config.settings.audio.scan_extensions),
    )
    first = repos.audio.list_library(include_inactive=True)
    assert len(first) == 1 and first[0].active == 1
    sha = first[0].sha256

    await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab,
        scan_extensions=tuple(config.settings.audio.scan_extensions),
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert len(rows) == 1  # idempotent

    # rename: same bytes must keep the row and update rel_path
    renamed = music / "renamed.wav"
    track.rename(renamed)
    _sidecar(music, [
        {"file": "renamed.wav", "title": "Song", "moods": ["hopeful"], "intensity": 2,
         "license": "CC0"},
    ])
    await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab,
        scan_extensions=tuple(config.settings.audio.scan_extensions),
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert len(rows) == 1
    assert rows[0].sha256 == sha
    assert rows[0].rel_path == "music/renamed.wav"
    assert rows[0].active == 1

    # delete: row stays, active=0, never removed
    renamed.unlink()
    await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab,
        scan_extensions=tuple(config.settings.audio.scan_extensions),
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert len(rows) == 1
    assert rows[0].active == 0
    assert rows[0].scan_error == "file missing"


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_library_loudness_within_half_lu(tmp_path, monkeypatch):
    config, _db, _clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename="lib3")
    root = tmp_path / "library"
    music = root / "music"
    music.mkdir(parents=True)
    track = music / "sine.wav"
    _sine(track, duration=5.0, freq=1000)
    _sidecar(music, [
        {"file": "sine.wav", "title": "Sine", "moods": ["awe"], "intensity": 4,
         "license": "CC-BY 4.0"},
    ])
    await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab,
        scan_extensions=tuple(config.settings.audio.scan_extensions),
    )
    row = repos.audio.list_library(include_inactive=True)[0]
    # ffmpeg lavfi sine has amplitude 1/8 -> RMS -21.07 LUFS at 1 kHz (K-weighting ~0)
    assert row.loudness_lufs is not None
    assert abs(row.loudness_lufs - (-21.07)) <= 0.5


# ============================================================ timeline

def _timeline_env(tmp_path, monkeypatch, *, filename="tl"):
    config, db, clock, _ids, repos, _home = _env(tmp_path, monkeypatch, filename=filename)
    _series, _episode, track = _make_track(repos)
    deliverable = repos.content.create_deliverable(
        track_id=track.id, language="ar", channel_id="yt"
    )
    return config, repos, track, deliverable, db, clock


def test_timeline_deterministic_and_spans_contiguous(tmp_path, monkeypatch):
    config, repos, track, deliverable, db, clock = _timeline_env(tmp_path, monkeypatch)
    cas = _cas(config, db, clock)
    for i in (1, 2, 3):
        shot = _locked_shot(repos, track.id, i, clip_duration_ms=5000)
        _make_line(repos, deliverable.id, shot.id, line_no=1,
                   conformed_samples=48000, cas=cas, payload=f"p{i}".encode())
    tl1 = build_timeline(repos.content, deliverable.id, config.settings)
    tl2 = build_timeline(repos.content, deliverable.id, config.settings)
    assert tl1.timeline_hash == tl2.timeline_hash
    assert [s.start_frame for s in tl1.shots] == [0, 120, 240]
    assert all(s.effective_frames == 120 for s in tl1.shots)
    assert tl1.total_frames == 360
    assert tl1.total_samples == 360 * 2000
    # lines are contiguous inside their shot: lead_in then line
    first = tl1.lines[0]
    assert first.start_sample == 150 * 48  # timing.lead_in_ms default 150 ms
    assert first.length_samples == 48000


def test_timeline_hash_changes_when_line_changes(tmp_path, monkeypatch):
    config, repos, track, deliverable, db, clock = _timeline_env(
        tmp_path, monkeypatch, filename="tl2"
    )
    cas = _cas(config, db, clock)
    shot = _locked_shot(repos, track.id, 1, clip_duration_ms=5000)
    _, _sha1 = _make_line(repos, deliverable.id, shot.id, line_no=1,
                         conformed_samples=48000, cas=cas, payload=b"one")
    tl1 = build_timeline(repos.content, deliverable.id, config.settings)
    # rewrite the line with different length -> hash must change
    line = repos.content.narration_lines(deliverable.id)[0]
    ref2 = cas.put_bytes(b"two", kind="audio", ext="wav")
    repos.content.mark_line_voiced(
        line.id, asset_sha256=ref2.sha256, duration_ms=1,
        conformed_sha256=ref2.sha256, conformed_samples=24000, voice_snapshot_json="{}",
    )
    tl2 = build_timeline(repos.content, deliverable.id, config.settings)
    assert tl1.timeline_hash != tl2.timeline_hash


def test_timeline_spans_identical_across_languages(tmp_path, monkeypatch):
    config, repos, track, deliverable, db, clock = _timeline_env(tmp_path, monkeypatch, filename="tl3")
    cas = _cas(config, db, clock)
    shot = _locked_shot(repos, track.id, 1, clip_duration_ms=5000)
    _make_line(repos, deliverable.id, shot.id, line_no=1, conformed_samples=48000,
               cas=cas, payload=b"ar-line")
    deliverable_en = repos.content.create_deliverable(
        track_id=track.id, language="en", channel_id="yt"
    )
    _make_line(repos, deliverable_en.id, shot.id, line_no=1, conformed_samples=96000,
               cas=cas, payload=b"en-line")
    tl_ar = build_timeline(repos.content, deliverable.id, config.settings)
    tl_en = build_timeline(repos.content, deliverable_en.id, config.settings)
    assert [s.start_frame for s in tl_ar.shots] == [s.start_frame for s in tl_en.shots]
    assert tl_ar.total_frames == tl_en.total_frames
    assert tl_ar.timeline_hash != tl_en.timeline_hash  # lines differ


def test_timeline_overflow_raises_exact_numbers(tmp_path, monkeypatch):
    config, repos, track, deliverable, db, clock = _timeline_env(tmp_path, monkeypatch, filename="tl4")
    cas = _cas(config, db, clock)
    shot = _locked_shot(repos, track.id, 1, clip_duration_ms=1000)  # 1 s = 48 000 samples
    need = 200_000
    _make_line(repos, deliverable.id, shot.id, line_no=1, conformed_samples=need,
               cas=cas, payload=b"big")
    with pytest.raises(TimelineOverflowError) as excinfo:
        build_timeline(repos.content, deliverable.id, config.settings)
    message = str(excinfo.value)
    assert "shot 1 overflows by" in message
    assert "ms" in message
    # nothing was truncated: the error, not a clamp
    lines = repos.content.narration_lines(deliverable.id)
    assert lines[0].conformed_samples == need


def test_timeline_fps_must_divide_sample_rate(tmp_path, monkeypatch):
    config, repos, track, deliverable, db, clock = _timeline_env(tmp_path, monkeypatch, filename="tl5")
    cas = _cas(config, db, clock)
    shot = _locked_shot(repos, track.id, 1, clip_duration_ms=1000)
    _make_line(repos, deliverable.id, shot.id, line_no=1, conformed_samples=100,
               cas=cas, payload=b"tiny")
    fps = 7  # 48000 % 7 != 0
    bad = config.settings.model_copy(
        update={"render": config.settings.render.model_copy(update={"fps": fps})}
    )
    with pytest.raises(ConfigError):
        build_timeline(repos.content, deliverable.id, bad)


# ============================================================ stem

def _write_wav(path: Path, samples, *, sample_rate=48000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(array("h", samples).tobytes())


def test_stem_bytes_at_placements_and_zero_elsewhere(tmp_path, monkeypatch):
    config, repos, track, deliverable, db, clock = _timeline_env(tmp_path, monkeypatch, filename="stem")
    sr = 48000
    cas = _cas(config, db, clock)
    shot = _locked_shot(repos, track.id, 1, clip_duration_ms=5000)  # 120 frames @24fps
    line_wav = tmp_path / "line.wav"
    _write_wav(line_wav, [111, -222, 333, -444], sample_rate=48000)
    conf_ref = cas.put_file(line_wav, kind="audio", ext="wav", move=False,
                            retention="intermediate")
    _, _conf_sha = _make_line(repos, deliverable.id, shot.id, line_no=1, conformed_samples=4,
                             cas=cas, payload=b"stem1")
    # point the line at the real file content
    narration = repos.content.narration_lines(deliverable.id)[0]
    repos.content.mark_line_voiced(
        narration.id, asset_sha256=conf_ref.sha256, duration_ms=1,
        conformed_sha256=conf_ref.sha256, conformed_samples=4, voice_snapshot_json="{}",
    )
    tl = build_timeline(repos.content, deliverable.id, config.settings)
    lead = 150 * sr // 1000  # default lead_in_ms
    assert tl.lines[0].start_sample == lead

    line_wav = tmp_path / "line.wav"
    _write_wav(line_wav, [111, -222, 333, -444], sample_rate=sr)
    out_path = tmp_path / "stem.wav"
    total = build_narration_stem(tl, {conf_ref.sha256: str(line_wav)}, str(out_path))
    assert total == tl.total_samples

    with wave.open(str(out_path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    assert len(samples) == tl.total_samples
    # silence before the line
    assert all(v == 0 for v in samples[:lead])
    # line bytes exactly at the placement
    assert list(samples[lead : lead + 4]) == [111, -222, 333, -444]
    # silence after the line
    assert all(v == 0 for v in samples[lead + 4 :])


def test_stem_rejects_length_mismatch(tmp_path, monkeypatch):
    config, repos, track, deliverable, db, clock = _timeline_env(tmp_path, monkeypatch, filename="stem2")
    cas = _cas(config, db, clock)
    shot = _locked_shot(repos, track.id, 1, clip_duration_ms=5000)
    _make_line(repos, deliverable.id, shot.id, line_no=1, conformed_samples=8,
               cas=cas, payload=b"stem2")
    narration = repos.content.narration_lines(deliverable.id)[0]
    line_wav = tmp_path / "line2.wav"
    _write_wav(line_wav, [1, 2, 3, 4])
    conf_ref = cas.put_file(line_wav, kind="audio", ext="wav", move=False)
    repos.content.mark_line_voiced(
        narration.id, asset_sha256=conf_ref.sha256, duration_ms=1,
        conformed_sha256=conf_ref.sha256, conformed_samples=8, voice_snapshot_json="{}",
    )
    tl = build_timeline(repos.content, deliverable.id, config.settings)
    line_wav = tmp_path / "line.wav"
    _write_wav(line_wav, [1, 2, 3, 4])  # 4 samples, timeline expects 8
    with pytest.raises(MediaToolError):
        build_narration_stem(tl, {conf_ref.sha256: str(line_wav)}, str(tmp_path / "out.wav"))


# ============================================================ conform (H5)

@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_conform_trims_to_keep_ms_and_limits_peak(tmp_path, monkeypatch):
    import wave as _wave

    from studio.audio.analysis import s16_le_samples, sample_peak_dbfs, wav_header_samples
    from studio.audio.conform import conform_line

    config, db, clock, _ids, _repos, _home = _env(tmp_path, monkeypatch, filename="conf1")
    sr = int(config.settings.audio.sample_rate)
    raw = tmp_path / "raw.wav"
    # 500 ms silence + 2.0 s tone + 700 ms silence
    subprocess.run(  # noqa: S603, ASYNC221
        [shutil.which("ffmpeg") or "ffmpeg", "-nostats", "-hide_banner", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2.0:sample_rate=48000",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.7",
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[out]",
         "-map", "[out]", "-c:a", "pcm_s16le", str(raw)],
        check=True, capture_output=True,
    )
    cas = _cas(config, db, clock)
    result = await conform_line(
        str(raw), cas=cas, settings=config.settings, mix_gain_db=0.0,
        work_dir=str(tmp_path / "work"),
    )
    keep = config.settings.audio.speech_trim.keep_ms
    expected_samples = (keep + 2000 + keep) * sr // 1000
    tolerance = 10 * sr // 1000  # +/- 10 ms
    assert abs(result.samples - expected_samples) <= tolerance
    stored = cas.resolve(result.asset.sha256)
    assert wav_header_samples(str(stored)) == result.samples  # DB count == WAV header
    with _wave.open(str(stored), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    assert sample_peak_dbfs(s16_le_samples(frames)) <= -1.0  # alimiter ceiling
    assert result.asset.kind == "audio"


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_conform_gain_clamped(tmp_path, monkeypatch):
    from studio.audio.conform import conform_line

    config, db, clock, _ids, _repos, _home = _env(tmp_path, monkeypatch, filename="conf2")
    raw = tmp_path / "quiet.wav"
    _sine(raw, duration=1.0, freq=1000, volume=0.1)  # ~-38 dBFS tone, demands ~+20 dB
    cas = _cas(config, db, clock)
    result = await conform_line(
        str(raw), cas=cas, settings=config.settings, mix_gain_db=0.0,
        work_dir=str(tmp_path / "work2"),
    )
    max_gain = config.settings.audio.levels.max_line_gain_db
    assert result.gain_db == pytest.approx(max_gain)
    assert result.gain_clamped is True
