"""Narration stem: mono 16-bit WAV assembled with stdlib `wave` (PHASE3 §H9).

The stem contains exactly ``total_samples`` samples: silence everywhere
except at each line placement, where the line's conformed WAV frames sit
(byte-compare exact). No FFmpeg is involved, so the stem is sample-exact by
construction.
"""

from __future__ import annotations

import wave
from pathlib import Path

from studio.audio.timeline import Timeline
from studio.domain.errors import MediaToolError


def _read_s16_mono(path: str) -> tuple[bytes, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2:
                raise MediaToolError(
                    f"{path}: expected 16-bit samples, got {handle.getsampwidth() * 8}-bit"
                )
            if handle.getnchannels() != 1:
                raise MediaToolError(f"{path}: expected mono, got {handle.getnchannels()} channels")
            frames = handle.readframes(handle.getnframes())
            return frames, int(handle.getnframes())
    except (wave.Error, OSError) as exc:
        raise MediaToolError(f"cannot read WAV {path}: {exc}") from exc


def build_narration_stem(
    timeline: Timeline,
    cas_paths: dict[str, str],
    out_path: str,
) -> int:
    """Write the stem to ``out_path``; returns ``total_samples``.

    ``cas_paths`` maps conformed_sha256 -> absolute file path of the conformed
    WAV (callers resolve CAS entries). Every line must be present.
    """
    total = int(timeline.total_samples)
    silence_chunk = b"\x00\x00" * 4096

    with wave.open(str(out_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(timeline.sample_rate)

        cursor = 0
        for placement in sorted(timeline.lines, key=lambda p: p.start_sample):
            line_path = cas_paths.get(placement.conformed_sha256)
            if line_path is None:
                raise MediaToolError(
                    f"missing CAS path for conformed line {placement.line_id} "
                    f"(sha256 {placement.conformed_sha256[:12]}...)"
                )
            frames, n_samples = _read_s16_mono(line_path)
            if n_samples != placement.length_samples:
                raise MediaToolError(
                    f"conformed line {placement.line_id} has {n_samples} samples "
                    f"but the timeline expects {placement.length_samples}"
                )
            if placement.start_sample < cursor:
                raise MediaToolError(
                    f"line {placement.line_id} overlaps a previous line in the timeline"
                )
            # silence up to the line start
            remaining = placement.start_sample - cursor
            while remaining > 0:
                take = min(remaining, len(silence_chunk) // 2)
                out.writeframesraw(silence_chunk[: take * 2])
                remaining -= take
            out.writeframesraw(frames)
            cursor = placement.start_sample + n_samples
        # trailing silence
        remaining = total - cursor
        while remaining > 0:
            take = min(remaining, len(silence_chunk) // 2)
            out.writeframesraw(silence_chunk[: take * 2])
            remaining -= take
    return total


def stem_digest(out_path: str | Path) -> str:
    """sha256 of the raw PCM payload (not the header) — used for caching."""
    import hashlib

    digest = hashlib.sha256()
    with wave.open(str(out_path), "rb") as handle:
        while True:
            chunk = handle.readframes(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
