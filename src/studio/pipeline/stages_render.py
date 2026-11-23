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
    SegmentSpec,
    join_segments,
    mux_audio,
    render_segment,
    render_still_segment,
)
from studio.audio.timeline import build_timeline
from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.domain.enums import Language, Scope
from studio.domain.errors import (
    ConfigError,
    MediaToolError,
    PreflightError,
    StudioError,
)
from studio.domain.refs import SubjectRef
from studio.pipeline.qc_render import (
    RENDER_QC_VERSION,
    run_render_qc,
    upstream_aggregate,
    write_qc_report,
)
from studio.pipeline.stage import StageOutput
from studio.video.encoders import (
    ASSEMBLER_VERSION,
    EncoderProfile,
    SubprocessProbeRunner,
    driver_string,
    profile_hash_for,
    resolve_encoder,
)
from studio.video.ffmpeg import ffmpeg_version
from studio.video.geometry import destination_for, fit_plan
from studio.video.probe import probe

if TYPE_CHECKING:
    from studio.pipeline.context import RunContext

ANIMATIC_STAGE = "s9_animatic"
ASSEMBLE_VERSION = ASSEMBLER_VERSION


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


# ===========================================================================
# S11 — assemble (per deliverable)
# ===========================================================================

ASSEMBLE_STAGE = "s11_assemble"


def _free_space_bytes(path: Path) -> int:
    """Injectable free-space probe (tests pass their own)."""
    import shutil

    probe_path = path
    while not probe_path.exists() and probe_path != probe_path.parent:
        probe_path = probe_path.parent
    return shutil.disk_usage(probe_path).free


@dataclass(frozen=True)
class AssembleShot:
    index: int
    shot_id: str
    clip_frames: int
    conformed_sha: str
    path: Path
    width: int
    height: int
    speed_factor: float = 1.0
    hold_frames: int = 0


@dataclass(frozen=True)
class AssemblePlan:
    shots: list[AssembleShot]
    total_frames: int
    canvas: tuple[int, int]
    master_sha: str
    master_samples: int
    preset: Any
    problems: list[str]


def _deliverable_preset(ctx: RunContext, deliverable: Any) -> Any:
    presets = ctx.config.presets
    try:
        return presets.for_channel(deliverable.channel_id)
    except Exception as exc:  # ConfigError: report it as a preflight problem
        raise PreflightError(str(exc)) from exc


async def assemble_plan(
    ctx: RunContext,
    deliverable_id: str,
    *,
    resolve_encoder: Any,
    free_space: Any = _free_space_bytes,
) -> AssemblePlan:
    """Free, fail-closed preflight: every problem is collected, never one at a time."""
    settings = ctx.config.settings
    problems: list[str] = []
    if int(settings.timing.j_cut_ms) != 0:
        problems.append("timing.j_cut_ms must be 0 in Phase 5 (S11 never overlaps audio)")
    deliverable = ctx.repos.content.get_deliverable(deliverable_id)
    if deliverable is None:
        raise PreflightError(f"deliverable not found: {deliverable_id}")
    track = ctx.repos.content.get_track(deliverable.track_id)
    if track is None:
        raise PreflightError(f"track not found: {deliverable.track_id}")
    preset = _deliverable_preset(ctx, deliverable)
    canvas = (int(preset.width), int(preset.height))
    try:
        timeline = build_timeline(ctx.repos.content, deliverable_id, settings)
    except StudioError as exc:
        # An unbuildable timeline (an S6 column missing) is one problem among
        # the others, not a crash: the operator sees every blocker at once.
        raise PreflightError(
            "assemble preflight failed; nothing was rendered:\n- " + str(exc)
        ) from exc
    cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)

    shots: list[AssembleShot] = []
    total_frames = 0
    for span in timeline.shots:
        shot = ctx.repos.content.get_shot(span.shot_id)
        if shot is None:
            problems.append(f"timeline references missing shot {span.shot_id}")
            continue
        attempt = ctx.repos.content.current_shot_attempt(shot.id, kind="clip")
        conformed = attempt.conformed_asset_id if attempt is not None else None
        if not conformed:
            problems.append(f"shot {shot.shot_no} ({shot.id}) has no accepted conformed clip")
            continue
        try:
            path = cas.resolve(conformed)
        except Exception as exc:
            problems.append(f"shot {shot.shot_no}: conformed clip unreadable ({exc})")
            continue
        try:
            info = await asyncio.to_thread(probe, path)
        except MediaToolError as exc:
            problems.append(f"shot {shot.shot_no}: ffprobe failed ({exc})")
            continue
        if info.width is None or info.height is None:
            problems.append(f"shot {shot.shot_no}: cannot read clip dimensions")
            continue
        try:
            fit_plan(
                int(info.width),
                int(info.height),
                canvas[0],
                canvas[1],
                tol_pct=float(settings.assemble.geometry_tolerance_pct),
            )
        except Exception as exc:
            problems.append(f"shot {shot.shot_no}: {exc}")
            continue
        clip_frames = int(span.effective_frames)
        total_frames += clip_frames
        shots.append(
            AssembleShot(
                index=span.idx,
                shot_id=shot.id,
                clip_frames=clip_frames,
                conformed_sha=conformed,
                path=path,
                width=int(info.width),
                height=int(info.height),
                speed_factor=float(shot.speed_factor),
                hold_frames=int(shot.hold_frames),
            )
        )

    master_sha = ""
    master_samples = 0
    try:
        master_sha = master_sha_for(ctx, deliverable_id)
        master_path = cas.resolve(master_sha)
        audio = await asyncio.to_thread(probe, master_path)
        master_samples = int((audio.duration_ms or 0) * int(settings.audio.sample_rate) / 1000)
    except Exception as exc:
        problems.append(f"audio master: {exc}")

    try:
        await resolve_encoder(settings, [(canvas[0], canvas[1])])
    except Exception as exc:
        problems.append(f"encoder: {exc}")

    need = sum(shot.path.stat().st_size for shot in shots if shot.path.exists())
    if master_sha:
        try:
            need += cas.resolve(master_sha).stat().st_size
        except MediaToolError:  # missing master is reported above
            pass
    free = free_space(Path(settings.paths.exports))
    required = int(
        need * float(settings.assemble.min_free_disk_factor)
    )
    if free < required:
        problems.append(
            f"InsufficientDisk: {free} bytes free, {required} required "
            f"({settings.assemble.min_free_disk_factor}x of {need})"
        )

    return AssemblePlan(
        shots=shots,
        total_frames=total_frames,
        canvas=canvas,
        master_sha=master_sha,
        master_samples=master_samples,
        preset=preset,
        problems=problems,
    )


