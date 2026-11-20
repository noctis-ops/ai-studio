"""Branch-coverage tests for the Phase 3 audio modules (spec §I coverage gate:
studio.audio + studio.providers.tts at >= 90%). Error paths and fallbacks that
the happy-path suite in test_audio_engine.py does not exercise.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from array import array
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from conftest import make_home

from studio.audio.analysis import (
    integrated_loudness,
    is_silent,
    parse_ebur128_summary,
    parse_loudnorm_json,
    rms_dbfs,
    sample_peak_dbfs,
    wav_header_samples,
)
from studio.audio.library import scan_library
from studio.audio.stem import build_narration_stem, stem_digest
from studio.audio.timeline import Timeline, build_timeline
from studio.audio.voice import (
    VoiceSettings,
    parse_voice_settings,
    request_settings,
    settings_fingerprint,
    voice_settings_json,
    voice_snapshot,
    worst_case_tts_musd,
)
from studio.config.loader import load_all
from studio.core.clock import FakeClock
from studio.core.ids import SequentialIds
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import Language
from studio.domain.errors import MediaToolError, StudioError
from studio.domain.money import usd_to_musd


def _env(tmp_path, monkeypatch, *, filename="cov"):
    home = make_home(tmp_path, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    repos = build_repos(db, clock, SequentialIds(filename))
    return config, db, clock, repos, home


def _sine_file(path: Path, *, duration=1.0, freq=1000, volume: float | None = None) -> None:
    graph = f"sine=frequency={freq}:duration={duration}"
    if volume is not None:
        graph += f",volume={volume}"
    subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-nostats", "-hide_banner", "-y",
         "-f", "lavfi", "-i", graph, "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True,
    )


# ============================================================ voice.py

def test_parse_voice_settings_rejects_bad_json():
    with pytest.raises(StudioError, match="not valid JSON"):
        parse_voice_settings("this is not json")


def test_parse_voice_settings_rejects_non_object():
    with pytest.raises(StudioError, match="JSON object"):
        parse_voice_settings("[1, 2, 3]")


def test_parse_voice_settings_rejects_validation_failure():
    with pytest.raises(StudioError, match="failed validation"):
        parse_voice_settings('{"stability": 1.5}')


def test_parse_voice_settings_rejects_unknown_field():
    with pytest.raises(StudioError, match="failed validation"):
        parse_voice_settings('{"surprise_field": 1}')


def test_voice_settings_roundtrip_and_request_subset():
    original = VoiceSettings(
        model_key="tts.main", stability=0.6, similarity_boost=0.8,
        mix_gain_db=2.5, license_attested_at="2026-01-01T00:00:00.000Z",
    )
    parsed = parse_voice_settings(voice_settings_json(original))
    assert parsed == original
    # provider-relevant subset only: model_key/license_attested_at excluded
    wire = request_settings(original)
    assert set(wire) == {"stability", "similarity_boost"}
    assert "model_key" not in wire
    assert "license_attested_at" not in wire
    assert "mix_gain_db" not in wire


def test_settings_fingerprint_stable_and_sensitive():
    a = VoiceSettings(stability=0.5)
    b = VoiceSettings(stability=0.5)
    c = VoiceSettings(stability=0.6)
    assert settings_fingerprint(a) == settings_fingerprint(b)
    assert settings_fingerprint(a) != settings_fingerprint(c)


def test_voice_snapshot_shape():
    settings = VoiceSettings(stability=0.5, speed=1.1)
    snapshot = json.loads(
        voice_snapshot(
            language=Language.AR, voice_id="voice-1", settings=settings,
            model_key="tts.main", model_id="eleven_multilingual_v2",
        )
    )
    assert snapshot["language"] == "ar"
    assert snapshot["voice_id"] == "voice-1"
    assert snapshot["model_key"] == "tts.main"
    assert snapshot["model_id"] == "eleven_multilingual_v2"
    assert snapshot["settings"]["stability"] == 0.5
    assert snapshot["settings_fingerprint"] == settings_fingerprint(settings)


def test_worst_case_tts_musd_matches_estimate(tmp_path, monkeypatch):
    config, _db, _clock, _repos, _home = _env(tmp_path, monkeypatch, filename="vc")
    entry = config.registry.models["tts.main"]
    total = worst_case_tts_musd(entry, ["abcd", "ef"])
    assert total == usd_to_musd(Decimal(6) * entry.pricing.usd / Decimal(1000))


def test_worst_case_tts_musd_rejects_non_per_1k_model(tmp_path, monkeypatch):
    config, _db, _clock, _repos, _home = _env(tmp_path, monkeypatch, filename="vc2")
    entry = config.registry.models["video.i2v.draft"]
    assert type(entry.pricing).__name__ == "PerSecond"
    with pytest.raises(StudioError, match="per_1k_chars"):
        worst_case_tts_musd(entry, ["text"])


# ============================================================ analysis.py

def test_empty_samples_report_negative_inf():
    assert rms_dbfs(array("h", [])) == -math.inf
    assert sample_peak_dbfs(array("h", [])) == -math.inf


def test_is_silent_threshold(tmp_path, monkeypatch):
    assert is_silent(array("h", [0] * 100), -60.0)
    assert not is_silent(array("h", [10000] * 100), -60.0)


def test_parse_ebur128_summary_with_inf_values():
    stderr = (
        "Integrated loudness:\n    I:         -inf LUFS\n"
        "Loudness range:\n    LRA:        -inf LU\n"
        "True peak:\n    Peak:       -inf dBTP\n"
    )
    summary = parse_ebur128_summary(stderr)
    assert summary.integrated_lufs == -math.inf
    assert summary.lra_lu == -math.inf
    assert summary.true_peak_dbtp == -math.inf


def test_parse_loudnorm_json_rejects_unparseable_block():
    with pytest.raises(MediaToolError, match="unparseable"):
        parse_loudnorm_json('loudnorm printed {"a": } here')


def test_parse_loudnorm_json_rejects_malformed_braces():
    with pytest.raises(MediaToolError, match="malformed"):
        parse_loudnorm_json('no closing brace { "a": 1 "')


def test_wav_header_samples_rejects_bad_file(tmp_path):
    bogus = tmp_path / "bogus.wav"
    bogus.write_bytes(b"definitely not a wav")
    with pytest.raises(MediaToolError, match="not a readable WAV"):
        wav_header_samples(str(bogus))


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_integrated_loudness_and_astats_on_sine(tmp_path):
    sine = tmp_path / "sine.wav"
    _sine_file(sine, duration=1.5, freq=1000)
    summary = await integrated_loudness(str(sine))
    assert summary.integrated_lufs is not None
    assert summary.integrated_lufs == pytest.approx(-21.0, abs=1.5)
    from studio.audio.analysis import astats_levels

    levels = await astats_levels(str(sine))
    assert "rms_db" in levels
    assert levels["rms_db"] < 0


# ============================================================ stem.py

def _timeline_with_lines(placements, *, total_samples=240_000):
    from studio.audio.timeline import LinePlacement  # noqa: F401

    return Timeline(
        fps=24, sample_rate=48000, shots=(), scenes=(), lines=placements,
        total_frames=total_samples // 2000, total_samples=total_samples,
        timeline_hash="h" * 64,
    )


def _placement(line_id: str, start: int, length: int, sha: str = "a" * 64):
    from studio.audio.timeline import LinePlacement

    return LinePlacement(
        line_id=line_id, shot_id="shot-1", start_sample=start,
        length_samples=length, conformed_sha256=sha,
    )


def _write_wav(path: Path, samples, *, sample_rate=48000, channels=1, sampwidth=2):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sampwidth)
        handle.setframerate(sample_rate)
        handle.writeframes(array("h", samples).tobytes())


def test_stem_rejects_8bit_wav(tmp_path):
    path = tmp_path / "8bit.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(48000)
        handle.writeframes(b"\x80" * 100)
    tl = _timeline_with_lines((_placement("l1", 0, 100),), total_samples=100)
    with pytest.raises(MediaToolError, match="16-bit"):
        build_narration_stem(tl, {"a" * 64: str(path)}, str(tmp_path / "out.wav"))


def test_stem_rejects_stereo_wav(tmp_path):
    path = tmp_path / "stereo.wav"
    _write_wav(path, [0] * 8, channels=2)
    tl = _timeline_with_lines((_placement("l1", 0, 4),), total_samples=8)
    with pytest.raises(MediaToolError, match="mono"):
        build_narration_stem(tl, {"a" * 64: str(path)}, str(tmp_path / "out.wav"))


def test_stem_rejects_unreadable_file(tmp_path):
    path = tmp_path / "bogus.wav"
    path.write_bytes(b"not a wav")
    tl = _timeline_with_lines((_placement("l1", 0, 4),), total_samples=8)
    with pytest.raises(MediaToolError, match="cannot read WAV"):
        build_narration_stem(tl, {"a" * 64: str(path)}, str(tmp_path / "out.wav"))


def test_stem_rejects_missing_cas_path(tmp_path):
    tl = _timeline_with_lines((_placement("l1", 0, 100, sha="b" * 64),), total_samples=200)
    with pytest.raises(MediaToolError, match="missing CAS path"):
        build_narration_stem(tl, {}, str(tmp_path / "out.wav"))


def test_stem_rejects_overlapping_lines(tmp_path):
    line_wav = tmp_path / "line.wav"
    _write_wav(line_wav, [1, 2, 3, 4])
    sha = "a" * 64
    tl = _timeline_with_lines(
        (
            _placement("l2", 0, 4, sha),
            _placement("l1", 2, 4, sha),
        ),
        total_samples=16,
    )
    with pytest.raises(MediaToolError, match="overlaps"):
        build_narration_stem(tl, {sha: str(line_wav)}, str(tmp_path / "out.wav"))


def test_stem_digest_is_pcm_only(tmp_path):
    path = tmp_path / "digest.wav"
    samples = [1, 2, 3, -4]
    _write_wav(path, samples)
    assert stem_digest(path) == __import__("hashlib").sha256(
        array("h", samples).tobytes()
    ).hexdigest()


def test_stem_with_no_lines_is_all_silence(tmp_path):
    tl = _timeline_with_lines((), total_samples=4096 + 7)
    out = tmp_path / "silence.wav"
    total = build_narration_stem(tl, {}, str(out))
    assert total == tl.total_samples
    with wave.open(str(out), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    assert frames == b"\x00\x00" * tl.total_samples


# ============================================================ timeline.py

def _timeline_env(tmp_path, monkeypatch, *, filename="tlc"):
    config, db, clock, repos, _home = _env(tmp_path, monkeypatch, filename=filename)
    series = repos.content.create_series(name="S", slug="s")
    episode = repos.content.create_episode(
        series_id=series.id, title="E", concept="c",
        languages_json='["ar"]', aspects_json='["9:16", "16:9"]',
    )
    track = repos.content.create_track(episode_id=episode.id, aspect="9:16")
    deliverable = repos.content.create_deliverable(
        track_id=track.id, language="ar", channel_id="yt"
    )
    return config, db, clock, repos, episode, track, deliverable


def test_timeline_unknown_deliverable_raises(tmp_path, monkeypatch):
    config, _db, _clock, repos, _episode, _track, _deliverable = _timeline_env(
        tmp_path, monkeypatch, filename="tlu"
    )
    with pytest.raises(StudioError, match="deliverable not found"):
        build_timeline(repos.content, "no-such-deliverable", config.settings)


def test_timeline_track_without_shots_raises(tmp_path, monkeypatch):
    config, _db, _clock, repos, _episode, _track, deliverable = _timeline_env(
        tmp_path, monkeypatch, filename="tls"
    )
    with pytest.raises(StudioError, match="no shots"):
        build_timeline(repos.content, deliverable.id, config.settings)


def test_timeline_shot_without_timing_raises(tmp_path, monkeypatch):
    config, _db, _clock, repos, _episode, track, deliverable = _timeline_env(
        tmp_path, monkeypatch, filename="tlt"
    )
    repos.content.create_shot(track_id=track.id, shot_no=1, prompt="p")
    with pytest.raises(StudioError, match="clip_duration_ms"):
        build_timeline(repos.content, deliverable.id, config.settings)


def _ref(cas, payload: bytes) -> str:
    return cas.put_bytes(payload, kind="audio", ext="wav").sha256


def _locked_shot(repos, track_id, shot_no, *, clip_duration_ms=5000):
    shot = repos.content.create_shot(track_id=track_id, shot_no=shot_no, prompt=f"p{shot_no}")
    repos.content.set_shot_timing(
        shot.id, clip_duration_ms=clip_duration_ms, speed_factor=1.0,
        hold_frames=0, timing_status="locked",
    )
    return shot


def test_timeline_line_for_other_tracks_shot_raises(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _timeline_env(
        tmp_path, monkeypatch, filename="tlo"
    )
    from studio.core.cas import ContentStore

    cas = ContentStore(Path(config.settings.paths.cas), _db, _clock)
    other_track = repos.content.create_track(episode_id=episode.id, aspect="16:9")
    foreign_shot = _locked_shot(repos, other_track.id, 1, clip_duration_ms=5000)
    _locked_shot(repos, track.id, 1, clip_duration_ms=5000)
    repos.content.upsert_narration_line(
        deliverable_id=deliverable.id, shot_id=foreign_shot.id, line_no=1,
        text="foreign", asset_sha256=_ref(cas, b"foreign"), duration_ms=1,
    )
    with pytest.raises(StudioError, match="another track"):
        build_timeline(repos.content, deliverable.id, config.settings)


def test_timeline_scene_outside_shot_range_raises(tmp_path, monkeypatch):
    config, _db, _clock, repos, _episode, track, deliverable = _timeline_env(
        tmp_path, monkeypatch, filename="tlx"
    )
    for i in (1, 2):
        shot = _locked_shot(repos, track.id, i)
        from studio.core.cas import ContentStore

        cas = ContentStore(Path(config.settings.paths.cas), _db, _clock)
        asset = _ref(cas, f"asset-{i}".encode())
        conf = _ref(cas, f"conf-{i}".encode())
        repos.content.mark_line_voiced(
            repos.content.upsert_narration_line(
                deliverable_id=deliverable.id, shot_id=shot.id, line_no=1,
                text="x", asset_sha256=asset, duration_ms=1,
            ).id,
            asset_sha256=asset, duration_ms=1,
            conformed_sha256=conf, conformed_samples=100, voice_snapshot_json="{}",
        )
    repos.content.upsert_scene(
        track_id=track.id, scene_no=1, shot_from=1, shot_to=9,
        music_mood="hopeful", intensity=3, ambience_tags_json="[]",
    )
    with pytest.raises(StudioError, match="outside the track"):
        build_timeline(repos.content, deliverable.id, config.settings)


def test_timeline_shot_without_scene_gets_none(tmp_path, monkeypatch):
    config, _db, _clock, repos, _episode, track, deliverable = _timeline_env(
        tmp_path, monkeypatch, filename="tlz"
    )
    for i in (1, 2):
        shot = _locked_shot(repos, track.id, i)
        from studio.core.cas import ContentStore

        cas = ContentStore(Path(config.settings.paths.cas), _db, _clock)
        asset = _ref(cas, f"asset-{i}".encode())
        conf = _ref(cas, f"conf-{i}".encode())
        repos.content.mark_line_voiced(
            repos.content.upsert_narration_line(
                deliverable_id=deliverable.id, shot_id=shot.id, line_no=1,
                text="x", asset_sha256=asset, duration_ms=1,
            ).id,
            asset_sha256=asset, duration_ms=1,
            conformed_sha256=conf, conformed_samples=100, voice_snapshot_json="{}",
        )
    repos.content.upsert_scene(
        track_id=track.id, scene_no=1, shot_from=1, shot_to=1,
        music_mood="hopeful", intensity=3, ambience_tags_json="[]",
    )
    tl = build_timeline(repos.content, deliverable.id, config.settings)
    scene_ids = [s.scene_id for s in tl.shots]
    assert scene_ids[0] is not None
    assert scene_ids[1] is None


def test_timeline_line_without_conformed_audio_raises(tmp_path, monkeypatch):
    config, _db, _clock, repos, _episode, track, deliverable = _timeline_env(
        tmp_path, monkeypatch, filename="tlq"
    )
    from studio.core.cas import ContentStore

    cas = ContentStore(Path(config.settings.paths.cas), _db, _clock)
    shot = _locked_shot(repos, track.id, 1)
    repos.content.upsert_narration_line(
        deliverable_id=deliverable.id, shot_id=shot.id, line_no=1,
        text="x", asset_sha256=_ref(cas, b"asset"), duration_ms=1,
    )
    with pytest.raises(StudioError, match="no conformed audio"):
        build_timeline(repos.content, deliverable.id, config.settings)


# ============================================================ library.py

def _library_env(tmp_path, monkeypatch, *, filename="libt"):
    config, _db, _clock, repos, _home = _env(tmp_path, monkeypatch, filename=filename)
    root = tmp_path / "library"
    root.mkdir()
    return config, repos, root


def _write_sidecar(kind_dir: Path, data) -> None:
    kind_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        (kind_dir / "index.yaml").write_text(data, encoding="utf-8")
    else:
        (kind_dir / "index.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )


def _touch_audio(path: Path) -> None:
    path.write_bytes(b"fake-audio-bytes")


@pytest.mark.asyncio
async def test_scan_unparseable_sidecar_registers_files_inactive(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls1")
    music = root / "music"
    _write_sidecar(music, "::: [unbalanced")
    _touch_audio(music / "x.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert len(rows) == 1
    assert rows[0].active == 0
    assert "sidecar" in (rows[0].scan_error or "")
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_sidecar_defaults_must_be_mapping(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls2")
    music = root / "music"
    _write_sidecar(music, {"defaults": [1, 2], "tracks": []})
    _touch_audio(music / "x.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_sidecar_tracks_must_be_list(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls3")
    music = root / "music"
    _write_sidecar(music, {"tracks": {"a": 1}})
    _touch_audio(music / "x.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_sidecar_track_entries_must_be_mappings(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls4")
    music = root / "music"
    _write_sidecar(music, {"tracks": [5]})
    _touch_audio(music / "x.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_missing_sidecar_registers_files_inactive(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls5")
    sfx = root / "sfx"
    sfx.mkdir(parents=True)
    _touch_audio(sfx / "boom.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert len(rows) == 1
    assert rows[0].scan_error == "missing index.yaml"
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_music_requires_intensity(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls6")
    music = root / "music"
    _write_sidecar(music, {"tracks": [
        {"file": "a.wav", "title": "A", "moods": ["hopeful"], "license": "CC0"},
    ]})
    _touch_audio(music / "a.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert rows[0].scan_error == "music entry requires intensity (1-5)"
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_entry_requires_nonempty_tags(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls7")
    music = root / "music"
    _write_sidecar(music, {"tracks": [
        {"file": "a.wav", "title": "A", "moods": [], "intensity": 2, "license": "CC0"},
    ]})
    _touch_audio(music / "a.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert "requires non-empty" in (rows[0].scan_error or "")
    assert report.invalid


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_scan_sfx_intensity_optional_and_unknown_tags_rejected(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls8")
    sfx = root / "sfx"
    _write_sidecar(sfx, {"tracks": [
        {"file": "whoosh.wav", "title": "Whoosh", "tags": ["whoosh"], "license": "CC0"},
        {"file": "bogus.wav", "title": "Bogus", "tags": ["not-a-tag"], "license": "CC0"},
    ]})
    _sine_file(sfx / "whoosh.wav", duration=0.5, freq=900)
    (sfx / "bogus.wav").write_bytes(b"fake-audio-bytes-2")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    rows = {row.title: row for row in repos.audio.list_library(include_inactive=True)}
    assert rows["Whoosh"].active == 1
    assert "unknown sfx tags" in (rows["Bogus"].scan_error or "")
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_ambience_unknown_tags_rejected(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls9")
    ambience = root / "ambience"
    _write_sidecar(ambience, {"tracks": [
        {"file": "rain.wav", "title": "Rain", "tags": ["not-rain"], "license": "CC0"},
    ]})
    _touch_audio(ambience / "rain.wav")
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert "unknown ambience tags" in (rows[0].scan_error or "")
    assert report.invalid


@pytest.mark.asyncio
async def test_scan_sidecar_listed_but_missing_file_never_scanned(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls10")
    music = root / "music"
    _write_sidecar(music, {"tracks": [
        {"file": "gone.wav", "title": "Gone", "moods": ["hopeful"], "intensity": 2,
         "license": "CC0"},
    ]})
    report = await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    assert ("music/gone.wav", "file missing and never scanned") in report.invalid


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_scan_sidecar_defaults_are_merged(tmp_path, monkeypatch):
    config, repos, root = _library_env(tmp_path, monkeypatch, filename="ls11")
    music = root / "music"
    _write_sidecar(music, {
        "defaults": {"license": "CC-BY 4.0", "intensity": 3},
        "tracks": [{"file": "a.wav", "title": "A", "moods": ["hopeful"]}],
    })
    _sine_file(music / "a.wav", duration=0.5, freq=800)
    await scan_library(
        library_root=root, repos=repos.audio, vocab=config.vocab, measure_loudness=False,
    )
    rows = repos.audio.list_library(include_inactive=True)
    assert rows[0].active == 1
    assert rows[0].intensity == 3


# ============================================================ conform.py

@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_conform_digital_silence_fails_closed(tmp_path, monkeypatch):
    from studio.audio.conform import conform_line
    from studio.core.cas import ContentStore

    config, db, clock, _repos, _home = _env(tmp_path, monkeypatch, filename="cfsil")
    raw = tmp_path / "silence.wav"
    _sine_file(raw, duration=1.0, freq=1000, volume=0.0)
    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    with pytest.raises(MediaToolError, match=r"digital silence|unusable"):
        await conform_line(
            str(raw), cas=cas, settings=config.settings, mix_gain_db=0.0,
            work_dir=str(tmp_path / "work"),
        )


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_conform_falls_back_to_astats_under_floor(tmp_path, monkeypatch):
    import math as _math

    from studio.audio import conform as conform_mod
    from studio.audio.analysis import Ebur128Summary, wav_header_samples
    from studio.audio.conform import conform_line
    from studio.core.cas import ContentStore

    config, db, clock, _repos, _home = _env(tmp_path, monkeypatch, filename="cfloud")
    raw = tmp_path / "quiet.wav"
    _sine_file(raw, duration=1.0, freq=1000, volume=0.01)

    async def _fake_integrated(*args, **kwargs):
        return Ebur128Summary(integrated_lufs=-_math.inf, true_peak_dbtp=None, lra_lu=None)

    async def _fake_astats(*args, **kwargs):
        return {"rms_db": -38.0}

    monkeypatch.setattr(conform_mod, "integrated_loudness", _fake_integrated)
    monkeypatch.setattr(conform_mod, "astats_levels", _fake_astats)
    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    result = await conform_line(
        str(raw), cas=cas, settings=config.settings, mix_gain_db=0.0,
        work_dir=str(tmp_path / "work"),
    )
    assert wav_header_samples(str(cas.resolve(result.asset.sha256))) == result.samples
