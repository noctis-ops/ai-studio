"""Timeline: single source of truth for sync (PHASE3 §H4).

Units: **frames** for video, **samples** for audio.
``samples_per_frame = sample_rate // fps`` (integer; validated by settings:
sample_rate % fps == 0, else ConfigError at load time and again here).

``clip_frames = round(clip_duration_ms * fps / 1000)``.
``effective_frames = ceil(clip_frames / speed_factor) + hold_frames``.
Shots are contiguous in ``idx`` order with hard cuts in Phase 3.

Line placement inside shot i::

    start = shot_start_samples + lead_in + Σ(previous line samples) + gaps

using ``lead_in_ms``, ``line_gap_ms`` and ``tail_ms`` from ``timing``
(milliseconds are rounded to samples, round half up).

INVARIANT: the last line of a shot ends at or before
``shot_end_samples - tail_samples``. A violation raises
``TimelineOverflowError`` listing the shot and the overflow in ms; nothing is
ever truncated silently.

``timeline_hash`` is the sha256 of the canonical JSON of every span and line
sha. **Phase 5's assembler must use this same function for video
concatenation.** Shot spans are a function of the track's shots only, never
of the language, so all deliverables of a track share spans.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Any

from studio.config.settings import TimingSettings
from studio.db.repos.content import ContentRepo
from studio.domain.errors import StudioError, TimelineOverflowError


@dataclass(frozen=True)
class ShotSpan:
    shot_id: str
    idx: int               # 0-based position in the track
    scene_id: str | None
    start_frame: int
    effective_frames: int


@dataclass(frozen=True)
class LinePlacement:
    line_id: str
    shot_id: str
    start_sample: int
    length_samples: int
    conformed_sha256: str


@dataclass(frozen=True)
class SceneSpan:
    scene_id: str
    idx: int               # 0-based position in the track
    start_frame: int
    end_frame: int


@dataclass(frozen=True)
class Timeline:
    fps: int
    sample_rate: int
    shots: tuple[ShotSpan, ...]
    scenes: tuple[SceneSpan, ...]
    lines: tuple[LinePlacement, ...]
    total_frames: int
    total_samples: int
    timeline_hash: str

    @property
    def samples_per_frame(self) -> int:
        return self.sample_rate // self.fps


def _ms_to_samples(ms: int | float, sample_rate: int) -> int:
    # round half up, all audio math stays in integer samples
    return int(ms * sample_rate / 1000 + 0.5)


def _canonical_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_timeline(
    repos: ContentRepo,
    deliverable_id: str,
    settings: Any,
) -> Timeline:
    deliverable = repos.get_deliverable(deliverable_id)
    if deliverable is None:
        raise StudioError(f"deliverable not found: {deliverable_id}")
    track = repos.get_track(deliverable.track_id)
    if track is None:
        raise StudioError(f"track not found for deliverable {deliverable_id}")

    fps = int(settings.render.fps)
    sample_rate = int(settings.audio.sample_rate)
    if sample_rate % fps != 0:
        # settings validation already forbids this; belt and braces
        from studio.domain.errors import ConfigError

        raise ConfigError(
            f"audio.sample_rate ({sample_rate}) must be divisible by render.fps ({fps})"
        )
    samples_per_frame = sample_rate // fps

    timing: TimingSettings = settings.timing
    lead_in = _ms_to_samples(timing.lead_in_ms, sample_rate)
    gap = _ms_to_samples(timing.line_gap_ms, sample_rate)
    tail = _ms_to_samples(timing.tail_ms, sample_rate)

    shots = sorted(repos.shots_for_track(track.id), key=lambda s: s.shot_no)
    if not shots:
        raise StudioError(f"track {track.id} has no shots")

    scenes = sorted(repos.list_scenes(track.id), key=lambda s: s.scene_no)
    scene_by_shot_range = _scene_map(scenes)

    shot_spans: list[ShotSpan] = []
    frame_cursor = 0
    for index, shot in enumerate(shots):
        if shot.clip_duration_ms is None:
            raise StudioError(
                f"shot {shot.shot_no} has no clip_duration_ms; run S6 (timing reconciler) first"
            )
        clip_frames = round(shot.clip_duration_ms * fps / 1000)
        effective = ceil(clip_frames / shot.speed_factor) + shot.hold_frames
        scene = scene_by_shot_range(shot.shot_no)
        shot_spans.append(
            ShotSpan(
                shot_id=shot.id,
                idx=index,
                scene_id=scene,
                start_frame=frame_cursor,
                effective_frames=effective,
            )
        )
        frame_cursor += effective

    total_frames = frame_cursor
    total_samples = total_frames * samples_per_frame

    lines = repos.narration_lines(deliverable_id)
    shot_index = {span.shot_id: span for span in shot_spans}
    shot_order = {span.shot_id: span.idx for span in shot_spans}
    ordered_lines = sorted(
        lines, key=lambda line: (shot_order.get(line.shot_id, 10**9), line.line_no)
    )

    placements: list[LinePlacement] = []
    by_shot: dict[str, list[Any]] = {}
    for line in ordered_lines:
        by_shot.setdefault(line.shot_id, []).append(line)

    for shot_id, shot_lines in by_shot.items():
        span = shot_index.get(shot_id)
        if span is None:
            raise StudioError(f"narration line references shot {shot_id} of another track")
        shot_start = span.start_frame * samples_per_frame
        shot_end = (span.start_frame + span.effective_frames) * samples_per_frame
        cursor = shot_start + lead_in
        for line in shot_lines:
            if line.conformed_samples is None or not line.conformed_sha256:
                raise StudioError(
                    f"narration line {line.id} has no conformed audio; run S5 (tts) first"
                )
            length = int(line.conformed_samples)
            placements.append(
                LinePlacement(
                    line_id=line.id,
                    shot_id=shot_id,
                    start_sample=cursor,
                    length_samples=length,
                    conformed_sha256=line.conformed_sha256,
                )
            )
            cursor += length + gap
        last_end = cursor - gap if shot_lines else cursor
        available_end = shot_end - tail
        if last_end > available_end:
            overflow_samples = last_end - available_end
            overflow_ms = overflow_samples * 1000 / sample_rate
            shot_row = next(s for s in shots if s.id == shot_id)
            need_ms = (last_end - shot_start) * 1000 / sample_rate
            avail_ms = (available_end - shot_start) * 1000 / sample_rate
            raise TimelineOverflowError(
                f"shot {shot_row.shot_no} overflows by {overflow_ms:.1f} ms "
                f"(narration needs {need_ms:.1f} ms, shot holds {avail_ms:.1f} ms "
                f"after {timing.lead_in_ms} ms lead-in and {timing.tail_ms} ms tail); "
                f"nothing was truncated"
            )

    placements.sort(key=lambda p: p.start_sample)

    scene_spans: list[SceneSpan] = []
    for index, scene_row in enumerate(scenes):
        first_span = next((s for s in shot_spans if s.idx == scene_row.shot_from - 1), None)
        last_span = next((s for s in shot_spans if s.idx == scene_row.shot_to - 1), None)
        if first_span is None or last_span is None:
            raise StudioError(
                f"scene {scene_row.scene_no} references shot range "
                f"{scene_row.shot_from}-{scene_row.shot_to} outside the track"
            )
        scene_spans.append(
            SceneSpan(
                scene_id=scene_row.id,
                idx=index,
                start_frame=first_span.start_frame,
                end_frame=last_span.start_frame + last_span.effective_frames,
            )
        )

    timeline_hash = _canonical_hash(
        {
            "fps": fps,
            "sample_rate": sample_rate,
            "shots": [
                {
                    "shot_id": s.shot_id,
                    "start_frame": s.start_frame,
                    "effective_frames": s.effective_frames,
                    "scene_id": s.scene_id,
                }
                for s in shot_spans
            ],
            "scenes": [
                {"scene_id": s.scene_id, "start_frame": s.start_frame, "end_frame": s.end_frame}
                for s in scene_spans
            ],
            "lines": [
                {
                    "line_id": p.line_id,
                    "shot_id": p.shot_id,
                    "start_sample": p.start_sample,
                    "length_samples": p.length_samples,
                    "sha256": p.conformed_sha256,
                }
                for p in placements
            ],
            "total_frames": total_frames,
            "total_samples": total_samples,
        }
    )

    return Timeline(
        fps=fps,
        sample_rate=sample_rate,
        shots=tuple(shot_spans),
        scenes=tuple(scene_spans),
        lines=tuple(placements),
        total_frames=total_frames,
        total_samples=total_samples,
        timeline_hash=timeline_hash,
    )


def _scene_map(scenes: Sequence[Any]) -> Callable[[int], str | None]:
    ranges = [(s.scene_no, s.shot_from, s.shot_to, s.id) for s in scenes]

    def find(shot_no: int) -> str | None:
        for _, start, end, scene_id in ranges:
            if start <= shot_no <= end:
                return str(scene_id)
        return None

    return find