def _rendered_video_ok(plan: AssemblePlan, fps: int) -> tuple[bool, str]:
    expected_s = plan.total_frames / fps
    if plan.total_frames <= 0 or expected_s <= 0:
        return False, "empty timeline"
    return True, ""


class AssembleHandler:
    """S11: geometry-applied segments -> concat join -> muxed MP4, verified."""

    _STAGE = ASSEMBLE_STAGE

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return deliverable_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        settings = ctx.config.settings
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        try:
            preset = _deliverable_preset(ctx, deliverable)
            timeline = build_timeline(ctx.repos.content, subject.id, settings)
            shas = []
            for span in timeline.shots:
                attempt = ctx.repos.content.current_shot_attempt(span.shot_id, kind="clip")
                shas.append(attempt.conformed_asset_id if attempt else None)
            master_sha: str | None = master_sha_for(ctx, subject.id)
            timeline_hash: str | None = timeline.timeline_hash
            total_frames: int | None = timeline.total_frames
        except Exception as exc:  # run() surfaces the real error
            return _sha({"stage": self._STAGE, "error": str(exc)})
        return _sha(
            {
                "stage": self._STAGE,
                "deliverable_id": subject.id,
                "conformed": shas,
                "master_sha256": master_sha,
                "timeline_hash": timeline_hash,
                "total_frames": total_frames,
                "preset": {
                    "aspect": preset.aspect.value,
                    "width": preset.width,
                    "height": preset.height,
                    "fps": preset.fps,
                    "video": preset.video.model_dump(mode="json"),
                    "audio": preset.audio.model_dump(mode="json"),
                },
                # Encoders produce different bytes: the profile hash is part
                # of the key (and of the render report).
                "encoder_mode": settings.encode.encoder,
                "encoder_profile_hash": profile_hash_for(
                    "resolve", [settings.encode.encoder, str(settings.encode.nvenc.cq),
                                str(settings.encode.x264.crf)]
                ),
                "assembler_version": ASSEMBLE_VERSION,
                "sample_rate": int(settings.audio.sample_rate),
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        settings = ctx.config.settings
        fps = int(settings.render.fps)
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None

        async def resolve(settings_obj: Any, geometries: list[tuple[int, int]]) -> Any:
            return await resolve_encoder(
                settings_obj,
                SubprocessProbeRunner(),
                geometries=geometries,
                cache_path=Path(settings_obj.paths.home) / settings_obj.encode.probe.cache_file,
                ffmpeg_version=".".join(str(p) for p in await ffmpeg_version()),
                driver=driver_string(),
            )

        plan = await assemble_plan(ctx, subject.id, resolve_encoder=resolve)
        if plan.problems:
            raise PreflightError(
                "assemble preflight failed; nothing was rendered:\n- "
                + "\n- ".join(plan.problems)
            )
        resolution = await resolve(settings, [plan.canvas])
        profile = resolution.profile
        if plan.preset.video.maxrate_k:
            profile = profile.with_bitrate(
                int(plan.preset.video.maxrate_k), int(plan.preset.video.bufsize_k)
            )

        work_dir = (
            Path(settings.paths.projects)
            / str(ctx.episode_id)
            / "work"
            / f"s11_{subject.id}"
            / self.input_hash(ctx, subject)[:16]
        )
        await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
        started = ctx.clock.now()
        parts: list[Path] = []
        geometry_report: list[dict[str, Any]] = []
        for shot in plan.shots:
            fit = fit_plan(
                shot.width,
                shot.height,
                plan.canvas[0],
                plan.canvas[1],
                tol_pct=float(settings.assemble.geometry_tolerance_pct),
            )
            spec = SegmentSpec(
                index=shot.index,
                shot_id=shot.shot_id,
                src=shot.path,
                clip_frames=shot.clip_frames,
                dst_w=plan.canvas[0],
                dst_h=plan.canvas[1],
                input_sha=shot.conformed_sha,
                speed_factor=shot.speed_factor,
                hold_frames=shot.hold_frames,
            )
            result = await render_segment(
                spec,
                plan=fit,
                profile=profile,
                fps=fps,
                root=work_dir / "segments",
                container=str(settings.assemble.segment_container),
            )
            parts.append(result.path)
            geometry_report.append(
                {"shot_id": shot.shot_id, **fit.report(), "reused": result.reused}
            )

        joined = await join_segments(
            parts,
            work_dir / "joined.mkv",
            max_inputs=int(settings.assemble.max_inputs_per_graph),
            work_dir=work_dir / "join",
        )
        cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
        master_path = cas.resolve(plan.master_sha)
        render_path = work_dir / f"render-{subject.id}.mp4"
        await mux_audio(
            joined,
            master_path,
            render_path,
            audio_bitrate_k=int(plan.preset.audio.bitrate_k),
            sample_rate=int(plan.preset.audio.sample_rate),
            channels=int(plan.preset.audio.channels),
        )

        from studio.video.analysis import probe_frames

        counted = await probe_frames(render_path)
        if counted.frame_count != plan.total_frames:
            raise MediaToolError(
                f"render has {counted.frame_count} frames, expected {plan.total_frames}"
            )
        expected_s = plan.total_frames / fps
        if counted.duration_s is not None:
            drift_ms = abs(counted.duration_s - expected_s) * 1000.0
            if drift_ms > float(settings.assemble.av_sync.fail_ms):
                raise MediaToolError(
                    f"A/V drift {drift_ms:.1f} ms exceeds "
                    f"{settings.assemble.av_sync.fail_ms} ms"
                )

        report = {
            "deliverable_id": subject.id,
            "encoder": profile.name,
            "encoder_args": list(profile.args),
            "encoder_reason": resolution.reason,
            "fallback_reason": resolution.fallback_reason,
            "encoder_profile_hash": profile.profile_hash,
            "assembler_version": ASSEMBLE_VERSION,
            "preset": {
                "aspect": plan.preset.aspect.value,
                "width": plan.canvas[0],
                "height": plan.canvas[1],
                "fps": fps,
            },
            "frames": plan.total_frames,
            "expected_duration_s": round(expected_s, 6),
            "probed_duration_s": counted.duration_s,
            "segment_count": len(parts),
            "max_inputs_per_graph": int(settings.assemble.max_inputs_per_graph),
            "geometry": geometry_report,
            "master_sha256": plan.master_sha,
            "master_samples": plan.master_samples,
            "segments_reused": sum(1 for item in geometry_report if item["reused"]),
            "timings": {
                "started_at": started.isoformat(),
                "elapsed_s": round((ctx.clock.now() - started).total_seconds(), 3),
            },
        }
        asset = await asyncio.to_thread(
            cas.put_file, render_path, kind="video", ext="mp4", retention="permanent"
        )
        report_path = work_dir / "render-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report_asset = await asyncio.to_thread(
            cas.put_file, report_path, kind="report", ext="json", retention="intermediate"
        )
        return StageOutput(
            output={
                "deliverable_id": subject.id,
                "render_sha256": asset.sha256,
                "render_report_sha256": report_asset.sha256,
                "frames": plan.total_frames,
                "fps": fps,
                "encoder": profile.name,
                "encoder_profile_hash": profile.profile_hash,
                "assembler_version": ASSEMBLE_VERSION,
                "segment_count": len(parts),
                "master_sha256": plan.master_sha,
            }
        )


RENDER_QC_STAGE = "s12_render_qc"


def _render_output(ctx: RunContext, deliverable_id: str) -> dict[str, Any]:
    """The S11 output for this deliverable, or a fail-closed preflight error."""
    rows = [
        row
        for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage=ASSEMBLE_STAGE)
        if row.status == "succeeded" and row.output_json
    ]
    for row in rows:
        if row.output_json is None:  # pragma: no cover - filtered above
            continue
        payload: dict[str, Any] = json.loads(row.output_json)
        if payload.get("deliverable_id") == deliverable_id:
            return payload
    raise PreflightError(
        f"no successful {ASSEMBLE_STAGE} output for deliverable {deliverable_id}; "
        "S12 only ever measures the final render"
    )


