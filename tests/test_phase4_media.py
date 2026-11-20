"""T-CLP-01 (conform table), T-CLP-04, T-CLP-05, T-QCV-01 (PHASE4 §P).

ffmpeg-backed: inputs are lavfi-generated. All tests are offline and use
the shipped sandbox settings (fps 24, tolerance 500 ms) unless overridden.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import make_home

from studio.config.loader import load_all
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock
from studio.db import Database, migrate
from studio.domain.enums import Aspect
from studio.domain.errors import VisualQcError
from studio.media.conform import clip_frames_for, conform_clip
from studio.qc.visual import qc_image, qc_video


def _settings(tmp_path, monkeypatch, *, filename="qc", settings_overrides=None):
    home = make_home(tmp_path, filename=filename, settings_overrides=settings_overrides)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    return load_all(home).settings


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *args],
        check=True,
        timeout=180,
    )


def _clip(path: Path, *, pattern: str, frames: int | None = None,
          duration_s: float | None = None, rate: int = 24,
          size: str = "270x480", audio: bool = False,
          fullrange: bool = False) -> Path:
    if pattern == "colorc":
        source = f"color=c=black:size={size}:rate={rate}"
    elif pattern == "colorred":
        source = f"color=c=red:size={size}:rate={rate}"
    else:
        source = f"{pattern}=size={size}:rate={rate}"
    args = ["-f", "lavfi", "-i", source + (f":duration={duration_s}" if duration_s else "")]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s or (frames or 1) / rate}"]
    if fullrange:
        args += ["-pix_fmt", "yuv444p", "-color_range", "pc"]
    else:
        args += ["-pix_fmt", "yuv420p"]
    if frames is not None:
        args += ["-frames:v", str(frames)]
    if audio:
        args += ["-shortest", "-c:a", "aac"]
    args += [str(path)]
    _ffmpeg(args)
    return path


def _check(checks, name):
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError(f"check {name!r} not in {[c.name for c in checks]}")


# ======================================================================
# T-CLP-01 — conform table-driven cases
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_clp_01_table_driven_conform(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    fps = int(settings.render.fps)
    locked_ms = 5000
    expected = clip_frames_for(locked_ms, fps)
    assert expected == 120

    cases = []
    exact = _clip(tmp_path / "exact.mp4", pattern="testsrc2", frames=120)
    cases.append(("exact", exact, 120, True))
    one_over = _clip(tmp_path / "one_over.mp4", pattern="testsrc2", frames=121)
    cases.append(("plus_one_frame", one_over, 120, False))
    three_short = _clip(tmp_path / "three_short.mp4", pattern="testsrc2", frames=117)
    cases.append(("three_short_pad", three_short, 120, False))
    long = _clip(tmp_path / "long.mp4", pattern="testsrc2", frames=144)
    cases.append(("long_trim", long, 120, False))
    at30 = _clip(tmp_path / "at30.mp4", pattern="testsrc2", frames=150, rate=30)
    cases.append(("thirty_fps_source", at30, 120, False))

    for label, src, want_frames, already in cases:
        work_dir = tmp_path / f"work_{label}"
        result = await conform_clip(src, clip_duration_ms=locked_ms,
                                    settings=settings, work_dir=work_dir)
        assert result.clip_frames == 120, label
        assert result.fps == fps, label
        assert result.already_compliant is already, label
        if already:
            assert result.path == src
        else:
            from studio.qc.visual import _probe

            info = await _probe(result.path)
            assert info.frame_count == want_frames, label
            assert abs((info.fps or 0) - fps) < 0.01, label
        assert result.report["clip_duration_ms"] == locked_ms


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_clp_01_beyond_tolerance_raises_duration_mismatch(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    short = _clip(tmp_path / "too_short.mp4", pattern="testsrc2", frames=96)  # 4 s vs 5 s
    with pytest.raises(VisualQcError, match="duration_mismatch"):
        await conform_clip(short, clip_duration_ms=5000, settings=settings,
                           work_dir=tmp_path / "work_mismatch")


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_clp_01_conformed_clip_is_audio_free(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    raw = _clip(tmp_path / "with_audio.mp4", pattern="testsrc2", frames=120,
                duration_s=5.0, audio=True)
    result = await conform_clip(raw, clip_duration_ms=5000, settings=settings,
                                work_dir=tmp_path / "work_audio")
    from studio.qc.visual import _probe

    info = await _probe(result.path)
    assert info.has_audio is False
    assert result.report["has_audio"] is True  # raw had audio; strip_audio is on


# ======================================================================
# T-CLP-05 — raw-clip retention upgrade + never-downgrade
# ======================================================================


@pytest.mark.ffmpeg
def test_clp_05_raw_clip_retention_upgrade_and_never_downgrade(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    home = Path(settings.paths.home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    cas = ContentStore(Path(settings.paths.cas), db, clock)

    raw = _clip(tmp_path / "raw.mp4", pattern="testsrc2", frames=120)
    # first stored as intermediate (e.g. audition path) …
    cas.put_file(raw, kind="video", ext="mp4", retention="intermediate", move=False)
    sha = None
    row = db.connect().execute("SELECT sha256, retention FROM assets").fetchone()
    sha = row["sha256"]
    assert row["retention"] == "intermediate"
    # … then the paid stage upgrades it to keep_n_days …
    cas.put_file(raw, kind="video", ext="mp4", retention="keep_n_days",
                 purge_after="2027-01-01T00:00:00Z", move=False)
    row = db.connect().execute(
        "SELECT retention, purge_after FROM assets WHERE sha256 = ?", (sha,)
    ).fetchone()
    assert row["retention"] == "keep_n_days"
    assert row["purge_after"] == "2027-01-01T00:00:00Z"
    # … and a later intermediate re-store never downgrades it back.
    cas.put_file(raw, kind="video", ext="mp4", retention="intermediate", move=False)
    row = db.connect().execute(
        "SELECT retention, purge_after FROM assets WHERE sha256 = ?", (sha,)
    ).fetchone()
    assert row["retention"] == "keep_n_days"
    assert row["purge_after"] == "2027-01-01T00:00:00Z"


# ======================================================================
# T-QCV-01 — each visual check against a purpose-built bad input
# ======================================================================


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_qcv_01_image_checks(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    # tiny file: readable fails
    tiny = tmp_path / "tiny.png"
    _ffmpeg(["-f", "lavfi", "-i", "color=red:size=16x16:duration=1:rate=1",
             "-frames:v", "1", str(tiny)])
    ok = tmp_path / "ok.png"
    _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=540x960:rate=1", "-frames:v", "1", str(ok)])
    black = tmp_path / "black.png"
    _ffmpeg(["-f", "lavfi", "-i", "color=c=black:size=540x960:duration=1:rate=1",
             "-pix_fmt", "gray", "-frames:v", "1", str(black)])
    wide = tmp_path / "wide.png"
    _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=960x540:rate=1", "-frames:v", "1", str(wide)])

    checks = await qc_image(ok, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "readable").status == "pass"
    assert _check(checks, "min_short_side").status == "pass"
    assert _check(checks, "aspect").status == "pass"
    assert _check(checks, "not_black").status == "pass"

    checks = await qc_image(tiny, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "readable").status == "fail"
    assert _check(checks, "min_short_side").status == "fail"

    checks = await qc_image(black, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "not_black").status == "fail"

    checks = await qc_image(wide, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "aspect").status == "fail"


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_qcv_01_video_checks(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    good = _clip(tmp_path / "good.mp4", pattern="testsrc2", frames=120)
    black = _clip(tmp_path / "black.mp4", pattern="colorc", frames=120,
               fullrange=True)
    frozen = _clip(tmp_path / "frozen.mp4", pattern="colorred", frames=120)
    audio = _clip(tmp_path / "audio.mp4", pattern="testsrc2", frames=120,
                  duration_s=5.0, audio=True)
    short = _clip(tmp_path / "short.mp4", pattern="testsrc2", frames=117)
    fps25 = _clip(tmp_path / "fps25.mp4", pattern="testsrc2", frames=125, rate=25)
    wide = _clip(tmp_path / "wide.mp4", pattern="testsrc2", frames=120, size="480x270")
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(good.read_bytes()[:4096])
    # Keyframe must match the clip's first frame exactly: compare against a
    # frame extracted from the clip itself (a lossless lavfi render of the
    # same source is measurably dissimilar after libx264's lossy encode).
    keyframe = tmp_path / "keyframe.png"
    _ffmpeg(["-i", str(good), "-frames:v", "1", str(keyframe)])

    # good baseline
    checks = await qc_video(good, aspect=Aspect.TALL, settings=settings,
                            keyframe_path=keyframe, expected_frames=120,
                            expected_fps=24)
    assert _check(checks, "decode").status == "pass"
    assert _check(checks, "aspect").status == "pass"
    assert _check(checks, "frame_count").status == "pass"
    assert _check(checks, "fps").status == "pass"
    assert _check(checks, "black_ratio").status == "pass"
    assert _check(checks, "has_audio").status == "pass"
    ssim = _check(checks, "first_frame_ssim")
    assert ssim.value is not None and float(ssim.value) > 0.9

    # black clip: black_ratio + first_frame_luma fail
    checks = await qc_video(black, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "black_ratio").status == "fail"
    assert _check(checks, "first_frame_luma").status == "fail"

    # fully frozen clip: freeze ratio fails under default thresholds
    checks = await qc_video(frozen, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "freeze_ratio").status == "fail"

    # audio stream: has_audio warns
    checks = await qc_video(audio, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "has_audio").status == "warn"

    # wrong frame count and wrong fps
    checks = await qc_video(short, aspect=Aspect.TALL, settings=settings,
                            expected_frames=120)
    assert _check(checks, "frame_count").status == "fail"
    checks = await qc_video(fps25, aspect=Aspect.TALL, settings=settings,
                            expected_fps=24)
    assert _check(checks, "fps").status == "fail"

    # wrong aspect
    checks = await qc_video(wide, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "aspect").status == "fail"

    # truncated file: decode fails
    checks = await qc_video(truncated, aspect=Aspect.TALL, settings=settings)
    assert _check(checks, "decode").status == "fail"


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_qcv_01_freeze_warn_vs_fail(tmp_path, monkeypatch):
    # warn band: warn at >10%, fail at >150% (unreachable) → warn
    warn_settings = _settings(
        tmp_path, monkeypatch, filename="qcwarn",
        settings_overrides={"media": {"qc": {"video": {
            "freeze_warn_ratio": 0.1, "freeze_fail_ratio": 1.0,
        }}}},
    )
    frozen = _clip(tmp_path / "frozen.mp4", pattern="colorred", frames=120)
    checks = await qc_video(frozen, aspect=Aspect.TALL, settings=warn_settings)
    assert _check(checks, "freeze_ratio").status == "warn"

    # fail band: warn at >50%, fail at >85% (defaults) → fail
    fail_settings = _settings(tmp_path, monkeypatch, filename="qcfail")
    checks = await qc_video(frozen, aspect=Aspect.TALL, settings=fail_settings)
    assert _check(checks, "freeze_ratio").status == "fail"


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_qcv_01_ssim_recorded_against_keyframe(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    good = _clip(tmp_path / "good.mp4", pattern="testsrc2", frames=24)
    black = _clip(tmp_path / "black.mp4", pattern="colorc", frames=24)
    # Extract the keyframe from the clip itself so the comparison is
    # encode-invariant (lavfi render vs libx264 output is dissimilar).
    keyframe = tmp_path / "keyframe.png"
    _ffmpeg(["-i", str(good), "-frames:v", "1", str(keyframe)])

    good_checks = await qc_video(good, aspect=Aspect.TALL, settings=settings,
                                 keyframe_path=keyframe)
    assert float(_check(good_checks, "first_frame_ssim").value or 0) > 0.9

    black_checks = await qc_video(black, aspect=Aspect.TALL, settings=settings,
                                  keyframe_path=keyframe)
    ssim = _check(black_checks, "first_frame_ssim")
    assert ssim.value is not None
    assert ssim.status in ("warn", "fail")  # far from the keyframe


@pytest.mark.ffmpeg
def test_qcv_01_min_bytes_video(tmp_path, monkeypatch):
    import asyncio

    settings = _settings(tmp_path, monkeypatch)
    tiny = _clip(tmp_path / "tiny.mp4", pattern="colorc", frames=1, size="16x32")
    assert tiny.stat().st_size < 20000

    async def run():
        return await qc_video(tiny, aspect=Aspect.TALL, settings=settings)

    checks = asyncio.run(run())
    assert _check(checks, "min_bytes").status == "fail"
