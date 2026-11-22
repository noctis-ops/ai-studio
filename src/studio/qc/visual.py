"""Visual technical QC (Phase 4 §K): ffmpeg/ffprobe only, no visual LLM.

Each check returns ``{name, status, retryable, value, threshold}`` and the
list persists to ``shot_attempts.qc_json``. Retry classes:

- retryable: zero-byte, decode error, black/blank, extreme freeze,
  first-frame black
- non-retryable: aspect mismatch, below-minimum resolution, unsupported
  container, duration mismatch beyond tolerance
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from studio.config.settings import Settings
from studio.domain.enums import Aspect
from studio.domain.errors import MediaToolError
from studio.video.analysis import (
    parse_blackdetect,
    parse_ffprobe_json,
    parse_freezedetect,
    parse_signalstats,
    parse_ssim,
)
from studio.video.ffmpeg import run_ffmpeg, run_ffprobe

CheckStatus = Literal["pass", "warn", "fail"]


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@dataclass(frozen=True)
class QcCheck:
    name: str
    status: CheckStatus
    retryable: bool
    value: float | str | None
    threshold: float | str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "retryable": self.retryable,
            "value": self.value,
            "threshold": self.threshold,
        }


def _aspect_ratio(aspect: Aspect | str) -> float:
    value = aspect.value if isinstance(aspect, Aspect) else str(aspect)
    w, _, h = value.partition(":")
    return float(w) / float(h)


def _aspect_ok(
    width: int | None, height: int | None, aspect: Aspect | str, tolerance_pct: float
) -> bool:
    if not width or not height:
        return False
    actual = width / height
    expected = _aspect_ratio(aspect)
    return abs(actual - expected) / expected * 100.0 <= tolerance_pct


async def _probe(path: Path) -> Any:
    stdout, _ = await run_ffprobe(
        [
            "-v", "error",
            "-count_frames",
            "-show_entries",
            "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,nb_read_frames,codec_name",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        timeout_s=60.0,
    )
    return parse_ffprobe_json(stdout)


async def _signalstats(path: Path) -> tuple[float, float] | None:
    _stdout, stderr = await run_ffmpeg(
        [
            "-v", "info",
            "-i", str(path),
            "-vf", "signalstats,metadata=print",
            "-f", "null", "-",
        ],
        timeout_s=60.0,
    )
    return parse_signalstats(stderr.decode("utf-8", errors="replace"))


async def qc_image(path: Path, *, aspect: Aspect | str, settings: Settings) -> list[QcCheck]:
    """Image checks: readable, size, aspect, not black/blank."""
    cfg = settings.media.qc.image
    checks: list[QcCheck] = []
    size = await asyncio.to_thread(_file_size, path)
    checks.append(
        QcCheck(
            name="readable",
            status="pass" if size >= cfg.min_bytes else "fail",
            retryable=True,
            value=size,
            threshold=cfg.min_bytes,
        )
    )
    try:
        info = await _probe(path)
    except MediaToolError:
        checks.append(
            QcCheck(
                name="probe",
                status="fail",
                retryable=True,
                value="unprobeable",
                threshold="readable by ffprobe",
            )
        )
        return checks
    short_side = min(info.width or 0, info.height or 0)
    checks.append(
        QcCheck(
            name="min_short_side",
            status="pass" if short_side >= cfg.min_short_side_px else "fail",
            retryable=False,
            value=short_side,
            threshold=cfg.min_short_side_px,
        )
    )
    checks.append(
        QcCheck(
            name="aspect",
            status=(
                "pass"
                if _aspect_ok(info.width, info.height, aspect, cfg.aspect_tolerance_pct)
                else "fail"
            ),
            retryable=False,
            value=f"{info.width}x{info.height}" if info.width else None,
            threshold=str(aspect),
        )
    )
    try:
        stats = await _signalstats(path)
    except Exception:
        stats = None
    mean, stddev = stats if stats is not None else (0.0, 0.0)
    black = mean < cfg.min_luma_mean or stddev < cfg.min_luma_stddev
    checks.append(
        QcCheck(
            name="not_black",
            status="fail" if black else "pass",
            retryable=True,
            value=round(mean, 3),
            threshold=cfg.min_luma_mean,
        )
    )
    return checks


async def qc_video(
    path: Path,
    *,
    aspect: Aspect | str,
    settings: Settings,
    keyframe_path: Path | None = None,
    expected_frames: int | None = None,
    expected_fps: int | None = None,
) -> list[QcCheck]:
    """Video checks: decode, size, aspect, duration/frames, black, freeze,
    first-frame luma, first-frame SSIM vs the keyframe."""
    cfg = settings.media.qc.video
    checks: list[QcCheck] = []
    size = await asyncio.to_thread(_file_size, path)
    checks.append(
        QcCheck(
            name="min_bytes",
            status="pass" if size >= cfg.min_bytes else "fail",
            retryable=True,
            value=size,
            threshold=cfg.min_bytes,
        )
    )
    # Full decode with zero errors.
    decode_ok = True
    try:
        await run_ffmpeg(
            ["-v", "error", "-i", str(path), "-f", "null", "-"],
            timeout_s=120.0,
        )
    except Exception:
        decode_ok = False
    checks.append(
        QcCheck(name="decode", status="pass" if decode_ok else "fail", retryable=True,
                value=0 if decode_ok else 1, threshold=0)
    )
    if not decode_ok:
        # An undecodable asset must read as a retryable QC failure, never as
        # an unhandled crash (which would park the stage without a report).
        return checks
    try:
        info = await _probe(path)
    except MediaToolError:
        checks.append(
            QcCheck(
                name="probe",
                status="fail",
                retryable=True,
                value="unprobeable",
                threshold="readable by ffprobe",
            )
        )
        return checks
    checks.append(
        QcCheck(
            name="aspect",
            status=(
                "pass"
                if _aspect_ok(info.width, info.height, aspect, cfg.aspect_tolerance_pct)
                else "fail"
            ),
            retryable=False,
            value=f"{info.width}x{info.height}" if info.width else None,
            threshold=str(aspect),
        )
    )
    if expected_frames is not None:
        checks.append(
            QcCheck(
                name="frame_count",
                status="pass" if info.frame_count == expected_frames else "fail",
                retryable=False,
                value=info.frame_count,
                threshold=expected_frames,
            )
        )
    if expected_fps is not None and info.fps is not None:
        checks.append(
            QcCheck(
                name="fps",
                status="pass" if abs(info.fps - expected_fps) < 0.01 else "fail",
                retryable=False,
                value=round(info.fps, 3),
                threshold=expected_fps,
            )
        )
    # Black ratio via blackdetect.
    try:
        _o, stderr = await run_ffmpeg(
            [
                "-v", "info",
                "-i", str(path),
                "-vf", f"blackdetect=d={cfg.black_min_d_s}:pic_th={cfg.black_pic_th}",
                "-f", "null", "-",
            ],
            timeout_s=120.0,
        )
    except Exception:
        checks.append(
            QcCheck(name="black_ratio", status="fail", retryable=True,
                    value="blackdetect failed", threshold=cfg.black_fail_ratio)
        )
        return checks
    black_s = parse_blackdetect(stderr.decode("utf-8", errors="replace"))
    duration = info.duration_s or 0.0
    black_ratio = black_s / duration if duration > 0 else 0.0
    checks.append(
        QcCheck(
            name="black_ratio",
            status="fail" if black_ratio > cfg.black_fail_ratio else "pass",
            retryable=True,
            value=round(black_ratio, 4),
            threshold=cfg.black_fail_ratio,
        )
    )
    # Freeze ratio via freezedetect.
    try:
        _o, stderr = await run_ffmpeg(
            ["-v", "info", "-i", str(path), "-vf", "freezedetect", "-f", "null", "-"],
            timeout_s=120.0,
        )
    except Exception:
        checks.append(
            QcCheck(name="freeze_ratio", status="fail", retryable=True,
                    value="freezedetect failed", threshold=cfg.freeze_fail_ratio)
        )
        return checks
    freeze_s = parse_freezedetect(
        stderr.decode("utf-8", errors="replace"),
        total_duration_s=info.duration_s,
    )
    freeze_ratio = freeze_s / duration if duration > 0 else 0.0
    if freeze_ratio > cfg.freeze_fail_ratio:
        freeze_status: CheckStatus = "fail"
    elif freeze_ratio > cfg.freeze_warn_ratio:
        freeze_status = "warn"
    else:
        freeze_status = "pass"
    checks.append(
        QcCheck(
            name="freeze_ratio",
            status=freeze_status,
            retryable=True,
            value=round(freeze_ratio, 4),
            threshold=cfg.freeze_fail_ratio,
        )
    )
    # First-frame luma (not black).
    try:
        stats = await _signalstats(path)
    except Exception:
        stats = None
    first_mean = stats[0] if stats is not None else 0.0
    checks.append(
        QcCheck(
            name="first_frame_luma",
            status="fail" if first_mean < cfg.first_frame_black_luma else "pass",
            retryable=True,
            value=round(first_mean, 3),
            threshold=cfg.first_frame_black_luma,
        )
    )
    # First-frame SSIM against the keyframe (recorded always).
    if keyframe_path is not None and await asyncio.to_thread(keyframe_path.exists):
        try:
            ssim_value = await _ssim_first_frame(
                path, keyframe_path, width=info.width, height=info.height
            )
        except Exception:
            ssim_value = None
        if ssim_value is not None:
            if cfg.first_frame_ssim_fail > 0 and ssim_value < cfg.first_frame_ssim_fail:
                ssim_status: CheckStatus = "fail"
            elif ssim_value < cfg.first_frame_ssim_warn:
                ssim_status = "warn"
            else:
                ssim_status = "pass"
            checks.append(
                QcCheck(
                    name="first_frame_ssim",
                    status=ssim_status,
                    retryable=True,
                    value=round(ssim_value, 4),
                    threshold=cfg.first_frame_ssim_warn,
                )
            )
    checks.append(
        QcCheck(
            name="has_audio",
            status="warn" if info.has_audio else "pass",
            retryable=False,
            value=info.has_audio,
            threshold=False,
        )
    )
    return checks


async def _ssim_first_frame(
    video_path: Path, keyframe_path: Path, *, width: int, height: int
) -> float | None:
    """First-frame luma SSIM against the keyframe (H5.3: no ``scale2ref``).

    The reference is scaled to the **clip's** probed dimensions with an
    explicit ``scale=W:H`` instead of the deprecated ``scale2ref`` helper. The
    clip's own first frame is passed through unchanged (it is already WxH), so
    the measured value is unchanged.
    """
    if width <= 0 or height <= 0:
        return None
    _o, stderr = await run_ffmpeg(
        [
            "-v", "info",
            "-i", str(video_path),
            "-i", str(keyframe_path),
            "-lavfi",
            # Compare luma only: x264 quantizes 420p chroma aggressively, so
            # the chroma planes measure encoder noise, not content drift.
            "[0:v]trim=end_frame=1,setpts=PTS-STARTPTS[main];"
            f"[1:v]trim=end_frame=1,setpts=PTS-STARTPTS,scale={width}:{height}:"
            "flags=lanczos[ref];"
            "[main][ref]ssim",
            "-f", "null", "-",
        ],
        timeout_s=60.0,
    )
    return parse_ssim(stderr.decode("utf-8", errors="replace"), component="Y")


def qc_summary(checks: list[QcCheck]) -> dict[str, Any]:
    """Aggregate: fail if any fail; else warn if any warn; else pass."""
    statuses = [c.status for c in checks]
    if "fail" in statuses:
        overall: CheckStatus = "fail"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "pass"
    retryable = any(c.retryable for c in checks if c.status == "fail")
    return {
        "status": overall,
        "retryable": retryable,
        "checks": [c.as_dict() for c in checks],
    }


def checks_to_json(checks: list[QcCheck]) -> str:
    return json.dumps([c.as_dict() for c in checks], separators=(",", ":"))