class RenderQcHandler:
    """S12: independent QC of the muxed MP4, then gate G3 on that render."""

    _STAGE = RENDER_QC_STAGE

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return deliverable_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        settings = ctx.config.settings
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        try:
            preset = _deliverable_preset(ctx, deliverable)
            render = _render_output(ctx, subject.id)
        except Exception as exc:  # run() reports the real reason
            return _sha({"stage": self._STAGE, "error": str(exc)})
        return _sha(
            {
                "stage": self._STAGE,
                "deliverable_id": subject.id,
                # E3: the G3 snapshot is the render sha plus the QC report sha,
                # so re-rendering or re-measuring blocks a stale approval.
                "render_sha256": render.get("render_sha256"),
                "render_report_sha256": render.get("render_report_sha256"),
                "preset": {
                    "aspect": preset.aspect.value,
                    "width": preset.width,
                    "height": preset.height,
                    "fps": preset.fps,
                    "video": preset.video.model_dump(mode="json"),
                    "audio": preset.audio.model_dump(mode="json"),
                },
                "qc_render": settings.qc.render.model_dump(mode="json"),
                "av_sync": settings.assemble.av_sync.model_dump(mode="json"),
                "qc_version": RENDER_QC_VERSION,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        settings = ctx.config.settings
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        preset = _deliverable_preset(ctx, deliverable)
        render = _render_output(ctx, subject.id)
        cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
        try:
            render_path = cas.resolve(str(render["render_sha256"]))
        except StudioError as exc:
            raise PreflightError(
                f"render asset {render['render_sha256']} is missing from the CAS "
                f"({exc}); re-run S11"
            ) from exc
        if not render_path.exists():
            raise PreflightError(
                f"render asset {render['render_sha256']} is missing from the CAS; "
                "re-run S11"
            )
        work_dir = (
            Path(settings.paths.projects)
            / str(ctx.episode_id)
            / "work"
            / f"s12_{subject.id}"
            / self.input_hash(ctx, subject)[:16]
        )
        metadata: dict[str, Any] | None = None
        if deliverable.metadata_json:
            try:
                parsed = json.loads(deliverable.metadata_json)
            except ValueError:  # pragma: no cover - column is JSON-checked
                parsed = None
            metadata = parsed if isinstance(parsed, dict) else None
        report = await run_render_qc(
            render_path,
            settings=settings,
            preset=preset,
            expected_frames=int(render["frames"]),
            master_sha256=render.get("master_sha256"),
            metadata=metadata,
            upstream=upstream_aggregate(
                ctx, ctx.run_id, track_id=getattr(deliverable, "track_id", None)
            ),
            work_dir=work_dir,
        )
        report_path = await write_qc_report(
            report,
            path=work_dir / "render-qc.json",
            extra={
                "deliverable_id": subject.id,
                "render_sha256": render["render_sha256"],
                "frames": int(render["frames"]),
            },
        )
        qc_asset = await asyncio.to_thread(
            cas.put_file, report_path, kind="report", ext="json", retention="intermediate"
        )
        if report.status == "fail":
            # Fail-fast, but the report is already in the CAS so the operator
            # can read exactly what failed (and its fix advice).
            details = "\n".join(
                f"- {check.name}: {check.detail or check.value}"
                f" (threshold {check.threshold})"
                for check in report.failures()
            )
            raise PreflightError(
                f"render QC failed for {subject.id} (qc report {qc_asset.sha256}):\n"
                + details
            )
        warnings = [check.name for check in report.checks if check.status == "warn"]
        return StageOutput(
            output={
                "deliverable_id": subject.id,
                "render_sha256": render["render_sha256"],
                "render_report_sha256": render.get("render_report_sha256"),
                "qc_report_sha256": qc_asset.sha256,
                "qc_status": report.status,
                "qc_version": RENDER_QC_VERSION,
                "frames": int(render["frames"]),
                "fps": int(preset.fps),
                "preset": {
                    "aspect": preset.aspect.value,
                    "width": preset.width,
                    "height": preset.height,
                },
                "warnings": warnings,
                "checks": [
                    {"name": check.name, "status": check.status}
                    for check in report.checks
                ],
            }
        )
