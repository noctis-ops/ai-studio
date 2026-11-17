"""ffprobe-based media probing. Sync entry points (CAS calls them); they are
cheap and always run on short files."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from studio.domain.errors import MediaToolError
from studio.video.ffmpeg import find_binary


@dataclass(frozen=True)
class ProbeResult:
    kind: str  # 'audio' | 'video' | 'image' | 'unknown'
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool
    audio_channels: int | None = None


def _parse_fraction(text: str) -> float | None:
    try:
        num, _, den = text.partition("/")
        if not den:
            return float(num)
        d = float(den)
        return float(num) / d if d else None
    except ValueError:
        return None


def probe(path: Path) -> ProbeResult:
    binary = find_binary("ffprobe")
    ext = path.suffix.lower().lstrip(".") if hasattr(path, "suffix") else ""
    cmd = [binary, "-v", "error"]
    if ext:
        cmd += ["-f", ext]
    cmd += [
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaToolError(f"ffprobe failed for {path}: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise MediaToolError(f"ffprobe exited with {proc.returncode} for {path}: {detail[-500:]}")
    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration_ms: int | None = None
    raw_duration = fmt.get("duration") or (vstream or astream or {}).get("duration")
    if raw_duration is not None:
        try:
            duration_ms = round(float(raw_duration) * 1000)
        except (TypeError, ValueError):
            duration_ms = None

    width = int(vstream["width"]) if vstream and vstream.get("width") else None
    height = int(vstream["height"]) if vstream and vstream.get("height") else None
    fps = _parse_fraction(str(vstream.get("r_frame_rate", ""))) if vstream else None
    audio_channels = int(astream["channels"]) if astream and astream.get("channels") else None

    if vstream is not None:
        # single-frame videos are still images on disk (png); ffprobe reports
        # them as video streams with nb_frames == 1
        nb_frames = vstream.get("nb_frames")
        if str(nb_frames) == "1":
            kind = "image"
        else:
            kind = "video"
    elif astream is not None:
        kind = "audio"
    else:
        kind = "unknown"
    return ProbeResult(
        kind=kind,
        duration_ms=duration_ms,
        width=width,
        height=height,
        fps=fps,
        has_audio=astream is not None,
        audio_channels=audio_channels,
    )
