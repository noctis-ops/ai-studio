"""Ducking helpers (PHASE3 §H9).

Envelope mode (default): derive the duck envelope from the timeline's line
placements — speech spans separated by less than ``merge_gap_ms`` are merged
first (no pumping in short pauses), then for every merged span:

    0 dB at start - pre_ms
    -depth_db at start - pre_ms + attack_ms   (attack_ms MUST be <= pre_ms;
                                               validated at settings load)
    -depth_db at end
    0 dB at end + release_ms

The envelope applies to the music bus; the ambience bus gets
``ambience_fraction * depth_db``. Mode ``sidechain`` is rendered with
``sidechaincompress`` keyed by the narration stem instead.

All functions are pure (no I/O) and sample-exact: milliseconds are rounded
to samples round-half-up.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def ms_to_samples(ms: float, sample_rate: int) -> int:
    return math.floor(float(ms) * sample_rate / 1000.0 + 0.5)


def merge_spans(
    spans: Sequence[tuple[int, int]], gap_samples: int
) -> list[tuple[int, int]]:
    """Merge overlapping/touching spans whose gap is below ``gap_samples``."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start - merged[-1][1] < gap_samples:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def speech_spans(
    placements: Sequence[tuple[int, int]],  # (start_sample, length_samples)
    *,
    merge_gap_ms: float,
    sample_rate: int,
) -> list[tuple[int, int]]:
    gap = ms_to_samples(merge_gap_ms, sample_rate)
    return merge_spans([(int(start), int(start) + int(length)) for start, length in placements],
                       gap)


def duck_envelope_points(
    spans: Sequence[tuple[int, int]],
    *,
    depth_db: float,
    pre_ms: float,
    attack_ms: float,
    release_ms: float,
    ambience_fraction: float = 1.0,
    sample_rate: int = 48000,
) -> list[tuple[float, float]]:
    """(time_seconds, gain_db) breakpoints for the duck envelope.

    ``ambience_fraction`` scales the depth for the ambience bus (1.0 for the
    music bus, e.g. 0.5 for ambience).
    """
    depth = float(depth_db) * float(ambience_fraction)
    pre = ms_to_samples(pre_ms, sample_rate)
    attack = ms_to_samples(attack_ms, sample_rate)
    release = ms_to_samples(release_ms, sample_rate)
    if attack > pre:
        # belt and braces: settings validation already forbids this
        from studio.domain.errors import ConfigError

        raise ConfigError(
            f"audio.duck.attack_ms ({attack_ms}) must be <= audio.duck.pre_ms ({pre_ms})"
        )
    def to_seconds(samples: int) -> float:
        return samples / sample_rate

    points: list[tuple[float, float]] = []
    for start, end in spans:
        points.append((max(0.0, to_seconds(start - pre)), 0.0))
        points.append((max(0.0, to_seconds(start - pre + attack)), -depth))
        points.append((to_seconds(end), -depth))
        points.append((to_seconds(end + release), 0.0))
    # merge duplicate-time points (release of one span meets the next
    # pre-attck of the following) keeping the *lower* gain (deeper duck)
    merged: list[tuple[float, float]] = []
    for t, db in points:
        t = round(t, 6)
        if merged and t <= merged[-1][0]:
            if db < merged[-1][1]:
                merged[-1] = (merged[-1][0], db)
            continue
        merged.append((t, db))
    return merged
