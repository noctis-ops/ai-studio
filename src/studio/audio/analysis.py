"""Parsing helpers and signal math for the audio engine (PHASE3 §H11/§H12).

Everything here is either pure (`s16_le_samples`, `rms_dbfs`, `rms_series_db`,
`sample_peak_dbfs`, the `parse_*` functions) or a thin async wrapper around
``studio.video.ffmpeg`` (`integrated_loudness`, `astats_levels`). Pure
functions are unit-tested on synthetic signals.

Level conventions: dBFS for sample values (0 dBFS = full scale, 32768), LUFS
for K-weighted integrated loudness. Silence maps to ``-math.inf`` rather than
a magic sentinel; callers that need a finite floor must clamp themselves.
"""

from __future__ import annotations

import math
import re
from array import array
from collections.abc import Sequence
from dataclasses import dataclass

from studio.domain.errors import MediaToolError
from studio.video.ffmpeg import run_ffmpeg

_LUFS_RE = re.compile(r"I:\s+(-?\d+(?:\.\d+)?|-inf)\s+LUFS")
_LRA_RE = re.compile(r"LRA:\s+(-?\d+(?:\.\d+)?|-inf)\s+LU")
_TP_RE = re.compile(r"Peak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dB(?:TP|FS)")
_RMS_RE = re.compile(r"RMS level dB:\s+(-?\d+(?:\.\d+)?|-inf)")
_PEAK_DB_RE = re.compile(r"Peak level dB:\s+(-?\d+(?:\.\d+)?|-inf)")
# K-weighting is, to first order, a constant offset of +0.691 dB over plain
# RMS for program material. Only used by the loudness *fallback* path;
# documented as an approximation, never for QC pass/fail decisions.
ASTATS_RMS_TO_LUFS_OFFSET_DB = 0.691


@dataclass(frozen=True)
class Ebur128Summary:
    integrated_lufs: float
    true_peak_dbtp: float | None
    lra_lu: float | None


def s16_le_samples(data: bytes) -> array[int]:
    """Decode little-endian signed 16-bit PCM bytes to an array of samples."""
    if len(data) % 2:
        raise MediaToolError("odd byte count in 16-bit PCM data")
    samples = array("h")
    samples.frombytes(data)
    return samples


def rms_dbfs(samples: Sequence[int]) -> float:
    """RMS level of s16 samples in dBFS; -inf for digital silence."""
    if not samples:
        return -math.inf
    total = sum(int(s) * int(s) for s in samples)
    mean = total / len(samples)
    if mean == 0:
        return -math.inf
    return 10.0 * math.log10(mean) - 20.0 * math.log10(32768.0)


def rms_series_db(
    samples: Sequence[int], *, sample_rate: int, window_ms: int = 100
) -> list[float]:
    """One dBFS RMS value per fixed window (last window may be short)."""
    window = max(1, int(sample_rate * window_ms / 1000))
    out: list[float] = []
    for start in range(0, len(samples), window):
        out.append(rms_dbfs(samples[start : start + window]))
    return out


def sample_peak_dbfs(samples: Sequence[int]) -> float:
    """Sample peak (not true peak) in dBFS; -inf for silence."""
    if not samples:
        return -math.inf
    peak = max(abs(int(s)) for s in samples)
    if peak == 0:
        return -math.inf
    return 20.0 * math.log10(peak / 32768.0)


def is_silent(samples: Sequence[int], threshold_db: float) -> bool:
    return rms_dbfs(samples) < threshold_db


def parse_ebur128_summary(stderr: bytes | str) -> Ebur128Summary:
    """Parse the summary block ffmpeg's ebur128 filter prints to stderr.

    Takes the *last* match (encoders can log per-frame lines); raises
    MediaToolError when the summary is missing or unparseable.
    """
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    i_match = _LUFS_RE.findall(text)
    if not i_match:
        raise MediaToolError("ffmpeg ebur128: integrated loudness not found in output")
    integrated = _parse_level(i_match[-1])
    lra_match = _LRA_RE.findall(text)
    tp_match = _TP_RE.findall(text)
    return Ebur128Summary(
        integrated_lufs=integrated,
        true_peak_dbtp=_parse_level(tp_match[-1]) if tp_match else None,
        lra_lu=_parse_level(lra_match[-1]) if lra_match else None,
    )


def parse_loudnorm_json(stderr: bytes | str) -> dict[str, str]:
    """Extract the last JSON object loudnorm prints (pass-1 measurement)."""
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    start = text.rfind("{")
    if start == -1:
        raise MediaToolError("loudnorm: no JSON block in ffmpeg output")
    end = text.rfind("}")
    if end <= start:
        raise MediaToolError("loudnorm: malformed JSON block in ffmpeg output")
    import json

    try:
        block = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"loudnorm: unparseable JSON block: {exc}") from exc
    if not isinstance(block, dict):
        raise MediaToolError("loudnorm: JSON block is not an object")
    return {str(k): str(v) for k, v in block.items()}


def parse_astats(stderr: bytes | str) -> dict[str, float]:
    """Parse `astats=measure_overall=Peak_level+RMS_level:measure_perchannel=none` output."""
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    out: dict[str, float] = {}
    rms = _RMS_RE.findall(text)
    peak = _PEAK_DB_RE.findall(text)
    if rms:
        out["rms_db"] = _parse_level(rms[-1])
    if peak:
        out["peak_db"] = _parse_level(peak[-1])
    return out


def _parse_level(raw: str) -> float:
    value = raw.strip().lower()
    if value in ("-inf", "inf"):
        return -math.inf if value == "-inf" else math.inf
    try:
        return float(value)
    except ValueError as exc:
        raise MediaToolError(f"unparseable level value {raw!r}") from exc


async def integrated_loudness(path: str, *, timeout_s: float = 60.0) -> Ebur128Summary:
    """Measure integrated loudness / true peak / LRA via ffmpeg ebur128."""
    ext = str(path).rsplit(".", 1)[-1].lower() if "." in str(path) else ""
    args = ["-nostats", "-hide_banner"]
    if ext:
        args += ["-f", ext]
    args += ["-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"]
    _, stderr = await run_ffmpeg(args, timeout_s=timeout_s)
    return parse_ebur128_summary(stderr)


async def astats_levels(path: str, *, timeout_s: float = 60.0) -> dict[str, float]:
    """Fallback level check via astats (used when ebur128 is unusable)."""
    ext = str(path).rsplit(".", 1)[-1].lower() if "." in str(path) else ""
    args = ["-nostats", "-hide_banner"]
    if ext:
        args += ["-f", ext]
    args += [
        "-i", str(path),
        "-af", "astats=measure_overall=Peak_level+RMS_level:measure_perchannel=none",
        "-f", "null", "-",
    ]
    _, stderr = await run_ffmpeg(args, timeout_s=timeout_s)
    return parse_astats(stderr)


def wav_header_samples(path: str) -> int:
    """Read the exact sample count of a WAV file from its header (stdlib wave)."""
    import wave

    try:
        with wave.open(str(path), "rb") as handle:
            return int(handle.getnframes())
    except (wave.Error, OSError) as exc:
        raise MediaToolError(f"not a readable WAV file: {path}: {exc}") from exc
