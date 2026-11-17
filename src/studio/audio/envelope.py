"""Gain-envelope expression builder (PHASE3 §H9).

``build_gain_expr`` turns piecewise-linear dB breakpoints into a single
FFmpeg expression over the variable ``t`` (seconds) using ``if``/``lt``/
``gt``/``lerp`` arranged as a **balanced binary decision tree** (depth
O(log n), never a linear chain). It is meant to be applied as

    asetnsamples=n=480:p=0,volume='pow(10,(EXPR)/20)':eval=frame

(10 ms frames, so linear ramps do not step audibly), with the expression
quoted inside a filtergraph *file* (Windows command lines cap near 32k).

Rules:

- times must be strictly increasing; duplicates are nudged forward by one
  sample at ``sample_rate`` (20.8 µs at 48 kHz);
- the expression is **constant outside the range** (first/last value
  repeated forever);
- a single point yields a constant expression;
- consecutive-equal dB values collapse to a constant leaf.

Unit-tested by round-trip through real FFmpeg (see tests; @pytest.mark.ffmpeg).
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def _fmt(value: float) -> str:
    if value == 0:
        return "0"
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text.endswith("."):
        text = text[:-1]
    return text or "0"


def normalize_points(
    points: Sequence[tuple[float, float]], *, sample_rate: int = 48000
) -> list[tuple[float, float]]:
    """Sort by time and nudge duplicate times forward by one sample."""
    if not points:
        raise ValueError("envelope requires at least one breakpoint")
    one_sample = 1.0 / float(sample_rate)
    out: list[tuple[float, float]] = []
    last_t = -math.inf
    for t, db in sorted(points, key=lambda item: (item[0], item[1])):
        t = float(t)
        if not math.isfinite(t) or not math.isfinite(float(db)):
            raise ValueError(f"envelope points must be finite, got ({t}, {db})")
        t = max(t, last_t + one_sample) if out else max(t, 0.0)
        out.append((t, float(db)))
        last_t = t
    return out


def build_gain_expr(
    points: Sequence[tuple[float, float]], *, sample_rate: int = 48000
) -> str:
    """Balanced-binary-tree FFmpeg expression for the gain (dB) at time ``t``."""
    pts = normalize_points(points, sample_rate=sample_rate)
    n = len(pts)
    if n == 1:
        return _fmt(pts[0][1])

    times = [t for t, _ in pts]

    def leaf(a: int) -> str:
        t0, d0 = pts[a]
        t1, d1 = pts[a + 1]
        if d0 == d1:
            return _fmt(d0)
        span = _fmt(t1 - t0)
        return f"lerp({_fmt(d0)},{_fmt(d1)},(t-{_fmt(t0)})/{span})"

    def tree(a: int, b: int) -> str:  # covers t in [times[a], times[b]]
        if b - a == 1:
            return leaf(a)
        mid = (a + b) // 2
        return f"if(lt(t,{_fmt(times[mid])}),{tree(a, mid)},{tree(mid, b)})"

    first_t, first_db = pts[0]
    last_t, last_db = pts[-1]
    body = tree(0, n - 1)
    return (
        f"if(lt(t,{_fmt(first_t)}),{_fmt(first_db)},"
        f"if(gt(t,{_fmt(last_t)}),{_fmt(last_db)},{body}))"
    )


def envelope_volume_filter(
    points: Sequence[tuple[float, float]], *, sample_rate: int = 48000
) -> str:
    """`asetnsamples` + `volume` chain applying the envelope (10 ms frames)."""
    expr = build_gain_expr(points, sample_rate=sample_rate)
    n = max(1, sample_rate // 100)
    return f"asetnsamples=n={n}:p=0,volume='pow(10,({expr})/20)':eval=frame"


def depth_of_expression(points: Sequence[tuple[float, float]]) -> int:
    """O(log n) nesting depth of the generated tree (used by T-ENV-01)."""
    n = max(1, len(points))
    return math.ceil(math.log2(max(1, n - 1))) + 2
