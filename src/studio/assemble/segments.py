"""Segmented render primitive (Phase 5 §E5, design D1).

A 20-minute episode is ~240 clips. A single ffmpeg graph with 240 inputs is
forbidden past ``assemble.max_inputs_per_graph`` (24): it is slow to build,
memory-hungry and a single bad input fails the whole render. Instead:

1. each shot becomes a **segment** (geometry plan + ``-an`` + an explicit
   ``-frames:v clip_frames`` + the final encoder profile),
2. every segment is verified by decode count and gets an ``.ok.json`` sidecar,
3. segments are joined by the concat demuxer with ``-c copy`` in chunks of at
   most ``max_inputs_per_graph`` inputs,
4. the codec-copied video is muxed with the AAC audio into MP4.

A verified segment is reused across a crash (the sidecar binds it to the input
sha, the frame count and the encoder profile hash), so resume re-renders only
what actually failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.assemble.paths import write_concat_list
from studio.domain.errors import MediaToolError
from studio.video.encoders import ASSEMBLER_VERSION, EncoderProfile
from studio.video.ffmpeg import run_ffmpeg
from studio.video.geometry import FitPlan


@dataclass(frozen=True)
class SegmentSpec:
    index: int
    shot_id: str
    src: Path
    clip_frames: int
    dst_w: int
    dst_h: int
    input_sha: str
    # S6's locked retiming: the conformed clip has `clip_frames` frames, the
    # timeline expects ceil(clip_frames / speed) + hold frames.
    speed_factor: float = 1.0
    hold_frames: int = 0

    def stem(self) -> str:
        return f"seg-{self.index:05d}"


@dataclass(frozen=True)
class SegmentResult:
    path: Path
    frames: int
    reused: bool
    report: dict[str, Any]


def segment_path(root: Path, spec: SegmentSpec, container: str) -> Path:
    suffix = ".mkv" if container == "mkv" else ".ts"
    return root / f"{spec.stem()}{suffix}"


def sidecar_path(segment: Path) -> Path:
    return segment.with_name(segment.name + ".ok.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_sidecar(segment: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(sidecar_path(segment).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def sidecar_matches(
    *,
    sidecar: dict[str, Any] | None,
    spec: SegmentSpec,
    profile: EncoderProfile,
    container: str,
) -> bool:
    if sidecar is None:
        return False
    return (
        sidecar.get("frames") == spec.clip_frames
        and sidecar.get("input_sha") == spec.input_sha
        and sidecar.get("profile_hash") == profile.profile_hash
        and sidecar.get("assembler_version") == ASSEMBLER_VERSION
        and sidecar.get("container") == container
        and sidecar.get("shot_id") == spec.shot_id
        and float(sidecar.get("speed_factor", 1.0)) == float(spec.speed_factor)
        and int(sidecar.get("hold_frames", 0)) == int(spec.hold_frames)
    )


def segment_filter(spec: SegmentSpec, plan: FitPlan, *, fps: int) -> str:
    """Scale/crop, S6 retiming, then the hold as cloned final frames."""
    chain = plan.filter_chain()
    if abs(float(spec.speed_factor) - 1.0) > 1e-9:
        # Retime, then resample to CFR with round=up: ceil(clip_frames/speed)
        # is exactly what S6 locked, and fractional timestamps would otherwise
        # drop the final frame (`round=down` produced N-1).
        chain = (
            f"setpts=(PTS-STARTPTS)/{float(spec.speed_factor):.9f},"
            f"fps={fps}:round=up," + chain
        )
    if int(spec.hold_frames) > 0:
        # `stop` counts frames; a duration would round to the wrong frame count.
        chain += f",tpad=stop_mode=clone:stop={int(spec.hold_frames)}"
    return chain


def segment_args(
    spec: SegmentSpec, plan: FitPlan, profile: EncoderProfile, *, fps: int
) -> list[str]:
    return [
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(spec.src),
        "-vf", segment_filter(spec, plan, fps=fps),
        "-an",
        "-frames:v", str(spec.clip_frames),
        "-r", str(fps),
        *profile.args,
        "-map_metadata", "-1",
    ]


async def render_segment(
    spec: SegmentSpec,
    *,
    plan: FitPlan,
    profile: EncoderProfile,
    fps: int,
    root: Path,
    container: str = "mkv",
    reuse: bool = True,
    timeout_s: float = 300.0,
) -> SegmentResult:
    out = segment_path(root, spec, container)
    if reuse and out.exists() and sidecar_matches(
        sidecar=load_sidecar(out), spec=spec, profile=profile, container=container
    ):
        sidecar = load_sidecar(out) or {}
        return SegmentResult(
            path=out,
            frames=int(sidecar.get("frames", spec.clip_frames)),
            reused=True,
            report={"geometry": plan.report(), "reused": True, "source_sha": spec.input_sha},
        )
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    sidecar_path(out).unlink(missing_ok=True)
    args = [*segment_args(spec, plan, profile, fps=fps), str(out)]
    await run_ffmpeg(args, timeout_s=timeout_s)
    frames = await _count_frames(out)
    if frames != spec.clip_frames:
        raise MediaToolError(
            f"segment {spec.shot_id} has {frames} frames, expected {spec.clip_frames}"
        )
    sidecar = {
        "shot_id": spec.shot_id,
        "index": spec.index,
        "frames": frames,
        "input_sha": spec.input_sha,
        "profile_hash": profile.profile_hash,
        "assembler_version": ASSEMBLER_VERSION,
        "container": container,
        "geometry": plan.report(),
        "speed_factor": float(spec.speed_factor),
        "hold_frames": int(spec.hold_frames),
    }
    sidecar_path(out).write_text(json.dumps(sidecar, indent=2, sort_keys=True), encoding="utf-8")
    return SegmentResult(
        path=out,
        frames=frames,
        reused=False,
        report={**sidecar, "reused": False},
    )


async def _count_frames(path: Path) -> int | None:
    from studio.video.analysis import probe_frames

    try:
        return (await probe_frames(path)).frame_count
    except MediaToolError:
        return None


async def render_still_segment(
    *,
    src: Path,
    out: Path,
    frames: int,
    plan: FitPlan,
    profile: EncoderProfile,
    fps: int,
    timeout_s: float = 300.0,
) -> Path:
    """Hold a still image for exactly ``frames`` frames (animatic, S9).

    Still inputs are RGB, and swscale's RGB->YUV default is not BT.709, so the
    scale filter carries ``out_color_matrix=bt709`` (§E6 ⚠). No drawtext.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-loop", "1",
        "-framerate", str(fps),
        "-i", str(src),
        "-vf", plan.filter_chain(out_color_matrix="bt709"),
        "-an",
        "-frames:v", str(frames),
        "-r", str(fps),
        *profile.args,
        "-map_metadata", "-1",
        str(out),
    ]
    await run_ffmpeg(args, timeout_s=timeout_s)
    count = await frame_count(out)
    if count != frames:
        raise MediaToolError(f"still segment {out.name} has {count} frames, expected {frames}")
    return out


