"""Phase 5 render stages: S9 animatic (G2), S11 assemble, S12 render QC (G3).

S9 is free and purely local: it shows the accepted stills at the real timeline
timing over the real mastered audio, *before* any clip money is spent. The
animatic MP4 is deliberately not part of the G2 snapshot: x264 output is not
byte-stable across thread counts or versions, so the snapshot is the
deterministic input-content hash (keyframe shas + master sha + timeline hash).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from studio.assemble.segments import (
    join_segments,
    mux_audio,
    render_still_segment,
)
from studio.audio.timeline import build_timeline
from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.domain.enums import Language, Scope
from studio.domain.errors import ConfigError, MediaToolError, PreflightError
from studio.domain.refs import SubjectRef
from studio.pipeline.stage import StageOutput
from studio.video.encoders import EncoderProfile, profile_hash_for
from studio.video.geometry import destination_for, fit_plan
from studio.video.probe import probe

if TYPE_CHECKING:
    from studio.pipeline.context import RunContext

ANIMATIC_STAGE = "s9_animatic"


def _sha(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _episode(ctx: RunContext) -> Any:
    if ctx.episode_id is None:
        return None
    return ctx.repos.content.get_episode(ctx.episode_id)


def deliverable_subjects(ctx: RunContext, *, scope: Scope = Scope.DELIVERABLE) -> list[SubjectRef]:
    """Every deliverable in the run whose language is enabled."""
    episode = _episode(ctx)
    if episode is None:
        return []
    subjects: list[SubjectRef] = []
    for deliverable in ctx.repos.content.list_deliverables(episode.id):
        if Language(deliverable.language) not in ctx.languages:
            continue
        track = ctx.repos.content.get_track(deliverable.track_id)
        if track is None:
            continue
        subjects.append(
            SubjectRef(
                scope=scope,
                id=deliverable.id,
                lineage=(
                    (Scope.SERIES, episode.series_id),
                    (Scope.EPISODE, episode.id),
                    (Scope.TRACK, track.id),
                ),
            )
        )
    return subjects


def _stage_output_for(
    ctx: RunContext, *, stage: str, subject_id: str
) -> dict[str, Any] | None:
    for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage=stage):
        if row.subject_id != subject_id or row.status != "succeeded" or not row.output_json:
            continue
        try:
            data = json.loads(row.output_json)
        except json.JSONDecodeError:  # pragma: no cover - stage_runs is CHECKed
            continue
        if isinstance(data, dict):
            return data
    return None


def master_sha_for(ctx: RunContext, deliverable_id: str) -> str:
    """The S7b master this deliverable's render must carry."""
    output = _stage_output_for(ctx, stage="s7b_audio_mix", subject_id=deliverable_id)
    if output and output.get("master_sha256"):
        return str(output["master_sha256"])
    for row in ctx.repos.audio.list_masters(deliverable_id=deliverable_id):
        if row.status == "ready":
            return row.master_sha256
    raise PreflightError(
        f"deliverable {deliverable_id} has no ready audio master; run S7b first"
    )


def keyframe_sha_for(ctx: RunContext, shot_id: str) -> str:
    attempt = ctx.repos.content.current_shot_attempt(shot_id, kind="keyframe")
    if attempt is None or not attempt.asset_id:
        raise PreflightError(f"shot {shot_id} has no accepted keyframe; run S8 first")
    return attempt.asset_id


@dataclass(frozen=True)
class AnimaticShot:
    index: int
    shot_id: str
    attempt_id: str | None
    keyframe_sha: str
    start_frame: int
    end_frame: int
    start_ms: int
    clip_duration_ms: int
    narration: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "shot_id": self.shot_id,
            "attempt_id": self.attempt_id,
            "keyframe_sha": self.keyframe_sha,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "start_ms": self.start_ms,
            "clip_duration_ms": self.clip_duration_ms,
            "narration": self.narration,
        }


@dataclass(frozen=True)
class AnimaticResult:
    path: Path
    frames: int
    canvas: tuple[int, int]
    shots: list[AnimaticShot]


