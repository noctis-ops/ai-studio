"""Two-pass loudnorm mastering (PHASE3 §H10).

Pass 1 measures with ``print_format=json`` (the JSON block is parsed from
stderr, last ``{...}`` wins). Pass 2 applies the measured values with
``linear=true`` against the ``audio.master`` targets, then *re-conforms the
length* (``aresample`` + ``apad=whole_len`` + ``atrim=end_sample``), because
loudnorm resamples internally and can shift the length by a few samples.

Output: a 24-bit FLAC. Verified afterwards exactly as the spec demands:
integrated loudness within ``target_lufs ± lufs_tolerance`` and true peak ≤
``tp_ceiling_db`` (both via ``ebur128``), sample peak ≤ ``clip_peak_db``
(via ``astats``), and exactly ``total_samples`` samples (counted by
decoding to raw PCM — FLAC has no stdlib reader). Any violation raises
:class:`MasteringError` and the stage fails; nothing half-written reaches
the CAS because the caller moves the file through ContentStore only on
success.

*Note for Phase 5:* AAC encoding can overshoot true peak by ~0.5 dB, so the
muxed file must be re-measured there.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from studio.audio.analysis import (
    astats_levels,
    integrated_loudness,
    parse_loudnorm_json,
    s16_le_samples,
)
from studio.domain.errors import MasteringError
from studio.video.ffmpeg import run_ffmpeg


@dataclass(frozen=True)
class MasterResult:
    master_path: Path
    loudness_i: float
    true_peak: float | None
    lra: float | None
    samples: int


async def _decode_s16(path: Path, *, timeout_s: float = 300.0) -> list[int]:
    hint = path.suffix.lower().lstrip(".")
    args = ["-hide_banner", "-v", "error"]
    if hint:
        args += ["-f", hint]
    args += ["-i", str(path), "-ac", "1", "-f", "s16le", "pipe:1"]
    stdout, _ = await run_ffmpeg(args, timeout_s=timeout_s)
    return list(s16_le_samples(stdout))


async def master_two_pass(
    premaster_path: Path,
    out_path: Path,
    *,
    target_lufs: float,
    true_peak_db: float,
    lra: float,
    lufs_tolerance: float,
    tp_ceiling_db: float,
    clip_peak_db: float,
    sample_rate: int,
    total_samples: int,
    timeout_s: float = 300.0,
) -> MasterResult:
    master_filter = f"loudnorm=I={target_lufs}:TP={true_peak_db}:LRA={lra}"

    # ---- pass 1: measure
    _, stderr = await run_ffmpeg(
        ["-hide_banner", "-nostats", "-f", "wav", "-i", str(premaster_path),
         "-af", f"{master_filter}:print_format=json",
         "-f", "null", "-"],
        timeout_s=timeout_s,
    )
    try:
        measured = parse_loudnorm_json(stderr)
    except Exception as exc:
        raise MasteringError(f"loudnorm pass 1 did not report measurements: {exc}") from exc
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if key not in measured:
            raise MasteringError(f"loudnorm pass 1 missing {key!r} in {measured!r}")

    # ---- pass 2: apply linear normalization + re-conform the length
    apply_filter = (
        f"{master_filter}:measured_I={measured['input_i']}:"
        f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:"
        f"linear=true,aresample={sample_rate},"
        # loudnorm emits one giant frame, which the FLAC encoder rejects
        # (max block size 65535): re-chunk into fixed 4096-sample frames first
        f"asetnsamples=n=4096:p=1,"
        f"apad=whole_len={total_samples},atrim=end_sample={total_samples}"
    )
    await run_ffmpeg(
        ["-hide_banner", "-y", "-f", "wav", "-i", str(premaster_path),
         "-af", apply_filter,
         "-bitexact", "-fflags", "+bitexact", "-map_metadata", "-1",
         "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
         "-c:a", "flac", str(out_path)],
        timeout_s=timeout_s,
    )

    # ---- verify: exactly total_samples (decode and count)
    samples = await _decode_s16(out_path, timeout_s=timeout_s)
    if len(samples) != total_samples:
        raise MasteringError(
            f"master has {len(samples)} samples, timeline expects {total_samples}"
        )

    # ---- verify: loudness + true peak within spec
    summary = await integrated_loudness(str(out_path), timeout_s=timeout_s)
    if (summary.integrated_lufs == -math.inf
            or abs(summary.integrated_lufs - target_lufs) > lufs_tolerance):
        raise MasteringError(
            f"master integrated loudness {summary.integrated_lufs:.2f} LUFS outside "
            f"{target_lufs} ± {lufs_tolerance} LUFS"
        )
    if summary.true_peak_dbtp is not None and summary.true_peak_dbtp > tp_ceiling_db:
        raise MasteringError(
            f"master true peak {summary.true_peak_dbtp:.2f} dBTP above {tp_ceiling_db} dBTP"
        )

    # ---- verify: sample peak (clipping)
    levels = await astats_levels(str(out_path), timeout_s=timeout_s)
    peak = levels.get("peak_db")
    if peak is not None and peak > clip_peak_db:
        raise MasteringError(
            f"master sample peak {peak:.2f} dBFS above {clip_peak_db} dBFS (clipping)"
        )

    return MasterResult(
        master_path=out_path,
        loudness_i=summary.integrated_lufs,
        true_peak=summary.true_peak_dbtp,
        lra=summary.lra_lu,
        samples=len(samples),
    )
