"""S8 keyframes (Gate G2 is created by the engine on this stage).

Split from ``studio.media.stages`` in Phase 5 (H5.8) as a pure move.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from studio.domain.enums import Aspect
from studio.domain.errors import (
    FailedPolicy,
    MediaCircuitOpen,
    PermanentProviderError,
    VisualQcError,
)
from studio.domain.money import usd_to_musd
from studio.domain.refs import AssetRef, SubjectRef
from studio.media.common import (
    _character_visual_spec,
    _episode,
    _image_request,
    _model_entry,
    _pinned_bible,
    _prepare_input_assets,
    _ref_assets_for_shot,
    _ref_clause,
    _sha,
    _shot_plan,
    _shot_subjects,
    _style_tokens,
)
from studio.media.preflight import _estimate_drift, _worst_case_pause
from studio.media.prompts import (
    PromptSpec,
    compose_keyframe_prompt,
    prompt_version_sha,
)
from studio.media.registry import (
    media_spec,
)
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
from studio.media.sheets import _estimate_image_cost
from studio.pipeline.stage import StageOutput
from studio.providers.base import MediaRequest
from studio.qc.visual import QcCheck

if TYPE_CHECKING:
    from studio.domain.models import ShotAttemptRow, ShotRow, TrackRow
    from studio.pipeline.context import RunContext

async def _qc_image_compat(path: Path, *, aspect: Aspect, settings: Any) -> list[QcCheck]:
    """Call the shim module's ``qc_image`` so the Phase 4 monkeypatch contract
    (``monkeypatch.setattr(studio.media.stages, "qc_image", ...)``) keeps
    working after the H5.8 pure-move split."""
    from studio.media import stages as shim

    return await shim.qc_image(path, aspect=aspect, settings=settings)


class KeyframeHandler:
    _STAGE = "s8_keyframes"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _shot_subjects(ctx)

    def _attempt(self, ctx: RunContext, shot: ShotRow) -> ShotAttemptRow | None:
        return ctx.repos.content.current_shot_attempt(shot.id, kind="keyframe")

    def _approved_refs(
        self, ctx: RunContext, shot: ShotRow, track: TrackRow
    ) -> tuple[list[tuple[str, str, AssetRef]], bool, list[str]]:
        episode = _episode(ctx)
        assert episode is not None
        plan = _shot_plan(ctx, track.id)
        characters = plan.get(shot.shot_no, {}).get("characters", [])
        names = [str(name) for name in characters] if isinstance(characters, list) else []
        _key, entry = _model_entry(ctx, "keyframes")
        refs, truncated = _ref_assets_for_shot(
            ctx, series_id=episode.series_id, characters=names, entry=entry
        )
        return refs, truncated, names

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        shot = ctx.repos.content.get_shot(subject.id)
        assert shot is not None
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        episode = _episode(ctx)
        assert episode is not None
        attempt = self._attempt(ctx, shot)
        refs, _truncated, _names = self._approved_refs(ctx, shot, track)
        bible = _pinned_bible(ctx, episode.id)
        settings = ctx.config.settings
        home = Path(settings.paths.home)
        model_key, _entry = _model_entry(ctx, "keyframes")
        return _sha(
            {
                "stage": self._STAGE,
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no if attempt else 0,
                "nonce": attempt.nonce if attempt else None,
                "prompt": attempt.prompt_used if attempt else shot.prompt,
                "model_key": model_key,
                "aspect": track.aspect,
                "refs": [ref.sha256 for _n, _r, ref in refs],
                "style_tokens": _style_tokens(bible),
                "prompt_version_sha": prompt_version_sha(home),
                "sanitizer_model_key": settings.sanitizer.model_key,
                "ref_max_side_px": settings.media.ref_max_side_px,
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
        bible = _pinned_bible(ctx, episode.id)
        style_tokens = _style_tokens(bible)
        plan = _shot_plan(ctx, track.id)
        plan_entry = plan.get(shot.shot_no, {})
        names = [str(n) for n in plan_entry.get("characters", [])] if isinstance(
            plan_entry.get("characters"), list
        ) else []

        model_key, entry = _model_entry(ctx, "keyframes")
        ref_specs, refs_truncated = _ref_assets_for_shot(
            ctx, series_id=episode.series_id, characters=names, entry=entry
        )
        ordered_refs = [ref for _n, _r, ref in ref_specs]
        ref_clauses = [
            (name, index + 1) for index, (name, _role, _ref) in enumerate(ref_specs)
        ]
        input_assets, prepared = await _prepare_input_assets(ctx, ordered_refs)

        # ---- worst-case budget for this shot before any spend
        worst = _estimate_image_cost(entry, aspect=aspect)
        if worst is not None:
            worst = int(
                worst * (1 + float(settings.media.assumed_retry_rate.get("image", 0.25)))
            ) + 1
            _worst_case_pause(
                ctx,
                episode,
                worst_case_musd=worst,
                label=f"keyframe for shot {shot.shot_no}",
            )

        existing_attempt = self._attempt(ctx, shot)
        # A human edit at G2 lives on the (kind=keyframe) attempt's
        # prompt_used; it wins over the shot's original prompt.
        scene_source = (
            existing_attempt.prompt_used
            if existing_attempt is not None and existing_attempt.prompt_used
            else shot.prompt
        )
        characters = [
            (name, _character_visual_spec(bible, name)) for name in names
        ]
        ref_clause_template = _ref_clause(entry)

        def make_prompt(scene_override: str | None) -> PromptSpec:
            spec = compose_keyframe_prompt(
                style_tokens=style_tokens,
                characters=characters,
                shot_visual=scene_source,
                ref_clause_template=ref_clause_template,
                ref_clauses=ref_clauses or None,
                home=home,
            )
            if scene_override is not None:
                spec = spec.with_scene(scene_override)
            return spec

        def make_request(spec: PromptSpec, seed: int, nonce: str) -> MediaRequest:
            return _image_request(
                ctx,
                entry=entry,
                spec=spec,
                negative=spec.negative if "negative" in media_spec(entry).param_map else None,
                input_assets=input_assets,
                prepared=prepared,
                aspect=aspect,
                seed=seed,
                nonce=nonce,
                purpose="keyframe",
            )

        async def qc_run(path: Path) -> QcOutcome:
            checks = await _qc_image_compat(path, aspect=aspect, settings=settings)
            return checks, None, {}

        attempt = _ensure_attempt(ctx, shot=shot, kind="keyframe", prompt=shot.prompt)
        persist = _attempt_persist(ctx, attempt.id)
        resume_state = _resume_state(attempt)
        base_nonce = _base_nonce(attempt.nonce) if resume_state is not None else attempt.nonce
        try:
            outcome = await _generate_with_retries(
                ctx,
                episode_id=episode.id,
                subject_key=shot.id,
                purpose="keyframe",
                kind="image",
                model_key=model_key,
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce=base_nonce,
                max_paid_attempts=int(settings.media.max_paid_attempts_per_shot),
                auto_retries=int(settings.media.qc.image.auto_retries),
                qc_run=qc_run,
                persist=persist,
                resume_state=resume_state,
                accumulate_cost=int(attempt.cost_musd),
            )
        except (FailedPolicy, VisualQcError) as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
            raise
        except MediaCircuitOpen as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
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
        if refs_truncated:
            warnings.append("refs_truncated")
        warnings.extend(f"qc_warn:{c.name}" for c in outcome.checks if c.status == "warn")
        if outcome.sanitize_level:
            warnings.append(f"sanitized:{outcome.sanitize_level}")

        # ---- clip cost projection from S6's locked durations (episode-wide)
        projected_first_pass = _project_clip_cost(ctx, episode.id)
        projected_with_retries = _project_clip_cost(
            ctx, episode.id, with_retries=True
        )
        drift = _estimate_drift(ctx, projected_musd=projected_first_pass or 0)

        return StageOutput(
            output={
                "shot_id": shot.id,
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "kind": "keyframe",
                "asset_sha256": outcome.asset.sha256,
                "refs": [ref.sha256 for ref in ordered_refs],
                "refs_truncated": refs_truncated,
                "warnings": warnings,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc_status": _qc_status(outcome.checks),
                "qc": [c.as_dict() for c in outcome.checks],
                "model_key": model_key,
                "projected_clip_cost_musd_first_pass": projected_first_pass,
                "projected_clip_cost_musd_with_retries": projected_with_retries,
                **({"estimate_drift": drift} if drift else {}),
            }
        )


def _project_clip_cost(
    ctx: RunContext, episode_id: str, *, with_retries: bool = False
) -> int | None:
    """Σ over all shots of the episode: locked clip_duration_ms x clips price.

    Returns None when any locked duration is missing or the pricing unit is
    not representable — never a guessed number.
    """
    from decimal import Decimal

    from studio.config.registry import PerSecond, PerVideo

    episode = ctx.repos.content.get_episode(episode_id)
    if episode is None:
        return None
    shots = ctx.repos.content.list_shots(episode_id)
    try:
        _key, entry = _model_entry(ctx, "clips")
    except Exception:
        return None
    if isinstance(entry.pricing, PerVideo):
        per = usd_to_musd(entry.pricing.usd)
        total = sum(per for shot in shots if shot.clip_duration_ms is not None)
    elif isinstance(entry.pricing, PerSecond):
        total = 0
        seconds_total = Decimal(0)
        for shot in shots:
            if shot.clip_duration_ms is None:
                return None
            seconds_total += Decimal(shot.clip_duration_ms) / Decimal(1000)
        total = usd_to_musd(seconds_total * entry.pricing.usd)
    else:
        return None
    if with_retries:
        rate = float(ctx.config.settings.media.assumed_retry_rate.get("video", 0.30))
        import math

        total = math.ceil(total * (1.0 + rate))
    return int(total)