async def _concat_once(parts: list[Path], out: Path, *, work_dir: Path, timeout_s: float) -> None:
    """Codec-copy concat of <= max_inputs_per_graph parts into ``out``."""
    await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
    list_path = write_concat_list(work_dir / f"{out.stem}.concat.txt", parts)
    args = [
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        "-map_metadata", "-1",
        str(out),
    ]
    await run_ffmpeg(args, timeout_s=timeout_s)


async def join_segments(
    parts: list[Path],
    out: Path,
    *,
    max_inputs: int,
    work_dir: Path,
    timeout_s: float = 600.0,
) -> Path:
    """Join with the concat demuxer, chunked so no graph exceeds ``max_inputs``."""
    if not parts:
        raise MediaToolError("cannot join zero segments")
    if max_inputs < 2:
        raise MediaToolError(f"max_inputs_per_graph must be >= 2, got {max_inputs}")
    await asyncio.to_thread(out.parent.mkdir, parents=True, exist_ok=True)
    current = list(parts)
    level = 0
    while len(current) > max_inputs:
        chunked: list[Path] = []
        for start in range(0, len(current), max_inputs):
            chunk = current[start : start + max_inputs]
            if len(chunk) == 1:
                chunked.append(chunk[0])
                continue
            intermediate = work_dir / f"join-{level}-{start // max_inputs:04d}{out.suffix}"
            await _concat_once(chunk, intermediate, work_dir=work_dir, timeout_s=timeout_s)
            chunked.append(intermediate)
        current = chunked
        level += 1
    if len(current) == 1:
        await asyncio.to_thread(_replace, current[0], out)
    else:
        await _concat_once(current, out, work_dir=work_dir, timeout_s=timeout_s)
    return out


def _replace(src: Path, dst: Path) -> None:
    src.replace(dst)


async def mux_audio(
    video: Path,
    audio: Path,
    out: Path,
    *,
    audio_bitrate_k: int,
    sample_rate: int,
    channels: int,
    timeout_s: float = 600.0,
) -> Path:
    """Mux the mastered audio into the joined video. No audio filters beyond fmt."""
    await asyncio.to_thread(out.parent.mkdir, parents=True, exist_ok=True)
    args = [
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0",
        "-map", "1:a:0",
        # `-c:v copy` keeps the segment's colour *matrix* but the MP4 colr atom
        # is built from these stream options; without them the final render has
        # an unspecified primaries/transfer pair and S12 fails §J.3.
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
        "-c:v", "copy",
        # Written into the H.264 VUI (no re-encode), so ffprobe/§J.3 see the
        # tags on the *muxed* file rather than only on the segment.
        "-bsf:v",
        "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
        "matrix_coefficients=1:video_full_range_flag=0",
        "-c:a", "aac",
        "-b:a", f"{audio_bitrate_k}k",
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-movflags", "+faststart",
        "-map_metadata", "-1",
        str(out),
    ]
    await run_ffmpeg(args, timeout_s=timeout_s)
    return out


async def frame_count(path: Path) -> int | None:
    return await _count_frames(path)
