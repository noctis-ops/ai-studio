"""Audio plan builder (PHASE3 §H8, stage S7).

Derives the music/ambience/SFX cues for a *deliverable* from:

- the timeline (frame spans — the same source of truth as video), and
- the scene rows for the track (``music_mood`` / ``intensity`` /
  ``ambience_tags``), and
- the active library rows, and
- the series' recent music use (last ``recent_penalty_episodes`` episodes),

as a **pure function** plus a plan hash. No RNG anywhere; the tie-break is
``sha256(f"{episode_id}:{segment_idx}:{track_sha256}")`` (see selector.py).

Music segmentation:

- consecutive scenes merge into one segment while ``music_mood`` is equal and
  ``|Δintensity| < segment_intensity_break``;
- each segment picks the best-scoring active music track. If not one track
  mood-matches, the closest-by-intensity track is used and a
  ``music_no_mood_match`` warn is emitted (never an exception);
- zero active music tracks raises :class:`LibraryEmptyError` (fail closed);
- a loopable track covers its whole segment (``params.loop=true``). A
  non-loopable track shorter than the segment is chained with the
  next-best tracks, cues overlapping by ``crossfade_ms``;
- every cue carries ``gain_db`` (``music_lufs - loudness``, clamped to
  ±12 dB) and ``params.envelope`` — intensity offset breakpoints with
  ``intensity_ramp_ms`` linear ramps at scene boundaries.

Frame math uses integer frames everywhere; milliseconds appear only at the
config/DB boundary and are rounded half up.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from studio.audio.selector import (
    any_mood_match,
    closest_by_intensity,
    rank_music_tracks,
    select_ambience,
    select_transition_sfx,
)
from studio.config.vocab import Vocab
from studio.core.canonical import canonical_json
from studio.domain.errors import LibraryEmptyError


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _ms_to_frames(ms: float, fps: int) -> int:
    # round half up, per repo convention for ms → frame conversion
    return math.floor(ms * fps / 1000.0 + 0.5)


def _frames_to_samples(frames: int, samples_per_frame: int) -> int:
    return frames * samples_per_frame


@dataclass(frozen=True)
class MusicSegment:
    index: int
    scene_ids: tuple[str, ...]
    start_frame: int
    end_frame: int
    mood: str | None
    intensity: int


@dataclass(frozen=True)
class ScenePiece:
    """Scene span decorated with its plan inputs."""

    scene_id: str
    scene_no: int
    idx: int               # 0-based position in the timeline's scene spans
    start_frame: int
    end_frame: int
    mood: str | None
    intensity: int
    ambience_tags: tuple[str, ...]


@dataclass(frozen=True)
class PlannedCue:
    cue_index: int
    kind: str
    library_sha256: str
    scene_id: str | None
    start_frame: int
    end_frame: int
    source_offset_ms: int
    gain_db: float
    fade_in_ms: int
    fade_out_ms: int
    params: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "cue_index": self.cue_index,
            "kind": self.kind,
            "library_sha256": self.library_sha256,
            "scene_id": self.scene_id,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "source_offset_ms": self.source_offset_ms,
            "gain_db": round(self.gain_db, 3),
            "fade_in_ms": self.fade_in_ms,
            "fade_out_ms": self.fade_out_ms,
            "params": self.params,
        }


@dataclass(frozen=True)
class PlanResult:
    plan_hash: str
    cues: tuple[PlannedCue, ...]
    warnings: tuple[str, ...]
    attribution: tuple[Any, ...]  # AudioLibraryRow items with attribution_required

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"music": 0, "ambience": 0, "sfx": 0}
        for cue in self.cues:
            counts[cue.kind] = counts.get(cue.kind, 0) + 1
        return counts


def scene_pieces(timeline: Any, scenes: list[Any]) -> list[ScenePiece]:
    by_id = {scene.id: scene for scene in scenes}
    pieces: list[ScenePiece] = []
    for span in timeline.scenes:
        scene = by_id.get(span.scene_id)
        if scene is None:
            continue
        try:
            tags = tuple(str(t) for t in json.loads(scene.ambience_tags_json or "[]"))
        except (json.JSONDecodeError, TypeError):
            tags = ()
        pieces.append(
            ScenePiece(
                scene_id=span.scene_id,
                scene_no=scene.scene_no,
                idx=span.idx,
                start_frame=span.start_frame,
                end_frame=span.end_frame,
                mood=scene.music_mood,
                intensity=int(scene.intensity),
                ambience_tags=tags,
            )
        )
    pieces.sort(key=lambda piece: piece.idx)
    return pieces


def build_music_segments(pieces: list[ScenePiece], intensity_break: int) -> list[MusicSegment]:
    segments: list[MusicSegment] = []
    for piece in pieces:
        if (
            segments
            and segments[-1].mood == piece.mood
            and abs(segments[-1].intensity - piece.intensity) < intensity_break
        ):
            prev = segments[-1]
            count = len(prev.scene_ids) + 1
            mean = round((prev.intensity * (count - 1) + piece.intensity) / count)
            segments[-1] = MusicSegment(
                index=prev.index,
                scene_ids=(*prev.scene_ids, piece.scene_id),
                start_frame=prev.start_frame,
                end_frame=piece.end_frame,
                mood=prev.mood,
                intensity=mean,
            )
        else:
            segments.append(
                MusicSegment(
                    index=len(segments),
                    scene_ids=(piece.scene_id,),
                    start_frame=piece.start_frame,
                    end_frame=piece.end_frame,
                    mood=piece.mood,
                    intensity=piece.intensity,
                )
            )
    return segments


def build_ambience_segments(
    pieces: list[ScenePiece],
) -> list[tuple[tuple[str, ...], int, int, str | None]]:
    """Consecutive scenes with identical ambience tag sets → (tags, start, end, scene_id)."""
    segments: list[tuple[tuple[str, ...], int, int, str | None]] = []
    for piece in pieces:
        if not piece.ambience_tags:
            continue
        if segments and segments[-1][0] == piece.ambience_tags:
            tags, start, _end, first_id = segments[-1]
            segments[-1] = (tags, start, piece.end_frame, first_id)
        else:
            segments.append(
                (piece.ambience_tags, piece.start_frame, piece.end_frame, piece.scene_id)
            )
    return segments


def _envelope_points(
    pieces: list[ScenePiece],
    cue_start_frame: int,
    fps: int,
    samples_per_frame: int,
    sample_rate: int,
    intensity_offset_db: dict[int, float],
    ramp_ms: int,
) -> list[list[float]]:
    points: list[list[float]] = []
    ramp_s = ramp_ms / 1000.0
    relevant = [p for p in pieces if p.end_frame > cue_start_frame]
    if not relevant:
        return [[0.0, 0.0]]

    def off(piece: ScenePiece) -> float:
        return float(intensity_offset_db.get(int(piece.intensity), 0.0))

    def t_of(frame: int) -> float:
        samples = _frames_to_samples(frame - cue_start_frame, samples_per_frame)
        return max(0.0, samples / sample_rate)

    prev_off = off(relevant[0])
    points.append([0.0, prev_off])
    for piece in relevant[1:]:
        t = t_of(piece.start_frame)
        points.append([round(t, 6), prev_off])
        points.append([round(t + ramp_s, 6), off(piece)])
        prev_off = off(piece)
    # strictly increasing guard for the mixer's build_gain_expr (duplicates
    # from zero-length ramps get nudged one microsecond)
    fixed: list[list[float]] = []
    last = -1e-6
    for t, db in points:
        t = max(t, last + 1e-6)
        fixed.append([round(t, 6), round(db, 4)])
        last = t
    return fixed


def _gain(levels_lufs: float, loudness_lufs: float | None) -> float:
    if loudness_lufs is None:
        return 0.0
    return max(-12.0, min(12.0, float(levels_lufs) - float(loudness_lufs)))


def plan_audio(
    *,
    episode_id: str,
    total_frames: int,
    fps: int,
    sample_rate: int,
    timeline_hash: str,
    pieces: list[ScenePiece],
    music_rows: list[Any],
    ambience_rows: list[Any],
    sfx_rows: list[Any],
    recent_shas: set[str],
    settings: Any,
    vocab: Vocab,
) -> PlanResult:
    audio = settings.audio
    levels = audio.levels
    music_cfg = audio.music
    sfx_cfg = audio.sfx
    samples_per_frame = sample_rate // fps  # fps divides sample_rate (validated)
    total_ms = total_frames * 1000 // fps
    warnings: list[str] = []
    attribution: list[Any] = []
    planned: list[PlannedCue] = []

    # ---------------- music ----------------
    active_music = [row for row in music_rows if int(row.active) == 1]
    if not active_music:
        raise LibraryEmptyError(
            "audio plan: zero active music tracks in the library; "
            "run `studio library scan` and add music before S7"
        )

    segments = build_music_segments(pieces, int(music_cfg.segment_intensity_break))
    previous_sha: str | None = None
    music_cues: list[PlannedCue] = []
    crossfade_frames = (
        max(1, _ms_to_frames(float(music_cfg.crossfade_ms), fps))
        if music_cfg.crossfade_ms else 0
    )
    crossfade_ms = int(music_cfg.crossfade_ms)
    cue_index = 0
    for segment in segments:
        seg_frames = segment.end_frame - segment.start_frame
        seg_ms = seg_frames * 1000 // fps
        ranked = rank_music_tracks(
            active_music,
            mood=segment.mood,
            intensity=segment.intensity,
            episode_id=episode_id,
            segment_idx=segment.index,
            segment_duration_ms=seg_ms,
            recent_shas=recent_shas,
            previous_sha=previous_sha,
            affinity=vocab.mood_affinity,
            recent_penalty=float(music_cfg.recent_penalty),
            loop_penalty=float(music_cfg.loop_penalty),
        )
        if not ranked:
            raise LibraryEmptyError("audio plan: no usable music tracks")
        if not any_mood_match(active_music, segment.mood, vocab.mood_affinity):
            chosen_row = closest_by_intensity(active_music, segment.intensity)
            warnings.append(f"music_no_mood_match:segment:{segment.index}")
            assert chosen_row is not None
            chosen_ranked = [item for item in ranked if item.row.sha256 == chosen_row.sha256]
            ranked = (chosen_ranked or ranked[:1]) + [
                item for item in ranked if item.row.sha256 != chosen_row.sha256
            ]
        # coverage: pick the best ranked track; loopable pieces cover the
        # whole (remaining) segment; otherwise chain next-best tracks.
        picks: list[Any] = []
        coverage_frames = 0
        for item in ranked:
            row = item.row
            picks.append(row)
            coverage_frames += max(1, _ms_to_frames(int(row.duration_ms), fps))
            if int(row.loopable) == 1 or coverage_frames >= seg_frames:
                break
        else:
            if coverage_frames < seg_frames and picks:
                warnings.append(f"music_insufficient_coverage:segment:{segment.index}")

        cursor = segment.start_frame
        segment_cues: list[PlannedCue] = []
        for pick in picks:
            track_frames = max(1, _ms_to_frames(int(pick.duration_ms), fps))
            loopable = int(pick.loopable) == 1
            length_frames = (segment.end_frame - cursor) if loopable else track_frames
            end_frame = min(segment.end_frame, cursor + length_frames) \
                if not loopable else segment.end_frame
            segment_pieces = [p for p in pieces
                              if p.end_frame > cursor and p.start_frame < segment.end_frame]
            envelope = _envelope_points(
                segment_pieces, cursor, fps, samples_per_frame, sample_rate,
                levels.intensity_offset_db, int(levels.intensity_ramp_ms),
            )
            cue = PlannedCue(
                cue_index=0,  # renumbered per kind afterwards
                kind="music",
                library_sha256=pick.sha256,
                scene_id=None,  # assigned at renumber time (cue midpoint)
                start_frame=cursor,
                end_frame=end_frame,
                source_offset_ms=0,
                gain_db=_gain(levels.music_lufs, pick.loudness_lufs),
                fade_in_ms=0,
                fade_out_ms=0,
                params={
                    "segment_idx": segment.index,
                    "intensity": segment.intensity,
                    "mood": segment.mood,
                    "loop": bool(loopable),
                    "envelope": envelope,
                },
            )
            segment_cues.append(cue)
            previous_sha = pick.sha256
            cursor = end_frame
            if loopable:
                break
            if cursor >= segment.end_frame:
                break
        music_cues.extend(segment_cues)

    # ---- global music overlap (crossfades) + fades.
    # Adjacent music cues are crossfaded: the earlier cue's end extends by
    # half the crossfade, the later cue's start moves earlier by half.
    if crossfade_frames > 0:
        for i in range(len(music_cues) - 1):
            a, b = music_cues[i], music_cues[i + 1]
            overlap = crossfade_frames
            min_len = min(a.end_frame - a.start_frame, b.end_frame - b.start_frame)
            if min_len <= 2:
                continue
            overlap = min(overlap, min_len - 1)
            half = overlap // 2
            if b.start_frame - a.end_frame == 0:
                music_cues[i] = _replace_cue(a, end_frame=a.end_frame + (overlap - half))
                music_cues[i + 1] = _replace_cue(b, start_frame=b.start_frame - half)
    if music_cues:
        music_cues[0] = _replace_cue(music_cues[0], fade_in_ms=int(music_cfg.fade_in_ms))
        final_fade = min(int(music_cfg.final_fade_ms), total_ms // 4)
        last = music_cues[-1]
        music_cues[-1] = _replace_cue(last, end_frame=total_frames, fade_out_ms=final_fade)
    # overlapped neighbours get crossfade-length fades (equal-power qsin in the mixer)
    for i in range(len(music_cues) - 1):
        a, b = music_cues[i], music_cues[i + 1]
        if a.end_frame > b.start_frame and crossfade_ms > 0:
            music_cues[i] = _replace_cue(a, fade_out_ms=crossfade_ms)
            music_cues[i + 1] = _replace_cue(b, fade_in_ms=crossfade_ms)

    for i, cue in enumerate(music_cues):
        scene_id = None
        mid = (cue.start_frame + cue.end_frame) // 2
        for piece in pieces:
            if piece.start_frame <= mid < piece.end_frame:
                scene_id = piece.scene_id
                break
        music_cues[i] = _replace_cue(cue, cue_index=cue_index, scene_id=scene_id)
        cue_index += 1
    planned.extend(music_cues)

    # ---------------- ambience ----------------
    amb_segments = build_ambience_segments(pieces)
    amb_index = 0
    ambience_cues: list[PlannedCue] = []
    for tags, start_frame, end_frame, scene_id in amb_segments:
        seg_frames = end_frame - start_frame
        pick = select_ambience(ambience_rows, tags=list(tags),
                               episode_id=episode_id, segment_idx=amb_index)
        if pick is None:
            warnings.append(f"ambience_no_match:tags:{'+'.join(tags)}")
            continue
        gain = _gain(levels.ambience_lufs, pick.loudness_lufs)
        cue = PlannedCue(
            cue_index=amb_index,
            kind="ambience",
            library_sha256=pick.sha256,
            scene_id=scene_id,
            start_frame=start_frame,
            end_frame=end_frame,
            source_offset_ms=0,
            gain_db=gain,
            fade_in_ms=0,
            fade_out_ms=0,
            params={"tags": list(tags), "loop": True},
        )
        ambience_cues.append(cue)
        amb_index += 1
    planned.extend(ambience_cues)

    # ---------------- sfx (transitions at scene boundaries, not the first)
    sfx_index = 0
    if sfx_cfg.transition_enabled:
        boundaries = [p for p in pieces if p.idx > 0 and p.start_frame > 0]
        for boundary in boundaries:
            pick = select_transition_sfx(
                sfx_rows, tag=str(sfx_cfg.transition_tag),
                episode_id=episode_id, boundary_idx=boundary.idx,
            )
            if pick is None:
                warnings.append(f"no_transition_sfx_available:boundary:{boundary.idx}")
                continue
            offset_frames = _ms_to_frames(float(sfx_cfg.transition_offset_ms), fps)
            dur_frames = max(1, _ms_to_frames(int(pick.duration_ms), fps))
            start = max(0, boundary.start_frame + offset_frames)
            cue = PlannedCue(
                cue_index=sfx_index,
                kind="sfx",
                library_sha256=pick.sha256,
                scene_id=boundary.scene_id,
                start_frame=start,
                end_frame=start + dur_frames,
                source_offset_ms=0,
                gain_db=_gain(levels.sfx_lufs, pick.loudness_lufs),
                fade_in_ms=0,
                fade_out_ms=0,
                params={
                    "tag": str(sfx_cfg.transition_tag),
                    "boundary_scene_id": boundary.scene_id,
                    "offset_ms": int(sfx_cfg.transition_offset_ms),
                },
            )
            planned.append(cue)
            sfx_index += 1

    # ---------------- licensing: attribution rows for anything we used
    used = {cue.library_sha256 for cue in planned}
    for row in [*active_music, *ambience_rows, *sfx_rows]:
        if row.sha256 in used and int(row.attribution_required) == 1:
            attribution.append(row)

    plan_payload = {
        "timeline_hash": timeline_hash,
        "fps": fps,
        "sample_rate": sample_rate,
        "cues": [cue.to_payload() for cue in planned],
    }
    return PlanResult(
        plan_hash=_canonical_hash(plan_payload),
        cues=tuple(planned),
        warnings=tuple(warnings),
        attribution=tuple(attribution),
    )


def _replace_cue(cue: PlannedCue, **changes: Any) -> PlannedCue:
    return dataclasses.replace(cue, **changes)