def animatic_profile(settings: Any) -> EncoderProfile:
    args: tuple[str, ...] = (
        "-c:v", "libx264",
        "-crf", str(int(settings.animatic.crf)),
        "-preset", str(settings.animatic.preset),
        "-pix_fmt", "yuv420p",
        "-fflags", "+bitexact",
    )
    return EncoderProfile(name="x264", args=args, profile_hash=profile_hash_for("animatic", args))


def _assert_j_cut_supported(settings: Any) -> None:
    if int(settings.timing.j_cut_ms) != 0:
        raise ConfigError(
            "timing.j_cut_ms must be 0 in Phase 5: overlapped audio requires a "
            "timeline change that S9/S11 deliberately never make"
        )


async def build_animatic(
    ctx: RunContext,
    deliverable_id: str,
    *,
    work_dir: Path,
) -> AnimaticResult:
    settings = ctx.config.settings
    _assert_j_cut_supported(settings)
    deliverable = ctx.repos.content.get_deliverable(deliverable_id)
    if deliverable is None:
        raise PreflightError(f"deliverable not found: {deliverable_id}")
    track = ctx.repos.content.get_track(deliverable.track_id)
    if track is None:
        raise PreflightError(f"track not found: {deliverable.track_id}")
    fps = int(settings.render.fps)
    timeline = build_timeline(ctx.repos.content, deliverable_id, settings)
    canvas = destination_for(track.aspect, int(settings.animatic.short_side_px))
    cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
    profile = animatic_profile(settings)

    shots: list[AnimaticShot] = []
    segments: list[Path] = []
    # Narration text is carried for the preview page only; it is escaped at
    # render time and never enters the video (no drawtext, §G2).
    line_rows = {row.id: row for row in ctx.repos.content.narration_lines(deliverable_id)}
    lines_by_shot: dict[str, list[dict[str, Any]]] = {}
    for line in timeline.lines:
        row = line_rows.get(line.line_id)
        lines_by_shot.setdefault(line.shot_id, []).append(
            {
                "line_id": line.line_id,
                "text": row.approved_text if row is not None else "",
                "start_sample": line.start_sample,
                "length_samples": line.length_samples,
            }
        )

    for span in timeline.shots:
        shot = ctx.repos.content.get_shot(span.shot_id)
        if shot is None:
            raise PreflightError(f"timeline references missing shot {span.shot_id}")
        attempt = ctx.repos.content.current_shot_attempt(shot.id, kind="keyframe")
        keyframe_sha = keyframe_sha_for(ctx, shot.id)
        keyframe_path = cas.resolve(keyframe_sha)
        info = await asyncio.to_thread(probe, keyframe_path)
        if info.width is None or info.height is None:
            raise MediaToolError(f"cannot read keyframe dimensions: {keyframe_path}")
        plan = fit_plan(
            int(info.width),
            int(info.height),
            canvas[0],
            canvas[1],
            tol_pct=float(settings.assemble.geometry_tolerance_pct),
        )
        if span.effective_frames <= 0:
            raise MediaToolError(f"shot {shot.id} has {span.effective_frames} frames")
        segment = await render_still_segment(
            src=keyframe_path,
            out=work_dir / f"still-{span.idx:05d}.mkv",
            frames=int(span.effective_frames),
            plan=plan,
            profile=profile,
            fps=fps,
        )
        segments.append(segment)
        if shot.clip_duration_ms is None:
            raise PreflightError(f"shot {shot.id} has no locked clip_duration_ms")
        shots.append(
            AnimaticShot(
                index=span.idx,
                shot_id=shot.id,
                attempt_id=attempt.id if attempt is not None else None,
                keyframe_sha=keyframe_sha,
                start_frame=span.start_frame,
                end_frame=span.start_frame + span.effective_frames,
                start_ms=round(span.start_frame * 1000 / fps),
                clip_duration_ms=int(shot.clip_duration_ms),
                narration=lines_by_shot.get(span.shot_id, []),
            )
        )

    joined = await join_segments(
        segments,
        work_dir / "animatic-video.mkv",
        max_inputs=int(settings.assemble.max_inputs_per_graph),
        work_dir=work_dir / "join",
    )
    master_path = cas.resolve(master_sha_for(ctx, deliverable_id))
    out = work_dir / f"animatic-{deliverable_id}.mp4"
    await mux_audio(
        joined,
        master_path,
        out,
        audio_bitrate_k=int(settings.animatic.audio_bitrate_k),
        sample_rate=int(settings.audio.sample_rate),
        channels=2,
    )

    expected_frames = int(timeline.total_frames)
    from studio.video.analysis import probe_frames

    counted = await probe_frames(out)
    if counted.frame_count != expected_frames:
        raise MediaToolError(
            f"animatic has {counted.frame_count} frames, expected {expected_frames}"
        )
    video_s = expected_frames / fps
    audio_s = counted.duration_s
    if audio_s is not None:
        drift_ms = abs(audio_s - video_s) * 1000.0
        if drift_ms > float(settings.assemble.av_sync.fail_ms):
            raise MediaToolError(
                f"animatic A/V drift {drift_ms:.1f} ms exceeds "
                f"{settings.assemble.av_sync.fail_ms} ms"
            )
    return AnimaticResult(
        path=out, frames=expected_frames, canvas=canvas, shots=shots
    )


