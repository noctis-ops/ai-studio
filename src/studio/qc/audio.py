"""Phase 3 audio QC (PHASE3 §H11) — persisted to ``qc_results``.

Checks (all persisted, all blocking except ``license_attribution``):

+------------------------------------+--------------------------------------+
| check                              | method                               |
+====================================+======================================+
| unreadable_or_empty                | decode to PCM; any error / 0 samples |
| exact_length                       | decoded samples vs timeline          |
| narration_presence                 | per-line window RMS in the mix       |
| loudness                           | ebur128 vs target ± tolerance        |
| true_peak                          | ebur128 true peak                    |
| clip_peak                          | astats Peak_level                    |
| duck_depth                         | 100 ms RMS pre vs post duck          |
| speech_to_music                    | 100 ms RMS speech vs music post-duck |
| plan_coverage                      | cues vs timeline                     |
| license_completeness               | cues -> library rows                 |
+------------------------------------+--------------------------------------+

The 100 ms RMS series uses the same window math as
:func:`studio.audio.analysis.rms_series_db` (the spec's prescribed
``asetnsamples + astats`` pipeline was replaced by decoding the buss stems
and applying identical integer-window math — recorded under Decisions).
"""

from __future__ import annotations

import json
import math
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.audio.analysis import (
    astats_levels,
    integrated_loudness,
    rms_dbfs,
    rms_series_db,
    s16_le_samples,
)
from studio.video.ffmpeg import run_ffmpeg


@dataclass(frozen=True)
class QcSpec:
    min_duck_db: float
    min_speech_to_music_db: float
    silent_line_rms_db: float
    window_ms: int
    exclude_onset_ms: int
    target_lufs: float
    lufs_tolerance: float
    tp_ceiling_db: float
    clip_peak_db: float


@dataclass(frozen=True)
class QcCheckResult:
    name: str
    status: str           # pass | warn | fail
    detail: dict[str, Any]


@dataclass
class AudioQcInputs:
    deliverable_id: str
    master_path: str
    narration_stem_path: str
    total_samples: int
    sample_rate: int
    placements: list[tuple[int, int]]   # (start_sample, length_samples) in the mix timeline
    cues: list[dict[str, Any]]          # {kind, library_sha256, start_frame, end_frame}
    library: dict[str, dict[str, Any]]  # sha256 -> {title, license, attribution_required, ...}
    fps: int
    music_pre_path: str | None = None
    music_post_path: str | None = None
    no_mood_match_warn: bool = False


async def _decode(path: str, timeout_s: float = 300.0) -> array[int]:
    """Decode to a stdlib ``array('h')`` of s16 samples (memory-lean for
    full-length masters; a Python int list would cost ~36 bytes/sample)."""
    hint = Path(str(path)).suffix.lower().lstrip(".")
    args = ["-hide_banner", "-v", "error"]
    if hint in ("wav", "flac", "mp3", "ogg", "aac", "m4a"):
        args += ["-f", "wav" if hint == "wav" else hint]
    args += ["-i", str(path), "-ac", "1", "-f", "s16le", "pipe:1"]
    stdout, _ = await run_ffmpeg(args, timeout_s=timeout_s)
    return s16_le_samples(stdout)


