"""Async ffmpeg/ffprobe runner. Raises MediaToolError on any failure."""

from __future__ import annotations

import asyncio
import shutil

from studio.domain.errors import MediaToolError

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def find_binary(name: str) -> str:
    if name not in (FFMPEG, FFPROBE):
        raise MediaToolError(f"unknown media binary: {name!r}")
    path = shutil.which(name)
    if path is None:
        raise MediaToolError(f"{name} is not on PATH; install ffmpeg (e.g. apt-get install ffmpeg)")
    return path


async def run_ffmpeg(args: list[str], *, timeout_s: float = 120.0) -> tuple[bytes, bytes]:
    """Run ffmpeg with the given arguments; returns (stdout, stderr)."""
    binary = find_binary(FFMPEG)
    return await _run(binary, args, timeout_s=timeout_s, tool="ffmpeg")


async def run_ffprobe(args: list[str], *, timeout_s: float = 60.0) -> tuple[bytes, bytes]:
    binary = find_binary(FFPROBE)
    return await _run(binary, args, timeout_s=timeout_s, tool="ffprobe")


async def _run(binary: str, args: list[str], *, timeout_s: float, tool: str) -> tuple[bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaToolError(f"{tool} failed to start: {exc}") from exc
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await proc.communicate()
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise MediaToolError(f"{tool} timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise MediaToolError(f"{tool} exited with {proc.returncode}: {detail[-500:]}")
    return stdout, stderr


# =====================================================================
# Phase 3 extensions (H5/H9/H10, D3)
# =====================================================================

# D3 + H3: exact filter set the audio chain and Phase 4 visual QC rely on.
# `asplit` is required by the ducking/envelope design; the second block is
# the video/QC set from PHASE4_IMPLEMENTATION_PROMPT.md §D H3.
REQUIRED_FILTERS = (
    "loudnorm", "ebur128", "astats", "sidechaincompress", "volume", "amix",
    "adelay", "afade", "atrim", "apad", "aresample", "aformat",
    "silenceremove", "areverse", "alimiter", "anullsrc", "asetnsamples",
    "ametadata", "pan", "asplit",
    "signalstats", "blackdetect", "freezedetect", "ssim", "fps", "trim",
    "setpts", "tpad", "scale", "setsar", "format",
    # Phase 5 (§H): segmented assembly + render QC.
    "concat",
)

# Phase 5 §H: assembly/QC need these encoders + muxers, not just filters.
REQUIRED_ENCODERS = ("aac", "libx264")
REQUIRED_MUXERS = ("mp4", "matroska", "mpegts")

_version_cache: tuple[int, int, int] | None = None


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse `ffmpeg version N.M...` output; raises MediaToolError if absent."""
    import re

    match = re.search(r"version\s+[^\d\n]*(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match is None:
        raise MediaToolError(f"could not parse ffmpeg version from: {text[:200]!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


async def ffmpeg_version() -> tuple[int, int, int]:
    """Detect (and cache) the ffmpeg major/minor/patch version."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    stdout, _ = await run_ffmpeg(["-version"], timeout_s=30.0)
    _version_cache = parse_version(stdout.decode("utf-8", errors="replace"))
    return _version_cache


def filtergraph_script_args(path: str, version: tuple[int, int, int]) -> list[str]:
    """Version-appropriate arguments to load a filtergraph from a UTF-8 file.

    FFmpeg >= 7.0 supports `-/filter_complex <file>`; older versions need
    `-filter_complex_script <file>`.
    """
    if version >= (7, 0, 0):
        return ["-/filter_complex", str(path)]
    return ["-filter_complex_script", str(path)]


async def available_filters() -> set[str]:
    """Names of compiled-in filters (from `ffmpeg -filters`)."""
    stdout, _ = await run_ffmpeg(["-hide_banner", "-filters"], timeout_s=30.0)
    filters: set[str] = set()
    import re

    for line in stdout.decode("utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*[A-Za-z0-9.|]{1,4}\s+([a-z0-9_]{2,})\s", line)
        if match:
            filters.add(match.group(1))
    return filters


def _parse_flag_table(text: str) -> set[str]:
    """Names from `ffmpeg -encoders` / `-muxers` tables.

    The flag column width differs per table and per ffmpeg release ("V....D",
    " E "), so the flags are validated as a token instead of at a fixed offset.
    """
    import re

    names: set[str] = set()
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 2:
            continue
        if re.fullmatch(r"[A-Za-z.]{1,6}", tokens[0]) and re.fullmatch(
            r"[a-z0-9_]+", tokens[1]
        ):
            names.add(tokens[1])
    return names


async def available_encoders() -> set[str]:
    stdout, _ = await run_ffmpeg(["-hide_banner", "-encoders"], timeout_s=30.0)
    return _parse_flag_table(stdout.decode("utf-8", errors="replace"))


async def available_muxers() -> set[str]:
    stdout, _ = await run_ffmpeg(["-hide_banner", "-muxers"], timeout_s=30.0)
    return _parse_flag_table(stdout.decode("utf-8", errors="replace"))


async def capability_check() -> tuple[tuple[int, int, int], list[str]]:
    """D3: return (version, missing_required_filters)."""
    version = await ffmpeg_version()
    have = await available_filters()
    missing = [name for name in dict.fromkeys(REQUIRED_FILTERS) if name not in have]
    return version, missing


async def assembly_capability_check() -> tuple[list[str], list[str], list[str]]:
    """Phase 5 §H: (missing_filters, missing_encoders, missing_muxers)."""
    encoders = await available_encoders()
    muxers = await available_muxers()
    _version, missing_filters = await capability_check()
    missing_encoders = [n for n in REQUIRED_ENCODERS if n not in encoders]
    missing_muxers = [n for n in REQUIRED_MUXERS if n not in muxers]
    return missing_filters, missing_encoders, missing_muxers


async def probe_duration_seconds(path: str, *, timeout_s: float = 30.0) -> float:
    """Container duration in seconds via ffprobe (float; informational only)."""
    import json

    stdout, _ = await run_ffprobe(
        [
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        timeout_s=timeout_s,
    )
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        return float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise MediaToolError(f"ffprobe could not read duration of {path}: {exc}") from exc


async def probe_sample_rate_channels(path: str, *, timeout_s: float = 30.0) -> tuple[int, int]:
    """(sample_rate, channels) of the first audio stream via ffprobe."""
    import json

    stdout, _ = await run_ffprobe(
        [
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "json",
            str(path),
        ],
        timeout_s=timeout_s,
    )
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        stream = data["streams"][0]
        return int(stream["sample_rate"]), int(stream["channels"])
    except (json.JSONDecodeError, KeyError, IndexError, ValueError, TypeError) as exc:
        raise MediaToolError(f"ffprobe could not read stream info of {path}: {exc}") from exc
