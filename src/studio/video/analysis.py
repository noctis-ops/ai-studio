"""Pure parsers for ffmpeg/ffprobe QC output (Phase 4 §K).

Every function here is deterministic and unit-tested without invoking ffmpeg
(T-QCV-01): the ffmpeg-facing code in ``qc/visual.py`` only collects stderr /
JSON and hands it to these parsers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_SIGNALSTATS_LEGACY_RE = re.compile(r"YAVG:\s*([0-9.]+).*?YSTD:\s*([0-9.]+)", re.DOTALL)
_SIGNALSTATS_KV_RE = re.compile(r"lavfi\.signalstats\.([A-Z0-9]+)=([0-9.]+)")
_BLACKDETECT_RE = re.compile(
    r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)"
)
_FREEZEDETECT_RE = re.compile(
    r"freeze_start:\s*([0-9.]+).*?freeze_duration:\s*([0-9.]+)", re.DOTALL
)
_SSIM_RE = re.compile(
    r"SSIM Y:([0-9.]+)(?:\s*\([^)]*\))?\s+U:([0-9.]+)(?:\s*\([^)]*\))?\s+"
    r"V:([0-9.]+)(?:\s*\([^)]*\))?\s+All:([0-9.]+)"
)


@dataclass(frozen=True)
class ProbeInfo:
    width: int | None
    height: int | None
    fps: float | None
    frame_count: int | None
    duration_s: float | None
    has_audio: bool
    codec: str | None


def parse_signalstats(stderr: str) -> tuple[float, float] | None:
    """(mean luma, luma spread) from the signalstats output, or None.

    ffmpeg >= 9 prints stats as frame metadata (`lavfi.signalstats.*`,
    collected with `signalstats,metadata=print`); older builds logged
    `YAVG: ... YSTD: ...` directly. The legacy path returns the luma stddev;
    the metadata path returns YMAX - YMIN as the spread measure (0 for a
    uniform/blank frame, which is what the not-black check needs).
    """
    legacy = _SIGNALSTATS_LEGACY_RE.findall(stderr)
    if legacy:
        mean, stddev = legacy[-1]
        return float(mean), float(stddev)
    kv: dict[str, float] = {}
    for key, value in _SIGNALSTATS_KV_RE.findall(stderr):
        kv[key] = float(value)
    if "YAVG" not in kv:
        return None
    high, low = kv.get("YMAX"), kv.get("YMIN")
    spread = (high - low) if high is not None and low is not None else 0.0
    return kv["YAVG"], spread


def parse_blackdetect(stderr: str) -> float:
    """Total black duration in seconds."""
    return sum(float(m[2]) for m in _BLACKDETECT_RE.findall(stderr))


def parse_freezedetect(stderr: str, *, total_duration_s: float | None = None) -> float:
    """Total frozen duration in seconds.

    ffmpeg prints ``freeze_start`` + ``freeze_duration`` + ``freeze_end`` for
    freezes that end before EOF; a freeze running to the end of the stream
    prints only ``freeze_start``. When ``total_duration_s`` is given, a
    trailing unterminated freeze is counted as frozen until EOF.
    """
    total = sum(float(m[1]) for m in _FREEZEDETECT_RE.findall(stderr))
    if total_duration_s is not None:
        starts = len(re.findall(r"freeze_start:", stderr))
        ends = len(_FREEZEDETECT_RE.findall(stderr))
        if starts > ends:
            tail_start = float(
                re.findall(r"freeze_start:\s*([0-9.]+)", stderr)[-1]
            )
            total += max(0.0, total_duration_s - tail_start)
    return total


_SSIM_COMPONENTS = {"Y": 0, "U": 1, "V": 2, "All": 3}


def parse_ssim(stderr: str, component: str = "All") -> float | None:
    """SSIM component (Y, U, V or All) from the last ssim summary, or None."""
    index = _SSIM_COMPONENTS.get(component)
    if index is None:
        raise ValueError(f"unknown ssim component {component!r}")
    matches = _SSIM_RE.findall(stderr)
    if not matches:
        return None
    return float(matches[-1][index])


def _parse_fraction(text: str) -> float | None:
    try:
        num, _, den = text.partition("/")
        if not den:
            return float(num)
        d = float(den)
        return float(num) / d if d else None
    except ValueError:
        return None


def parse_ffprobe_json(raw: str | bytes) -> ProbeInfo:
    """ProbeInfo from `ffprobe -print_format json -show_streams -show_format`."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    data: dict[str, Any] = json.loads(text)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    width = int(video["width"]) if video and video.get("width") else None
    height = int(video["height"]) if video and video.get("height") else None
    if video:
        raw_fps = video.get("avg_frame_rate") or video.get("r_frame_rate") or ""
        fps = _parse_fraction(str(raw_fps))
    else:
        fps = None
    frames = None
    if video is not None:
        for key in ("nb_read_frames", "nb_frames"):
            if video.get(key) is not None:
                try:
                    frames = int(video[key])
                except (TypeError, ValueError):
                    frames = None
                break
    duration_s = None
    raw_duration = fmt.get("duration") or (video or {}).get("duration")
    if raw_duration is not None:
        try:
            duration_s = float(raw_duration)
        except (TypeError, ValueError):
            duration_s = None
    codec = str(video.get("codec_name")) if video and video.get("codec_name") else None
    return ProbeInfo(
        width=width,
        height=height,
        fps=fps,
        frame_count=frames,
        duration_s=duration_s,
        has_audio=has_audio,
        codec=codec,
    )