class AnimaticHandler:
    """S9: per-deliverable animatic; carries Gate G2."""

    _STAGE = ANIMATIC_STAGE

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return deliverable_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        """E3: deterministic input content, never the MP4's bytes."""
        settings = ctx.config.settings
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        track = ctx.repos.content.get_track(deliverable.track_id)
        assert track is not None
        try:
            timeline = build_timeline(ctx.repos.content, subject.id, settings)
            timeline_payload: dict[str, Any] = {
                "timeline_hash": timeline.timeline_hash,
                "total_frames": timeline.total_frames,
                "total_samples": timeline.total_samples,
            }
            keyframes = [
                [span.shot_id, keyframe_sha_for(ctx, span.shot_id)] for span in timeline.shots
            ]
        except Exception as exc:  # run() surfaces the real error
            timeline_payload = {"timeline_error": str(exc)}
            keyframes = []
        try:
            master_sha: str | None = master_sha_for(ctx, subject.id)
        except PreflightError:
            master_sha = None
        return _sha(
            {
                "stage": self._STAGE,
                "deliverable_id": subject.id,
                "timeline": timeline_payload,
                "keyframes": keyframes,
                "master_sha256": master_sha,
                "aspect": track.aspect,
                "animatic": settings.animatic.model_dump(mode="json"),
                "fps": int(settings.render.fps),
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        settings = ctx.config.settings
        _assert_j_cut_supported(settings)
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        track = ctx.repos.content.get_track(deliverable.track_id)
        assert track is not None
        work_dir = (
            Path(settings.paths.projects)
            / str(ctx.episode_id)
            / "work"
            / f"s9_{subject.id}"
        )
        await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
        result = await build_animatic(ctx, subject.id, work_dir=work_dir)
        cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
        asset = await asyncio.to_thread(
            cas.put_file, result.path, kind="video", ext="mp4", retention="intermediate"
        )
        shots_json = work_dir / "animatic_shots.json"
        shots_json.write_text(
            json.dumps([shot.as_dict() for shot in result.shots], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        shots_asset = await asyncio.to_thread(
            cas.put_file, shots_json, kind="report", ext="json", retention="intermediate"
        )
        return StageOutput(
            output={
                "deliverable_id": subject.id,
                "animatic_sha256": asset.sha256,
                "animatic_shots_sha256": shots_asset.sha256,
                "frames": result.frames,
                "canvas": list(result.canvas),
                "aspect": track.aspect,
                "fps": int(settings.render.fps),
                "shots": [shot.as_dict() for shot in result.shots],
            }
        )
