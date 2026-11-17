"""Per-line conform of RAW TTS audio (PHASE3 §H5). Local, deterministic, free.

Pipeline for one raw asset:

1. Decode to mono, resample to ``audio.sample_rate``, 16-bit.
2. Trim leading/trailing silence (``silenceremove`` forward, then
   ``areverse → silenceremove → areverse``), keeping ``speech_trim.keep_ms``
   of padding each side, threshold from ``speech_trim.threshold_db``.
3. Measure integrated loudness (``ebur128``). If unusable (< -60 LUFS or the
   measurement fails), fall back to an RMS-based estimate via ``astats`` with
   the documented offset ``ASTATS_RMS_TO_LUFS_OFFSET_DB``.
4. gain = ``speech_line_lufs - measured + voice.mix_gain_db``, clamped to
   ±``max_line_gain_db``, then ``alimiter`` with a -1 dBFS ceiling.
5. Store the WAV via the CAS (retention=intermediate) and return
   ``(AssetRef, samples)`` — samples come from the WAV header.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from studio.audio.analysis import (
    ASTATS_RMS_TO_LUFS_OFFSET_DB,
    astats_levels,
    integrated_loudness,
    wav_header_samples,
)
from studio.core.cas import ContentStore
from studio.domain.errors import MediaToolError
from studio.domain.refs import AssetRef
from studio.video.ffmpeg import run_ffmpeg

_LOUDNESS_FLOOR_LUFS = -60.0
# alimiter ceiling for -1 dBFS (10 ** (-1/20) ≈ 0.8913)
_LIMIT_CEILING = 0.8913


@dataclass(frozen=True)
class ConformedLine:
    asset: AssetRef
    samples: int
    measured_lufs: float
    gain_db: float
    gain_clamped: bool


def _limiter_expr() -> str:
    return f"alimiter=limit={_LIMIT_CEILING}:attack=5:release=50"


async def conform_line(
    raw_path: str,
    *,
    cas: ContentStore,
    settings: Any,
    mix_gain_db: float = 0.0,
    work_dir: str | None = None,
) -> ConformedLine:
    import os
    import tempfile

    sample_rate = int(settings.audio.sample_rate)
    trim = settings.audio.speech_trim
    levels = settings.audio.levels
    keep_pad_s = float(trim.keep_ms) / 1000.0
    # ffmpeg silenceremove thresholds are LINEAR amplitudes, not dB suffixes
    # (verified against ffmpeg n9: "start_threshold <double> ... from 0 to DBL_MAX")
    threshold_lin = 10 ** (float(trim.threshold_db) / 20)
    threshold = str(threshold_lin)

    workdir = work_dir or tempfile.mkdtemp(prefix="studio-conform-")
    os.makedirs(workdir, exist_ok=True)
    mono_path = os.path.join(workdir, "mono.wav")
    trimmed_path = os.path.join(workdir, "trimmed.wav")
    final_path = os.path.join(workdir, "conformed.wav")

    # 1. mono / resample / 16-bit
    raw_hint = str(raw_path).rsplit(".", 1)[-1].lower() if "." in str(raw_path) else ""
    raw_args = ["-nostats", "-hide_banner", "-y"]
    if raw_hint in ("wav", "flac", "mp3", "ogg", "aac", "m4a"):
        raw_args += ["-f", raw_hint]
    await run_ffmpeg(
        [
            *raw_args,
            "-i", str(raw_path),
            "-ac", "1", "-ar", str(sample_rate),
            "-bitexact", "-fflags", "+bitexact", "-map_metadata", "-1",
            "-c:a", "pcm_s16le", mono_path,
        ],
        timeout_s=300.0,
    )

    # 2. trim edge silence, keeping keep_ms of padding at both ends
    filter_chain = (
        f"silenceremove=start_periods=1:start_threshold={threshold}:start_silence={keep_pad_s},"
        f"areverse,"
        f"silenceremove=start_periods=1:start_threshold={threshold}:start_silence={keep_pad_s},"
        f"areverse"
    )
    await run_ffmpeg(
        [
            "-nostats", "-hide_banner", "-y", "-f", "wav",
            "-i", mono_path,
            "-af", filter_chain,
            "-bitexact", "-fflags", "+bitexact", "-map_metadata", "-1",
            "-c:a", "pcm_s16le", trimmed_path,
        ],
        timeout_s=300.0,
    )

    # 3. measure integrated loudness; fallback to RMS-based estimate
    measured: float
    try:
        summary = await integrated_loudness(trimmed_path)
        if summary.integrated_lufs == -math.inf or summary.integrated_lufs < _LOUDNESS_FLOOR_LUFS:
            raise MediaToolError(
                f"integrated loudness unusable ({summary.integrated_lufs}); falling back to astats"
            )
        measured = summary.integrated_lufs
    except (MediaToolError, ValueError):
        levels_astats = await astats_levels(trimmed_path)
        rms_db = levels_astats.get("rms_db")
        if rms_db is None or rms_db == -math.inf:
            raise MediaToolError("line is digital silence (rms = -inf); cannot conform") from None
        measured = rms_db + ASTATS_RMS_TO_LUFS_OFFSET_DB

    # 4. gain toward speech_line_lufs (+ voice mix gain), clamped, limited
    target = float(levels.speech_line_lufs)
    max_gain = float(levels.max_line_gain_db)
    gain = target - measured + float(mix_gain_db)
    clamped_gain = max(-max_gain, min(max_gain, gain))
    await run_ffmpeg(
        [
            "-nostats", "-hide_banner", "-y", "-f", "wav",
            "-i", trimmed_path,
            "-af", f"volume={clamped_gain}dB,{_limiter_expr()}",
            "-bitexact", "-fflags", "+bitexact", "-map_metadata", "-1",
            "-c:a", "pcm_s16le", final_path,
        ],
        timeout_s=300.0,
    )

    # 5. store via CAS; exact samples from the WAV header
    samples = wav_header_samples(final_path)
    from pathlib import Path as _Path

    asset = cas.put_file(
        _Path(final_path), kind="audio", ext="wav", move=True,
        retention="intermediate",
    )
    return ConformedLine(
        asset=asset,
        samples=samples,
        measured_lufs=measured,
        gain_db=clamped_gain,
        gain_clamped=clamped_gain != gain,
    )
