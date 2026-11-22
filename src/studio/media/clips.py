"""S10 clips (locked S6 duration, conform, clip QC).

Split from ``studio.media.stages`` in Phase 5 (H5.8) as a pure move.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from studio.core.cas import ContentStore
from studio.core.clock import format_ts
from studio.domain.enums import Aspect
from studio.domain.errors import (
    FailedPolicy,
    MediaCircuitOpen,
    MediaJobFailedError,
    PermanentProviderError,
    PreflightError,
    VisualQcError,
)
from studio.domain.refs import AssetRef, SubjectRef
from studio.media.common import (
    _episode,
    _model_entry,
    _pinned_bible,
    _sha,
    _shot_plan,
    _shot_subjects,
    _style_tokens,
    _video_request,
    logger,
)
from studio.media.conform import clip_frames_for, conform_clip
from studio.media.preflight import _worst_case_pause
from studio.media.prompts import (
    PromptSpec,
    compose_motion_prompt,
    compose_motion_prompt_spec,
    prompt_version_sha,
)
from studio.media.refs import prepare_refs
from studio.media.retry import (
    QcOutcome,
    _attempt_persist,
    _base_nonce,
    _ensure_attempt,
    _generate_with_retries,
    _media_failure_audit,
    _qc_status,
    _resume_state,
)
from studio.media.sheets import _estimate_clip_cost
from studio.pipeline.stage import StageOutput
from studio.providers.base import MediaRequest
from studio.qc.visual import QcCheck, qc_video

if TYPE_CHECKING:
    from studio.domain.models import ShotAttemptRow, ShotRow
    from studio.pipeline.context import RunContext

class ClipGenHandler:
    _STAGE = "s10_clips"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _shot_subjects(ctx)

    def _attempt(self, ctx: RunContext, shot: ShotRow) -> ShotAttemptRow | None:
        return ctx.repos.content.current_shot_attempt(shot.id, kind="clip")

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        shot = ctx.repos.content.get_shot(subject.id)
        assert shot is not None
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        episode = _episode(ctx)
        assert episode is not None
        attempt = self._attempt(ctx, shot)
        keyframe = ctx.repos.content.current_shot_attempt(shot.id, kind="keyframe")
        bible = _pinned_bible(ctx, episode.id)
        settings = ctx.config.settings
        home = Path(settings.paths.home)
        model_key, _entry = _model_entry(ctx, "clips")
        return _sha(
            {
                "stage": self._STAGE,
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no if attempt else 0,
                "nonce": attempt.nonce if attempt else None,
                "model_key": model_key,
                "aspect": track.aspect,
                "keyframe_sha": keyframe.asset_id if keyframe else None,
                "clip_duration_ms": shot.clip_duration_ms,
                "fps": int(settings.render.fps),
                "style_tokens": _style_tokens(bible),
                "prompt_version_sha": prompt_version_sha(home),
                "sanitizer_model_key": settings.sanitizer.model_key,
                "conform": {
                    "crf": settings.media.clip.conform_crf,
                    "duration_tolerance_ms": settings.media.clip.duration_tolerance_ms,
                    "strip_audio": settings.media.clip.strip_audio,
                },
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        shot = ctx.repos.content.get_shot(subject.id)
        if shot is None:
            raise PermanentProviderError(f"shot not found: {subject.id}")
        track = ctx.repos.content.get_track(shot.track_id)
        if track is None:
            raise PermanentProviderError(f"track not found: {shot.track_id}")
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        home = Path(settings.paths.home)
        aspect = Aspect(track.aspect)
        fps = int(settings.render.fps)
        locked_ms = int(shot.clip_duration_ms or 0)
        if locked_ms <= 0:
            raise PreflightError(
                f"shot {shot.shot_no} ({shot.id}) has no locked duration; run S6 first"
            )
        clip_frames = clip_frames_for(locked_ms, fps)
        model_key, entry = _model_entry(ctx, "clips")
        duration_s = locked_ms / 1000.0

        # ---- the accepted keyframe (G2) conditions the clip
        keyframe_attempt = ctx.repos.content.current_shot_attempt(shot.id, kind="keyframe")
        if keyframe_attempt is None or not keyframe_attempt.asset_id:
            raise PreflightError(
                f"shot {shot.shot_no} ({shot.id}) has no keyframe attempt; run the "
                f"keyframe stage first"
            )
        keyframe_asset_row = ctx.repos.assets.get(keyframe_attempt.asset_id)
        if keyframe_asset_row is None:
            raise PermanentProviderError(
                f"keyframe asset {keyframe_attempt.asset_id} is missing from the CAS index"
            )
        keyframe_ref = AssetRef(
            sha256=keyframe_asset_row.sha256,
            rel_path=keyframe_asset_row.rel_path,
            kind=keyframe_asset_row.kind,
            mime=keyframe_asset_row.mime,
            size_bytes=keyframe_asset_row.size_bytes,
            duration_ms=keyframe_asset_row.duration_ms,
            width=keyframe_asset_row.width,
            height=keyframe_asset_row.height,
        )

        cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
        prepared = await prepare_refs(cas, [keyframe_ref], settings)
        keyframe_prepared = prepared[0]

        # ---- worst-case budget for this shot before any spend
        worst = _estimate_clip_cost(entry, clip_duration_ms=locked_ms)
        if worst is not None:
            worst = int(
                worst * (1 + float(settings.media.assumed_retry_rate.get("video", 0.30)))
            ) + 1
            _worst_case_pause(
                ctx,
                episode,
                worst_case_musd=worst,
                label=f"clip for shot {shot.shot_no}",
            )

        plan = _shot_plan(ctx, track.id)
        camera = str(plan.get(shot.shot_no, {}).get("camera", "") or "")
        motion_source = keyframe_attempt.prompt_used or shot.prompt
        motion = compose_motion_prompt(
            shot_visual=motion_source,
            camera=camera,
            home=home,
        )
        def make_prompt(scene_override: str | None) -> PromptSpec:
            if scene_override is not None:
                base = compose_motion_prompt_spec(motion=motion, home=home)
                return base.with_scene(scene_override)
            return compose_motion_prompt_spec(motion=motion, home=home)

        def make_request(spec: PromptSpec, seed: int, nonce: str) -> MediaRequest:
            return _video_request(
                ctx,
                entry=entry,
                prompt=spec.full_prompt,
                keyframe=keyframe_ref,
                keyframe_prepared=keyframe_prepared,
                aspect=aspect,
                duration_s=duration_s,
                seed=seed,
                prompt_sha=spec.prompt_version_sha,
            )

        async def qc_run(path: Path) -> QcOutcome:
            # Keep the raw clip (retention keep_n_days) so re-mixing never repays.
            ext = path.suffix.lstrip(".") or "mp4"
            purge_after = format_ts(
                ctx.clock.now()
                + timedelta(days=int(settings.retention.keep_raw_clips_days))
            )
            raw_ref: AssetRef | None = None
            try:
                raw_ref = cas.put_file(
                    path,
                    kind="video",
                    ext=ext,
                    retention="keep_n_days",
                    purge_after=purge_after,
                    move=False,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("could not upgrade raw clip retention: %s", exc)
            if raw_ref is not None:
                raw_sha = raw_ref.sha256
            else:  # pragma: no cover - defensive
                raw_sha = await asyncio.to_thread(
                    lambda: hashlib.sha256(path.read_bytes()).hexdigest()
                )
            work_dir = Path(settings.paths.projects) / str(episode.id) / "work"
            try:
                conform = await conform_clip(
                    path,
                    clip_duration_ms=locked_ms,
                    settings=settings,
                    work_dir=work_dir,
                )
            except VisualQcError as exc:
                return (
                    [
                        QcCheck(
                            name="conform",
                            status="fail",
                            retryable=True,
                            value=str(exc),
                            threshold="conform within tolerance",
                        )
                    ],
                    None,
                    {"conform_error": str(exc), "raw_sha256": raw_sha},
                )
            if conform.already_compliant:
                if raw_ref is not None:
                    conformed_asset = raw_ref
                else:
                    row = ctx.repos.assets.get(raw_sha)
                    if row is None:
                        raise PermanentProviderError(
                            f"raw clip {raw_sha} is not in the CAS asset index"
                        )
                    conformed_asset = AssetRef(
                        sha256=row.sha256,
                        rel_path=row.rel_path,
                        kind=row.kind,
                        mime=row.mime,
                        size_bytes=row.size_bytes,
                        duration_ms=row.duration_ms,
                        width=row.width,
                        height=row.height,
                    )
            else:
                conformed_asset = cas.put_file(
                    conform.path,
                    kind="video",
                    ext="mp4",
                    retention="intermediate",
                    move=True,
                )
                conform.path.unlink(missing_ok=True)
            conformed_path = cas.resolve(conformed_asset.sha256)
            keyframe_path = cas.resolve(keyframe_ref.sha256)
            checks = await qc_video(
                conformed_path,
                aspect=aspect,
                settings=settings,
                keyframe_path=keyframe_path if keyframe_path.exists() else None,
                expected_frames=clip_frames,
                expected_fps=fps,
            )
            report = {
                **conform.report,
                "conformed_sha256": conformed_asset.sha256,
                "already_compliant": conform.already_compliant,
            }
            return checks, conformed_asset, report

        attempt = _ensure_attempt(ctx, shot=shot, kind="clip", prompt=shot.prompt)
        persist = _attempt_persist(ctx, attempt.id)
        resume_state = _resume_state(attempt)
        base_nonce = (
            _base_nonce(attempt.nonce) if resume_state is not None else attempt.nonce
        )
        try:
            outcome = await _generate_with_retries(
                ctx,
                episode_id=episode.id,
                subject_key=shot.id,
                purpose="clip",
                kind="video",
                model_key=model_key,
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce=base_nonce,
                max_paid_attempts=int(settings.media.max_paid_attempts_per_shot),
                auto_retries=int(settings.media.qc.video.auto_retries),
                qc_run=qc_run,
                persist=persist,
                resume_state=resume_state,
                accumulate_cost=int(attempt.cost_musd),
            )
        except (FailedPolicy, VisualQcError, MediaCircuitOpen, MediaJobFailedError) as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
            if isinstance(exc, MediaCircuitOpen):
                ctx.repos.audit.append(
                    actor="system",
                    entity=f"run:{ctx.run_id}",
                    event="media_circuit_open",
                    payload={
                        "stage": self._STAGE,
                        "episode_id": episode.id,
                        "subject_id": subject.id,
                        "error": str(exc),
                    },
                )
            raise

        warnings: list[str] = []
        warnings.extend(f"qc_warn:{c.name}" for c in outcome.checks if c.status == "warn")
        if outcome.sanitize_level:
            warnings.append(f"sanitized:{outcome.sanitize_level}")

        # clip_duration_ms / clip_frames are NEVER regenerated by this stage.
        report = dict(outcome.report)
        return StageOutput(
            output={
                "shot_id": shot.id,
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "kind": "clip",
                "asset_sha256": outcome.asset.sha256,
                "conformed_asset_id": (
                    outcome.conformed_asset.sha256 if outcome.conformed_asset else None
                ),
                "clip_duration_ms": locked_ms,
                "clip_frames": clip_frames,
                "fps": fps,
                "conform": report,
                "warnings": warnings,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc_status": _qc_status(outcome.checks),
                "qc": [c.as_dict() for c in outcome.checks],
                "model_key": model_key,
            }
        )
