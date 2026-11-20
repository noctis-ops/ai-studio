"""Phase-3 pre-flight regression: `probe()` must work with a real ffprobe.

The Phase 3 sandbox used an ffprobe stand-in that ignored `-f <extension>`.
Real ffprobe (>= 5) rejects `-f png` ("Unknown input format: png"), which
broke every image/keyframe CAS write. This module pins the fix: probe
auto-detects the container and classifies single-frame stills as images.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.ffmpeg


@pytest.mark.asyncio
async def test_probe_autodetects_png_without_forcing_demuxer(tmp_path: Path) -> None:
    from studio.video.ffmpeg import run_ffmpeg
    from studio.video.probe import probe

    png = tmp_path / "still.png"
    await run_ffmpeg(
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x123456:s=64x36",
            "-frames:v",
            "1",
            str(png),
        ],
        timeout_s=60.0,
    )
    info = probe(png)
    assert info.kind == "image"
    assert (info.width, info.height) == (64, 36)
    assert info.has_audio is False


@pytest.mark.asyncio
async def test_probe_autodetects_audio_and_video_containers(tmp_path: Path) -> None:
    from studio.video.ffmpeg import run_ffmpeg
    from studio.video.probe import probe

    wav = tmp_path / "tone.wav"
    await run_ffmpeg(
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(wav),
        ],
        timeout_s=60.0,
    )
    audio = probe(wav)
    assert audio.kind == "audio"
    assert audio.has_audio is True
    assert audio.audio_channels == 1
    assert audio.duration_ms is not None
    assert abs(audio.duration_ms - 250) <= 30

    mp4 = tmp_path / "clip.mp4"
    await run_ffmpeg(
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x36:d=0.2:r=24",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(mp4),
        ],
        timeout_s=60.0,
    )
    video = probe(mp4)
    assert video.kind == "video"
    assert (video.width, video.height) == (64, 36)
    assert video.fps is not None and abs(video.fps - 24.0) < 0.01
