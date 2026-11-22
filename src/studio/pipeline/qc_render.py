"""Render QC (Phase 5 §J, stage S12) — measured on the final MP4 only.

Every number here comes from the *muxed* file: the AAC is decoded and the video
is decoded frame-by-frame. Nothing is copied from S7b's mix QC or S11's report;
those are read as an *upstream aggregate* (one check), never trusted as
evidence for the render.

Checks (fail-fast at the stage, ``pass|warn|fail`` per check):

======  ===========================================================  =======
#       check                                                      level
======  ===========================================================  =======
1       non-empty, ffprobe-parseable, MP4 container                fail
2       faststart: top-level ``moov`` before ``mdat``               fail
3       exactly one h264/yuv420p/24-fps/SAR-1:1/BT.709 video and
        exactly one aac/48 kHz/2 ch audio                          fail
4       decoded frame count == Σ ``clip_frames``                    fail
5       video/audio duration and A/V start delta within tolerance   fail
6       integrated LUFS, sample peak, true peak on the muxed AAC    warn/fail
7       blackdetect / freezedetect / first+last frame luma          warn
8       upstream QC aggregate (S3b tashkeel, S7b mix, S10 clips)    warn
9       preset limits (duration, bitrate, title/description/tags)   warn
======  ===========================================================  =======

§J's true peak is deliberately re-measured: AAC can overshoot the master by
about 0.5 dB, so a master that passed S7b can still fail here. The fix advice
is "lower the mastering true-peak target and re-run S7b" — S11/S12 never apply
a silent gain change.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from studio.audio.analysis import integrated_loudness
from studio.config.presets import ExportPreset
from studio.domain.errors import MediaToolError
from studio.video.analysis import (
    parse_blackdetect,
    parse_freezedetect,
    parse_signalstats,
    probe_frames,
)
from studio.video.ffmpeg import run_ffmpeg, run_ffprobe

CheckStatus = Literal["pass", "warn", "fail"]
RENDER_QC_VERSION = "1"

# Human-readable remediation per failing check (§J "fix advice").
_FIX_HINTS = {
    "container": "S11 must mux to MP4 (+faststart); rebuild with `studio render rebuild`.",
    "faststart": "S11 muxes with +faststart; rebuild with `studio render rebuild`.",
    "streams": "the preset, encoder and mux maps disagree; rebuild with "
    "`studio render rebuild`.",
    "frame_count": "segment retiming lost frames; rebuild with `studio render rebuild`.",
    "duration": "the master does not match the timeline; re-run S7b then rebuild.",
    "av_sync": "audio and video drifted; re-run S7b, then `studio render rebuild`.",
    "loudness": "re-run S7b with a corrected mastering target; S12 never adds gain.",
    "true_peak": "lower the mastering true-peak target and re-run S7b.",
    "clip_peak": "lower the mastering ceiling and re-run S7b.",
}


@dataclass(frozen=True)
class RenderQcCheck:
    name: str
    status: CheckStatus
    value: Any
    threshold: Any = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "value": self.value,
            "threshold": self.threshold,
        }
        if self.detail:
            out["detail"] = self.detail
        if self.status == "fail" and self.name in _FIX_HINTS:
            out["fix"] = _FIX_HINTS[self.name]
        return out


@dataclass
class RenderQcReport:
    checks: list[RenderQcCheck] = field(default_factory=list)
    streams: dict[str, Any] = field(default_factory=dict)
    upstream: dict[str, Any] = field(default_factory=dict)
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> CheckStatus:
        statuses = {check.status for check in self.checks}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        return "pass"

    def failures(self) -> list[RenderQcCheck]:
        return [check for check in self.checks if check.status == "fail"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "qc_version": RENDER_QC_VERSION,
            "status": self.status,
            "checks": [check.as_dict() for check in self.checks],
            "streams": self.streams,
            "upstream": self.upstream,
            "measured": self.measured,
            "failed": [check.name for check in self.failures()],
        }


# --------------------------------------------------------------------------
# 1-2. Container: top-level ISO-BMFF box scan (stdlib only)
# --------------------------------------------------------------------------


def top_level_boxes(path: Path) -> list[tuple[str, int, int]]:
    """``[(type, offset, size)]`` for the top-level boxes of an MP4.

    Handles 32-bit sizes, 64-bit sizes (``size == 1`` + ``largesize``) and
    "rest of file" boxes (``size == 0``). A truncated or non-MP4 file raises
    ``MediaToolError`` rather than returning a half-parsed list.
    """
    boxes: list[tuple[str, int, int]] = []
    total = path.stat().st_size
    with path.open("rb") as handle:
        offset = 0
        while offset + 8 <= total:
            handle.seek(offset)
            header = handle.read(8)
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header)
            name = kind.decode("latin-1")
            if size == 1:
                large = handle.read(8)
                if len(large) < 8:
                    raise MediaToolError(f"truncated 64-bit box header in {path}")
                (size,) = struct.unpack(">Q", large)
            elif size == 0:
                size = total - offset
            if size < 8 or offset + size > total:
                raise MediaToolError(
                    f"invalid box {name!r} at offset {offset} (size {size}) in {path}"
                )
            boxes.append((name, offset, size))
            offset += size
    if not boxes:
        raise MediaToolError(f"no ISO-BMFF boxes in {path}")
    return boxes


def faststart_ok(boxes: list[tuple[str, int, int]]) -> bool:
    names = [name for name, _offset, _size in boxes]
    if "moov" not in names or "mdat" not in names:
        return False
    return names.index("moov") < names.index("mdat")


# --------------------------------------------------------------------------
# Stream probing
# --------------------------------------------------------------------------

_VIDEO_OK = {"codec_name": "h264", "pix_fmt": "yuv420p"}


def _parse_rational(text: str | None) -> float | None:
    if not text:
        return None
    num, _, den = text.partition("/")
    try:
        return float(num) / float(den) if den else float(num)
    except ValueError:
        return None


def _is_bt709(stream: dict[str, Any]) -> bool:
    return (
        stream.get("color_primaries") == "bt709"
        and stream.get("color_transfer") == "bt709"
        and stream.get("color_space") == "bt709"
    )


def _sar_ok(sample_aspect_ratio: str | None) -> bool:
    return sample_aspect_ratio in (None, "", "1:1", "0:1")


async def probe_streams(path: Path) -> dict[str, Any]:
    stdout, _stderr = await run_ffprobe(
        [
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    try:
        parsed: dict[str, Any] = json.loads(stdout.decode("utf-8"))
        return parsed
    except (ValueError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        raise MediaToolError(f"unparseable ffprobe output for {path}: {exc}") from exc


# --------------------------------------------------------------------------
# 8-9. Upstream aggregate and preset limits
# --------------------------------------------------------------------------

_UPSTREAM_STAGES = ("s3b_tashkeel", "s7b_audio_mix", "s10_clips")


def _stream_qc_status(output: dict[str, Any]) -> str | None:
    for key in ("qc_status", "status", "summary"):
        value = output.get(key)
        if isinstance(value, str) and value in ("pass", "warn", "fail"):
            return value
    qc = output.get("qc")
    if isinstance(qc, dict):
        value = qc.get("status")
        if isinstance(value, str):
            return value
    return None


def upstream_aggregate(ctx: Any, run_id: str, *, track_id: str | None) -> dict[str, Any]:
    """Read — never recompute — the QC verdicts of the upstream free stages."""
    out: dict[str, Any] = {}
    for stage in _UPSTREAM_STAGES:
        rows = [
            row
            for row in ctx.repos.pipeline.list_stage_runs(run_id, stage=stage)
            if row.status == "succeeded" and row.output_json
        ]
        if not rows:
            out[stage] = {"status": "missing", "detail": "stage did not run"}
            continue
        statuses = []
        for row in rows:
            try:
                payload = json.loads(row.output_json)
            except ValueError:  # pragma: no cover - defensively tolerant
                statuses.append("unknown")
                continue
            statuses.append(_stream_qc_status(payload) or "unknown")
        worst = "fail" if "fail" in statuses else "warn" if "warn" in statuses else (
            "unknown" if "unknown" in statuses else "pass"
        )
        out[stage] = {"status": worst, "per_subject": statuses}
    return out


def _metadata_limits(
    preset: ExportPreset, metadata: dict[str, Any] | None
) -> list[RenderQcCheck]:
    checks: list[RenderQcCheck] = []
    limits = preset.limits
    if metadata is None:
        return checks
    title = metadata.get("title")
    if limits.title_max_chars is not None and isinstance(title, str):
        checks.append(
            RenderQcCheck(
                name="title_limit",
                status="warn" if len(title) > limits.title_max_chars else "pass",
                value=len(title),
                threshold=limits.title_max_chars,
                detail="⚠ unverified platform limit",
            )
        )
    description = metadata.get("description")
    if limits.description_max_bytes is not None and isinstance(description, str):
        size = len(description.encode("utf-8"))
        checks.append(
            RenderQcCheck(
                name="description_limit",
                status="warn" if size > limits.description_max_bytes else "pass",
                value=size,
                threshold=limits.description_max_bytes,
                detail="⚠ unverified platform limit",
            )
        )
    hashtags = metadata.get("hashtags")
    if limits.hashtags_max is not None and isinstance(hashtags, list):
        checks.append(
            RenderQcCheck(
                name="hashtags_limit",
                status="warn" if len(hashtags) > limits.hashtags_max else "pass",
                value=len(hashtags),
                threshold=limits.hashtags_max,
                detail="⚠ unverified platform limit",
            )
        )
    return checks


# --------------------------------------------------------------------------
# The full S12 check list
# --------------------------------------------------------------------------


async def run_render_qc(
    path: Path,
    *,
    settings: Any,
    preset: ExportPreset,
    expected_frames: int,
    master_sha256: str | None = None,
    upstream: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    work_dir: Path | None = None,
) -> RenderQcReport:
    """Measure ``path`` (S11's render) and return the report.

    A missing/unreadable file is a ``fail`` check, never an exception: the stage
    must persist a QC report it can show the operator.
    """
    cfg = settings.qc.render
    fps = int(preset.fps or settings.render.fps)
    report = RenderQcReport(upstream=upstream or {})
    checks = report.checks

    size = await asyncio.to_thread(lambda: path.stat().st_size if path.exists() else 0)
    if size == 0:
        checks.append(
            RenderQcCheck("non_empty", "fail", 0, "> 0 bytes", "render asset missing")
        )
        return report
    checks.append(RenderQcCheck("non_empty", "pass", size, "> 0 bytes"))

    # 1. MP4 container + 2. faststart.
    container_ok = path.suffix.lower() == ".mp4"
    boxes: list[tuple[str, int, int]] = []
    if container_ok:
        try:
            boxes = top_level_boxes(path)
        except (MediaToolError, OSError) as exc:
            container_ok = False
            detail = str(exc)
            checks.append(RenderQcCheck("container", "fail", path.suffix, ".mp4", detail))
    if container_ok:
        checks.append(RenderQcCheck("container", "pass", ".mp4", ".mp4"))
        faststart = faststart_ok(boxes)
        checks.append(
            RenderQcCheck(
                "faststart",
                "pass" if faststart else "fail",
                [name for name, _o, _s in boxes],
                "moov before mdat",
            )
        )
    if not container_ok:
        checks.append(RenderQcCheck("faststart", "fail", None, "moov before mdat"))
        return report

    # 3. Streams — exactly one video and one audio, with the preset's geometry.
    try:
        probed = await probe_streams(path)
    except MediaToolError as exc:
        checks.append(RenderQcCheck("streams", "fail", "unprobeable", "ffprobe", str(exc)))
        return report
    streams = probed.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    problems: list[str] = []
    if len(video) != 1:
        problems.append(f"{len(video)} video streams (want exactly 1)")
    if len(audio) != 1:
        problems.append(f"{len(audio)} audio streams (want exactly 1)")
    if video:
        v = video[0]
        if v.get("codec_name") != _VIDEO_OK["codec_name"]:
            problems.append(f"video codec {v.get('codec_name')!r} (want h264)")
        if v.get("pix_fmt") != _VIDEO_OK["pix_fmt"]:
            problems.append(f"pix_fmt {v.get('pix_fmt')!r} (want yuv420p)")
        if (v.get("width"), v.get("height")) != (preset.width, preset.height):
            problems.append(
                f"geometry {v.get('width')}x{v.get('height')} "
                f"(want {preset.width}x{preset.height})"
            )
        if not _sar_ok(v.get("sample_aspect_ratio")):
            problems.append(f"SAR {v.get('sample_aspect_ratio')!r} (want 1:1)")
        rate = _parse_rational(v.get("avg_frame_rate") or v.get("r_frame_rate"))
        if rate is None or abs(rate - fps) > 0.01:
            problems.append(f"fps {rate} (want {fps})")
        if not _is_bt709(v):
            problems.append("colour tags are not BT.709")
    if audio:
        a = audio[0]
        if a.get("codec_name") != "aac":
            problems.append(f"audio codec {a.get('codec_name')!r} (want aac)")
        if int(a.get("sample_rate") or 0) != int(preset.audio.sample_rate):
            problems.append(
                f"sample rate {a.get('sample_rate')} (want {preset.audio.sample_rate})"
            )
        if int(a.get("channels") or 0) != int(preset.audio.channels):
            problems.append(f"channels {a.get('channels')} (want {preset.audio.channels})")
    checks.append(
        RenderQcCheck(
            "streams",
            "pass" if not problems else "fail",
            {
                "video": [
                    {k: v.get(k) for k in ("codec_name", "pix_fmt", "width", "height",
                                            "avg_frame_rate", "sample_aspect_ratio")}
                    for v in video
                ],
                "audio": [
                    {k: a.get(k) for k in ("codec_name", "sample_rate", "channels")}
                    for a in audio
                ],
            },
            "1 h264/yuv420p video + 1 aac audio at the preset geometry",
            "; ".join(problems) or None,
        )
    )
    report.streams = checks[-1].as_dict()

    video_duration = _parse_rational(video[0].get("duration")) if video else None
    audio_duration = _parse_rational(audio[0].get("duration")) if audio else None
    video_start = _parse_rational(video[0].get("start_time")) if video else None
    audio_start = _parse_rational(audio[0].get("start_time")) if audio else None
    expected_duration = expected_frames / fps
    report.measured = {
        "expected_frames": expected_frames,
        "expected_duration_s": round(expected_duration, 6),
        "video_duration_s": video_duration,
        "audio_duration_s": audio_duration,
        "video_start_s": video_start,
        "audio_start_s": audio_start,
        "bytes": size,
        "master_sha256": master_sha256,
    }

    # 4. Decoded frame count is the only frame evidence.
    try:
        info = await probe_frames(path)
        decoded = info.frame_count
    except MediaToolError as exc:
        checks.append(RenderQcCheck("frame_count", "fail", None, expected_frames, str(exc)))
        decoded = None
    if decoded is not None:
        checks.append(
            RenderQcCheck(
                "frame_count",
                "pass" if decoded == expected_frames else "fail",
                decoded,
                expected_frames,
                None if decoded == expected_frames else (
                    f"off by {decoded - expected_frames} frame(s)"
                ),
            )
        )

    # 5. Durations and A/V start delta.
    tolerance = cfg.duration_tolerance_frames / fps
    if video_duration is not None:
        drift = abs(video_duration - expected_duration)
        checks.append(
            RenderQcCheck(
                "duration",
                "pass" if drift <= tolerance + 1e-6 else "fail",
                round(video_duration, 6),
                round(expected_duration, 6),
                None if drift <= tolerance + 1e-6 else (
                    f"off by {drift * 1000:.1f} ms (tolerance {tolerance * 1000:.1f} ms)"
                ),
            )
        )
    if video_start is not None and audio_start is not None:
        delta_ms = abs(audio_start - video_start) * 1000.0
        if delta_ms > settings.assemble.av_sync.fail_ms:
            status: CheckStatus = "fail"
        elif delta_ms > settings.assemble.av_sync.warn_ms:
            status = "warn"
        else:
            status = "pass"
        checks.append(
            RenderQcCheck(
                "av_sync",
                status,
                round(delta_ms, 3),
                {
                    "warn_ms": settings.assemble.av_sync.warn_ms,
                    "fail_ms": settings.assemble.av_sync.fail_ms,
                },
                "⚠ uncalibrated thresholds",
            )
        )
    if video_duration is not None and audio_duration is not None:
        gap_ms = abs(video_duration - audio_duration) * 1000.0
        checks.append(
            RenderQcCheck(
                "av_duration",
                "fail" if gap_ms > settings.assemble.av_sync.fail_ms else "pass",
                round(gap_ms, 3),
                settings.assemble.av_sync.fail_ms,
            )
        )

    # 6. Loudness on the muxed AAC (never copied from S7b).
    try:
        loudness = await integrated_loudness(str(path))
    except MediaToolError as exc:
        checks.append(RenderQcCheck("loudness", "fail", None, "ebur128", str(exc)))
    else:
        tp_value = loudness.true_peak_dbtp
        target = float(settings.audio.master.target_lufs)
        tolerance_lufs = float(settings.audio.master.lufs_tolerance)
        lufs = loudness.integrated_lufs
        report.measured["lufs"] = lufs
        report.measured["true_peak_dbtp"] = tp_value
        checks.append(
            RenderQcCheck(
                "loudness",
                "pass" if lufs is not None and abs(lufs - target) <= tolerance_lufs else "fail",
                lufs,
                {"target_lufs": target, "tolerance": tolerance_lufs},
            )
        )
        tp = loudness.true_peak_dbtp
        report.measured["measured_on"] = "muxed_aac"
        if tp is None:
            checks.append(RenderQcCheck("true_peak", "fail", None, cfg.true_peak_fail_db))
        elif tp > cfg.true_peak_fail_db:
            checks.append(
                RenderQcCheck(
                    "true_peak",
                    "fail",
                    tp,
                    {"warn_db": cfg.true_peak_warn_db, "fail_db": cfg.true_peak_fail_db},
                    "AAC overshoot is expected; the ceiling belongs to S7b",
                )
            )
        elif tp > cfg.true_peak_warn_db:
            checks.append(
                RenderQcCheck("true_peak", "warn", tp, cfg.true_peak_warn_db)
            )
        else:
            checks.append(RenderQcCheck("true_peak", "pass", tp, cfg.true_peak_warn_db))

    # 7. Video scan (warn-level at render scope).
    if cfg.video_scan:
        checks.extend(
            await _video_scan(path, duration=video_duration or expected_duration)
        )

    # 8. Upstream aggregate (read, not recomputed).
    if report.upstream:
        upstream_statuses = {v.get("status") for v in report.upstream.values()}
        upstream_status: CheckStatus = (
            "warn" if upstream_statuses - {"pass"} else "pass"
        )
        checks.append(
            RenderQcCheck(
                "upstream_qc",
                upstream_status,
                report.upstream,
                "all upstream QC passed",
                None
                if upstream_status == "pass"
                else "upstream QC is aggregated, not recomputed at render scope",
            )
        )

    # 9. Preset limits (⚠ warn-only).
    if video_duration is not None and preset.limits.max_duration_s is not None:
        checks.append(
            RenderQcCheck(
                "duration_limit",
                "warn" if video_duration > preset.limits.max_duration_s else "pass",
                round(video_duration, 3),
                preset.limits.max_duration_s,
                "⚠ unverified platform limit",
            )
        )
    if video_duration:
        measured_kbps = (size * 8 / 1000.0) / video_duration
        checks.append(
            RenderQcCheck(
                "bitrate_limit",
                "warn" if measured_kbps > preset.video.maxrate_k else "pass",
                round(measured_kbps, 1),
                preset.video.maxrate_k,
                "⚠ unwritten ceiling; maxrate is a hint",
            )
        )
    checks.extend(_metadata_limits(preset, metadata))

    return report


async def _video_scan(path: Path, *, duration: float) -> list[RenderQcCheck]:
    """blackdetect + freezedetect + first/last frame luma; all warn-level."""
    checks: list[RenderQcCheck] = []
    try:
        _out, stderr = await run_ffmpeg(
            ["-v", "info", "-i", str(path), "-vf", "blackdetect=d=0.5:pic_th=0.98",
             "-an", "-f", "null", "-"],
            timeout_s=300.0,
        )
    except MediaToolError as exc:
        checks.append(RenderQcCheck("black_sections", "warn", None, None, str(exc)))
    else:
        black_s = parse_blackdetect(stderr.decode("utf-8", errors="replace"))
        checks.append(
            RenderQcCheck(
                "black_sections",
                "warn" if black_s > 0 else "pass",
                round(black_s, 3),
                0.0,
                "black intervals (warn-level at render scope)",
            )
        )
    try:
        _out, stderr = await run_ffmpeg(
            ["-v", "info", "-i", str(path), "-vf", "freezedetect=n=-60dB:d=2",
             "-an", "-f", "null", "-"],
            timeout_s=300.0,
        )
    except MediaToolError as exc:
        checks.append(RenderQcCheck("frozen_sections", "warn", None, None, str(exc)))
    else:
        freeze_s = parse_freezedetect(
            stderr.decode("utf-8", errors="replace"), total_duration_s=duration
        )
        checks.append(
            RenderQcCheck(
                "frozen_sections",
                "warn" if freeze_s > 0 else "pass",
                round(freeze_s, 3),
                0.0,
            )
        )
    first = await _frame_luma(path, position=0.0)
    last = await _frame_luma(path, position=max(duration - 1.0 / 24.0, 0.0))
    for name, luma, position in (
        ("first_frame_black", first, "first"),
        ("last_frame_black", last, "last"),
    ):
        if luma is None:
            checks.append(
                RenderQcCheck(name, "warn", None, "> 0 luma", f"{position} frame unreadable")
            )
        else:
            checks.append(
                RenderQcCheck(
                    name,
                    "warn" if luma <= 0.5 else "pass",
                    round(luma, 3),
                    "> 0 luma",
                )
            )
    return checks


async def _frame_luma(path: Path, *, position: float) -> float | None:
    args = ["-v", "info", "-ss", f"{position:.6f}", "-i", str(path), "-frames:v", "1",
            "-vf", "signalstats,metadata=print", "-an", "-f", "null", "-"]
    try:
        _out, stderr = await run_ffmpeg(args, timeout_s=120.0)
    except MediaToolError:
        return None
    parsed = parse_signalstats(stderr.decode("utf-8", errors="replace"))
    return parsed[0] if parsed else None


async def write_qc_report(
    report: RenderQcReport, *, path: Path, extra: dict[str, Any] | None = None
) -> Path:
    """Persist the report next to the render (never inside the CAS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.as_dict()
    if extra:
        payload.update(extra)
    await asyncio.to_thread(path.write_text, json.dumps(payload, indent=2, sort_keys=True))
    return path
