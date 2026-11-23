"""Thumbnail candidates (Phase 5 §K).

N frames at deterministic indexes (~15 %, ~45 %, ~75 % of the render) with an
exact-frame `select`. Deterministic means the same render yields the same
numbers of the same frames, so re-exporting is idempotent. No text overlays: a
platform's own text rendering is not something this tool can preview.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from studio.domain.errors import MediaToolError
from studio.video.ffmpeg import run_ffmpeg

FRACTIONS = (0.15, 0.45, 0.75)


def frame_indexes(total_frames: int, count: int) -> list[int]:
    """Deterministic frame numbers for `count` candidates."""
    if count <= 0 or total_frames <= 0:
        return []
    fractions = FRACTIONS[:count] if count <= len(FRACTIONS) else _spread(count)
    indexes: list[int] = []
    for fraction in fractions:
        index = round(total_frames * fraction)
        index = max(0, min(total_frames - 1, index))
        if index not in indexes:
            indexes.append(index)
    return indexes


def _spread(count: int) -> tuple[float, ...]:
    """Evenly spaced interior positions when more than three are requested."""
    step = 1.0 / (count + 1)
    return tuple(round(step * (i + 1), 6) for i in range(count))


def thumbnail_name(index: int) -> str:
    return f"frame_{index:06d}.png"


async def extract_thumbnails(
    render_path: Path,
    out_dir: Path,
    *,
    total_frames: int,
    count: int,
    timeout_s: float = 300.0,
) -> list[Path]:
    """Write `thumbnail_candidates/frame_<n>.png` for each deterministic index."""
    indexes = frame_indexes(total_frames, count)
    if not indexes:
        return []
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    written: list[Path] = []
    for index in indexes:
        target = out_dir / thumbnail_name(index)
        # `select=eq(n\,<index>)` takes the exact frame, not a seeks-nearest
        # approximation (`-ss` before `-i` is keyframe-accurate at best).
        select = f"select=eq(n\\,{index})"
        try:
            await run_ffmpeg(
                [
                    "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(render_path),
                    "-vf", select,
                    # `-fps_mode passthrough`: `-vsync` was removed in ffmpeg 9.
                    "-fps_mode", "passthrough",
                    "-frames:v", "1",
                    str(target),
                ],
                timeout_s=timeout_s,
            )
        except MediaToolError as exc:
            raise MediaToolError(
                f"thumbnail extraction failed at frame {index}: {exc}"
            ) from exc
        if not target.exists():
            raise MediaToolError(f"thumbnail frame {index} produced no file")
        written.append(target)
    return written