async def run_audio_qc(inputs: AudioQcInputs, spec: QcSpec) -> list[QcCheckResult]:
    results: list[QcCheckResult] = []
    sr = inputs.sample_rate

    # ---- decode master once (drives length + presence + ratio)
    master_samples: array[int] | None = None
    try:
        master_samples = await _decode(inputs.master_path)
        decode_error = None
    except Exception as exc:  # any decode failure is a QC fail, not a crash
        decode_error = str(exc)

    ok = decode_error is None and master_samples is not None
    if not ok or (master_samples is not None and len(master_samples) == 0):
        results.append(QcCheckResult(
            "unreadable_or_empty", "fail",
            {"error": decode_error or "zero samples decoded"},
        ))
        # everything downstream is meaningless; return early
        return results

    assert master_samples is not None
    results.append(QcCheckResult(
        "unreadable_or_empty", "pass", {"samples": len(master_samples)},
    ))

    # ---- exact length
    if len(master_samples) != inputs.total_samples:
        results.append(QcCheckResult(
            "exact_length", "fail",
            {"samples": len(master_samples), "expected": inputs.total_samples},
        ))
    else:
        results.append(QcCheckResult("exact_length", "pass", {"samples": len(master_samples)}))

    # ---- narration presence (per-line window RMS in the final mix)
    silent: list[dict[str, Any]] = []
    for index, (start, length) in enumerate(inputs.placements):
        window = master_samples[start:start + length]
        if len(window) == 0:
            silent.append({"line": index, "start_sample": start, "rms_db": -math.inf})
            continue
        level = rms_dbfs(window)
        if level < spec.silent_line_rms_db:
            silent.append({"line": index, "start_sample": start, "rms_db": level})
    if silent:
        results.append(QcCheckResult("narration_presence", "fail", {"silent_lines": silent}))
    else:
        results.append(QcCheckResult(
            "narration_presence", "pass", {"lines": len(inputs.placements)}))

    # ---- loudness + true peak
    summary = await integrated_loudness(inputs.master_path)
    if summary.integrated_lufs == -math.inf:
        results.append(QcCheckResult(
            "loudness", "fail", {"integrated_lufs": "-inf"}))
    elif abs(summary.integrated_lufs - spec.target_lufs) > spec.lufs_tolerance:
        results.append(QcCheckResult(
            "loudness", "fail",
            {"integrated_lufs": summary.integrated_lufs,
             "target_lufs": spec.target_lufs,
             "tolerance": spec.lufs_tolerance}))
    else:
        results.append(QcCheckResult(
            "loudness", "pass", {"integrated_lufs": summary.integrated_lufs}))

    if summary.true_peak_dbtp is None:
        results.append(QcCheckResult("true_peak", "warn",
                                     {"detail": "ebur128 reported no true peak"}))
    elif summary.true_peak_dbtp > spec.tp_ceiling_db:
        results.append(QcCheckResult(
            "true_peak", "fail",
            {"true_peak_dbtp": summary.true_peak_dbtp, "ceiling_db": spec.tp_ceiling_db}))
    else:
        results.append(QcCheckResult(
            "true_peak", "pass", {"true_peak_dbtp": summary.true_peak_dbtp}))

    levels = await astats_levels(inputs.master_path)
    peak = levels.get("peak_db")
    if peak is None:
        results.append(QcCheckResult("clip_peak", "warn",
                                     {"detail": "astats reported no peak"}))
    elif peak > spec.clip_peak_db:
        results.append(QcCheckResult(
            "clip_peak", "fail", {"peak_db": peak, "ceiling_db": spec.clip_peak_db}))
    else:
        results.append(QcCheckResult("clip_peak", "pass", {"peak_db": peak}))

    # ---- duck depth + speech-to-music (100 ms RMS windows)
    placements = [(int(s), int(length)) for s, length in inputs.placements]
    onset = sr * spec.exclude_onset_ms // 1000

    stem_samples: array[int] | None = None
    try:
        stem_samples = await _decode(inputs.narration_stem_path)
    except Exception:
        stem_samples = None

    pre_samples = await _decode(inputs.music_pre_path) if inputs.music_pre_path else None
    post_samples = await _decode(inputs.music_post_path) if inputs.music_post_path else None

    def series(samples: Any) -> list[float] | None:
        if samples is None:
            return None
        return rms_series_db(samples, sample_rate=sr, window_ms=spec.window_ms)

    pre_series = series(pre_samples)
    post_series = series(post_samples)
    stem_series = series(stem_samples)

    def speech_window_indices() -> list[int]:
        window = max(1, sr * spec.window_ms // 1000)
        out: list[int] = []
        for start, length in placements:
            lo = (start + onset) // window
            hi = (start + length) // window
            out.extend(range(lo, hi + 1))
        return sorted(set(out))

    speech_idx = speech_window_indices()

    if pre_series is not None and post_series is not None:
        depths = []
        for i in speech_idx:
            if i < len(pre_series) and i < len(post_series):
                a, b = pre_series[i], post_series[i]
                if a != -math.inf and b != -math.inf:
                    depths.append(a - b)
        avg_depth = sum(depths) / len(depths) if depths else 0.0
        if not depths:
            results.append(QcCheckResult(
                "duck_depth", "warn", {"detail": "no measurable speech windows"}))
        elif avg_depth < spec.min_duck_db:
            results.append(QcCheckResult(
                "duck_depth", "fail",
                {"avg_duck_db": avg_depth, "min_duck_db": spec.min_duck_db}))
        else:
            results.append(QcCheckResult(
                "duck_depth", "pass", {"avg_duck_db": avg_depth}))
        if stem_series is not None:
            ratios = []
            for i in speech_idx:
                if i < len(stem_series) and i < len(post_series):
                    a, b = stem_series[i], post_series[i]
                    if a != -math.inf and b != -math.inf:
                        ratios.append(a - b)
            avg_ratio = sum(ratios) / len(ratios) if ratios else 0.0
            if not ratios:
                results.append(QcCheckResult(
                    "speech_to_music", "warn", {"detail": "no measurable speech windows"}))
            elif avg_ratio < spec.min_speech_to_music_db:
                results.append(QcCheckResult(
                    "speech_to_music", "fail",
                    {"avg_ratio_db": avg_ratio,
                     "min_speech_to_music_db": spec.min_speech_to_music_db}))
            else:
                results.append(QcCheckResult(
                    "speech_to_music", "pass", {"avg_ratio_db": avg_ratio}))
    else:
        results.append(QcCheckResult(
            "duck_depth", "warn", {"detail": "music bus stems unavailable"}))
        results.append(QcCheckResult(
            "speech_to_music", "warn", {"detail": "music bus stems unavailable"}))

    # ---- plan coverage: cues cover the music span of the timeline
    music = [c for c in inputs.cues if c.get("kind") == "music"]
    frames_per_sample_block = max(1, inputs.sample_rate // inputs.fps)
    total_frames = math.ceil(inputs.total_samples / frames_per_sample_block)
    beyond = [c for c in inputs.cues if int(c["end_frame"]) > total_frames]
    if beyond:
        results.append(QcCheckResult(
            "plan_coverage", "fail",
            {"cues_beyond_timeline": [
                {"sha256": c.get("library_sha256"), "end_frame": c["end_frame"]} for c in beyond]}))
    elif not music and not inputs.no_mood_match_warn:
        results.append(QcCheckResult(
            "plan_coverage", "fail",
            {"uncovered": "no music cues and no no-mood-match warn"}))
    else:
        results.append(QcCheckResult(
            "plan_coverage", "pass", {"music_cues": len(music)}))

    # ---- license completeness: every used cue resolves to a licensed library row
    missing_license = []
    for cue in inputs.cues:
        row = inputs.library.get(cue.get("library_sha256") or "")
        if row is None:
            missing_license.append(
                {"sha256": cue.get("library_sha256"), "problem": "not in library"})
        elif not row.get("license"):
            missing_license.append(
                {"sha256": cue["library_sha256"], "problem": "no license"})
    if missing_license:
        results.append(QcCheckResult(
            "license_completeness", "fail", {"missing": missing_license}))
    else:
        results.append(QcCheckResult("license_completeness", "pass",
                                     {"cues": len(inputs.cues)}))

    return results


def qc_rows(
    deliverable_id: str, results: list[QcCheckResult]
) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        rows.append({
            "deliverable_id": deliverable_id,
            "subject_scope": "deliverable",
            "subject_id": deliverable_id,
            "check_name": result.name,
            "status": result.status,
            "detail_json": json.dumps(result.detail, ensure_ascii=False, sort_keys=True),
        })
    return rows

