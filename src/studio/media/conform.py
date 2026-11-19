"""S10 clip conform (Phase 4 §J): derived, free, deterministic.

The raw paid clip is stored first and never regenerated. Conform normalizes
to ``timing.fps``, trims/pads to exactly ``clip_frames``, strips audio,
keeps native resolution with SAR 1, and encodes libx264/yuv420p. A raw clip
that is already exactly compliant is referenced with no re-encode. The
conformed frame count is verified with ffprobe and must equal
``clip_frames``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.config.settings import Settings
from studio.domain.errors import VisualQcError
from studio.qc.visual import _probe
from studio.video.ffmpeg import run_ffmpeg


@dataclass(frozen=True)
class ConformResult:
    path: Path
    clip_frames: int
    fps: int
    already_compliant: bool
    report: dict[str, Any]


def clip_frames_for(clip_duration_ms: int, fps: int) -> int:
    return round(clip_duration_ms * fps / 1000)


async def conform_clip(
    raw_path: Path,
    *,
    clip_duration_ms: int,
    settings: Settings,
    work_dir: Path,
) -> ConformResult:
    fps = int(settings.render.fps)
    clip_frames = clip_frames_for(clip_duration_ms, fps)
    tolerance_ms = int(settings.media.clip.duration_tolerance_ms)
    info = await _probe(raw_path)
    raw_frames = info.frame_count
    raw_fps = info.fps
    report: dict[str, Any] = {
        "clip_duration_ms": clip_duration_ms,
        "clip_frames": clip_frames,
        "fps": fps,
        "raw_frames": raw_frames,
        "raw_fps": raw_fps,
        "raw_duration_s": info.duration_s,
        "has_audio": info.has_audio,
        "width": info.width,
        "height": info.height,
    }
    strip_audio = bool(settings.media.clip.strip_audio)
    compliant = (
        raw_frames == clip_frames
        and raw_fps is not None
        and abs(raw_fps - fps) < 0.01
        and (not strip_audio or not info.has_audio)
    )
    if compliant:
        report["already_compliant"] = True
        return ConformResult(
            path=raw_path, clip_frames=clip_frames, fps=fps,
            already_compliant=True, report=report,
        )
    if raw_frames is None:
        raise VisualQcError("duration_mismatch: ffprobe could not count raw frames")
    delta_frames = raw_frames - clip_frames
    delta_ms = delta_frames * 1000.0 / fps
    if abs(delta_ms) > tolerance_ms and delta_frames < 0:
        raise VisualQcError(
            f"duration_mismatch: raw clip is {abs(delta_ms):.0f} ms short "
            f"({raw_frames} frames vs {clip_frames}); tolerance is {tolerance_ms} ms"
        )
    await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
    out = work_dir / f"conformed-{raw_path.stem}.mp4"
    # fps normalize -> trim/pad to exactly clip_frames -> SAR 1 -> encode.
    vf = f"fps={fps},setsar=1"
    if delta_frames < 0:
        vf += f",tpad=stop_mode=clone:stop_duration={abs(delta_ms) / 1000.0:.3f}"
    vf += f",trim=end_frame={clip_frames},setpts=PTS-STARTPTS"
    args = [
        "-y",
        "-i", str(raw_path),
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", str(int(settings.media.clip.conform_crf)),
        "-pix_fmt", "yuv420p",
        "-bitexact",
        "-fflags", "+bitexact",
        "-map_metadata", "-1",
        # Pin CFR output timing so the container duration (and hence the
        # probed avg_frame_rate) accounts for the final frame.
        "-r", str(fps),
    ]
    if strip_audio:
        args.append("-an")
    else:
        args.extend(["-c:a", "aac"])
    args.append(str(out))
    await run_ffmpeg(args, timeout_s=300.0)
    verified = await _probe(out)
    report["conformed_frames"] = verified.frame_count
    report["conformed_fps"] = verified.fps
    if verified.frame_count != clip_frames:
        raise VisualQcError(
            f"duration_mismatch: conformed clip has {verified.frame_count} frames, "
            f"expected {clip_frames}"
        )
    if verified.fps is not None and abs(verified.fps - fps) >= 0.01:
        raise VisualQcError(
            f"duration_mismatch: conformed clip probes at {verified.fps:.4f} fps, "
            f"expected {fps} fps"
        )
    report["already_compliant"] = False
    return ConformResult(
        path=out, clip_frames=clip_frames, fps=fps,
        already_compliant=False, report=report,
    )
