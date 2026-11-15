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
