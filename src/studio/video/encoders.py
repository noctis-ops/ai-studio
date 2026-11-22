"""Encoder profiles and NVENC probing (Phase 5 §E6 / §H).

Presence in ``ffmpeg -encoders`` is **never** sufficient evidence: a container
can list ``h264_nvenc`` while the host has no NVIDIA driver (or no GPU at all).
``resolve_encoder`` therefore performs a real 24-frame probe encode at every
preset geometry and only then trusts NVENC.

``encode.encoder: nvenc`` fails closed (``EncoderUnavailable``); ``auto`` falls
back to x264 and records a scrubbed ``fallback_reason``. Results are cached,
keyed by ffmpeg version + platform + driver string + geometry, so a worker that
starts thousands of times pays for one probe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from studio.core.canonical import canonical_json
from studio.domain.errors import EncoderUnavailable, MediaToolError
from studio.video.ffmpeg import FFMPEG, find_binary

# Bumped when the assembled bytes would change for the same inputs. It is part
# of the S11 cache key, exactly like the encoder profile hash.
ASSEMBLER_VERSION = "5.0.0"

PROBE_FRAMES = 24
PROBE_FPS = 24

_COLOR_TAGS = (
    "-colorspace", "bt709",
    "-color_primaries", "bt709",
    "-color_trc", "bt709",
    "-color_range", "tv",
)
_BITEXACT = ("-fflags", "+bitexact", "-flags:a", "+bitexact")

_SCRUB_PATTERNS = (
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"fal[_-]?key[=:\s]+\S+", re.IGNORECASE), "fal_key=<redacted>"),
    (re.compile(r"[A-Za-z0-9_-]{32,}"), "<redacted>"),
)


@dataclass(frozen=True)
class EncoderProfile:
    """A frozen encoder name + argument tuple + its stable hash."""

    name: str
    args: tuple[str, ...]
    profile_hash: str

    def with_bitrate(self, maxrate_k: int, bufsize_k: int) -> EncoderProfile:
        augmented = (*self.args, "-maxrate", f"{maxrate_k}k", "-bufsize", f"{bufsize_k}k")
        return EncoderProfile(
            name=self.name, args=augmented, profile_hash=profile_hash_for(self.name, augmented)
        )

    def report(self) -> dict[str, object]:
        return {
            "name": self.name,
            "args": list(self.args),
            "profile_hash": self.profile_hash,
            "assembler_version": ASSEMBLER_VERSION,
        }


def profile_hash_for(name: str, args: Sequence[str]) -> str:
    return hashlib.sha256(
        canonical_json({"name": name, "args": list(args)})
    ).hexdigest()


def x264_profile(
    settings: Any, *, maxrate_k: int | None = None, bufsize_k: int | None = None
) -> EncoderProfile:
    args: tuple[str, ...] = (
        "-c:v", "libx264",
        "-crf", str(int(settings.encode.x264.crf)),
        "-preset", str(settings.encode.x264.preset),
        "-pix_fmt", "yuv420p",
        *_COLOR_TAGS,
        *_BITEXACT,
    )
    profile = EncoderProfile(name="x264", args=args, profile_hash=profile_hash_for("x264", args))
    if maxrate_k is not None and bufsize_k is not None:
        return profile.with_bitrate(maxrate_k, bufsize_k)
    return profile


def nvenc_profile(
    settings: Any, *, maxrate_k: int | None = None, bufsize_k: int | None = None
) -> EncoderProfile:
    """The production NVENC arguments (⚠ operator-tunable, not platform-mandated)."""
    args: tuple[str, ...] = (
        "-c:v", "h264_nvenc",
        "-preset", str(settings.encode.nvenc.preset),
        "-rc", str(settings.encode.nvenc.rc),
        "-cq", str(int(settings.encode.nvenc.cq)),
        "-b:v", "0",
        "-pix_fmt", "yuv420p",
        *_COLOR_TAGS,
        *_BITEXACT,
    )
    profile = EncoderProfile(name="nvenc", args=args, profile_hash=profile_hash_for("nvenc", args))
    if maxrate_k is not None and bufsize_k is not None:
        return profile.with_bitrate(maxrate_k, bufsize_k)
    return profile


def scrub_text(text: str) -> str:
    """Remove URLs, keys and any long opaque token from a stderr tail."""
    cleaned = text
    for pattern, replacement in _SCRUB_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned.strip()[-500:]


# ---------------------------------------------------------------------------
# Probe running (injectable: tests use a fake, production spawns ffmpeg)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeOutput:
    returncode: int
    stderr_tail: str


class ProbeRunner(Protocol):
    async def run(self, args: list[str], *, timeout_s: float) -> ProbeOutput: ...


FrameCounter = Callable[[Path], Awaitable["int | None"]]


async def decoded_frame_count(path: Path) -> int | None:
    """Default frame counter: a real decode count, never a container field."""
    from studio.video.analysis import probe_frames

    try:
        return (await probe_frames(path)).frame_count
    except MediaToolError:
        return None


class SubprocessProbeRunner:
    """Real ffmpeg probe runner. Never raises: a timeout is returncode -1."""

    async def run(self, args: list[str], *, timeout_s: float) -> ProbeOutput:
        binary = find_binary(FFMPEG)
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:  # pragma: no cover - ffmpeg presence is checked earlier
            return ProbeOutput(returncode=-1, stderr_tail=f"failed to start: {exc}")
        try:
            async with asyncio.timeout(timeout_s):
                _stdout, stderr = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ProbeOutput(returncode=-1, stderr_tail=f"probe timed out after {timeout_s}s")
        return ProbeOutput(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stderr_tail=stderr.decode("utf-8", errors="replace"),
        )





def probe_args(profile: EncoderProfile, *, width: int, height: int, out_path: Path) -> list[str]:
    """A 24-frame lavfi color source through the exact production arguments."""
    return [
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"color=c=gray:s={width}x{height}:r={PROBE_FPS}:d={PROBE_FRAMES / PROBE_FPS:.3f}",
        *profile.args,
        "-frames:v", str(PROBE_FRAMES),
        "-an",
        str(out_path),
    ]


async def probe_encoder(
    profile: EncoderProfile,
    *,
    width: int,
    height: int,
    work_dir: Path,
    runner: ProbeRunner,
    timeout_s: float,
    frame_counter: FrameCounter | None = None,
) -> tuple[bool, str]:
    """Returns (ok, scrubbed_reason). Success needs exit 0, bytes and frame count."""
    out_path = work_dir / f"probe-{profile.name}-{width}x{height}.mkv"
    await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    result = await runner.run(
        probe_args(profile, width=width, height=height, out_path=out_path), timeout_s=timeout_s
    )
    reason = scrub_text(result.stderr_tail) or f"exit {result.returncode}"
    if result.returncode != 0:
        return False, reason
    try:
        size = out_path.stat().st_size
    except OSError:
        return False, "probe produced no output file"
    if size == 0:
        return False, "probe produced an empty file"
    frames = await (frame_counter or decoded_frame_count)(out_path)
    if frames != PROBE_FRAMES:
        return False, f"probe produced {frames} frames, expected {PROBE_FRAMES}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def driver_string() -> str | None:
    """`nvidia-smi --query-gpu=driver_version` if available; absence is fine."""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, optional tool
            [executable, "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    return text.splitlines()[0] if completed.returncode == 0 and text else None


def cache_key(*, ffmpeg_version: str, geometry: tuple[int, int], driver: str | None) -> str:
    payload = {
        "ffmpeg": ffmpeg_version,
        "platform": platform.platform(),
        "driver": driver or "none",
        "geometry": f"{geometry[0]}x{geometry[1]}",
        "args_version": ASSEMBLER_VERSION,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def load_probe_cache(path: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {str(k): v for k, v in entries.items() if isinstance(v, dict)}


def save_probe_cache(path: Path, entries: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"entries": entries}, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncoderResolution:
    profile: EncoderProfile
    reason: str
    fallback_reason: str | None = None
    cache_hit: bool = False

    def report(self) -> dict[str, object]:
        return {
            "encoder": self.profile.name,
            "profile_hash": self.profile.profile_hash,
            "args": list(self.profile.args),
            "reason": self.reason,
            "fallback_reason": self.fallback_reason,
            "cache_hit": self.cache_hit,
        }


async def resolve_encoder(
    settings: Any,
    runner: ProbeRunner,
    *,
    geometries: Sequence[tuple[int, int]],
    cache_path: Path | None = None,
    ffmpeg_version: str = "unknown",
    driver: str | None = None,
    frame_counter: FrameCounter | None = None,
) -> EncoderResolution:
    """Decide between NVENC and x264 for the given preset geometries."""
    mode = str(settings.encode.encoder)
    if mode == "x264":
        return EncoderResolution(profile=x264_profile(settings), reason="x264 configured")
    if not geometries:
        raise MediaToolError("resolve_encoder needs at least one preset geometry")

    entries = load_probe_cache(cache_path) if cache_path is not None else {}
    cache_hit = False
    failures: list[str] = []
    timeout_s = float(settings.encode.probe.timeout_s)
    work_dir = (
        (cache_path.parent if cache_path is not None else Path(".")) / "probe-work"
    )

    for geometry in geometries:
        key = cache_key(ffmpeg_version=ffmpeg_version, geometry=geometry, driver=driver)
        cached = entries.get(key)
        if cached is not None and "ok" in cached:
            cache_hit = True
            ok = bool(cached.get("ok"))
            reason = str(cached.get("reason", ""))
        else:
            ok, reason = await probe_encoder(
                nvenc_profile(settings),
                width=geometry[0],
                height=geometry[1],
                work_dir=work_dir,
                runner=runner,
                timeout_s=timeout_s,
                frame_counter=frame_counter,
            )
            if cache_path is not None:
                entries[key] = {"ok": ok, "reason": reason}
                save_probe_cache(cache_path, entries)
        if not ok:
            failures.append(f"{geometry[0]}x{geometry[1]}: {reason}")

    if not failures:
        return EncoderResolution(
            profile=nvenc_profile(settings),
            reason="nvenc probe passed",
            cache_hit=cache_hit,
        )
    detail = scrub_text("; ".join(failures))
    if mode == "nvenc":
        raise EncoderUnavailable(
            f"encode.encoder is 'nvenc' but the probe failed: {detail}"
        )
    return EncoderResolution(
        profile=x264_profile(settings),
        reason="auto fell back to x264",
        fallback_reason=detail,
        cache_hit=cache_hit,
    )
