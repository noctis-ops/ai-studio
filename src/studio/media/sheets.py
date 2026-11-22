"""S8a character sheets (Gate G0) and the sheet cost/geometry helpers.

Split from ``studio.media.stages`` in Phase 5 (H5.8) as a pure move.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from studio.config.registry import ModelEntry
from studio.core.canonical import canonical_json
from studio.domain.enums import Aspect, Scope
from studio.domain.errors import (
    FailedPolicy,
    MediaCircuitOpen,
    MediaJobFailedError,
    PermanentProviderError,
    PreflightError,
    StudioError,
    VisualQcError,
)
from studio.domain.money import usd_to_musd
from studio.domain.refs import AssetRef, SubjectRef
from studio.media.common import (
    _bible_character,
    _character_visual_spec,
    _episode,
    _image_request,
    _model_entry,
    _pinned_bible,
    _prepare_input_assets,
    _ref_clause,
    _sha,
    _style_tokens,
)
from studio.media.preflight import _worst_case_pause
from studio.media.prompts import (
    PromptSpec,
    compose_sheet_prompt,
    prompt_version_sha,
)
from studio.media.registry import (
    media_spec,
)
from studio.media.retry import QcOutcome, _generate_with_retries, _media_failure_audit, _qc_status
from studio.pipeline.stage import StageOutput
from studio.providers.base import MediaRequest
from studio.qc.visual import qc_image

if TYPE_CHECKING:
    from studio.pipeline.context import RunContext

def _pixel_dims_for(entry: ModelEntry, aspect: Aspect) -> tuple[int, int] | None:
    raw: dict[str, Any] = {}
    raw.update(media_spec(entry).fixed_params)
    raw.update(media_spec(entry).aspect_extra_params.get(aspect.value, {}))
    width, height = raw.get("width"), raw.get("height")
    if width is None or height is None:
        return None
    if "<CONFIRM" in str(width) or "<CONFIRM" in str(height):
        return None
    try:
        return int(width), int(height)
    except (TypeError, ValueError):
        return None


def _estimate_image_cost(entry: ModelEntry, *, aspect: Aspect) -> int | None:
    """Per-image worst case in micro-USD; None when not computable offline."""
    from decimal import Decimal

    from studio.config.registry import PerImage, PerMegapixel, PerVideo

    pricing = entry.pricing
    if isinstance(pricing, PerImage):
        return usd_to_musd(pricing.usd)
    if isinstance(pricing, PerVideo):
        return usd_to_musd(pricing.usd)
    if isinstance(pricing, PerMegapixel):
        dims = _pixel_dims_for(entry, aspect)
        if dims is None:
            return None
        width, height = dims
        return usd_to_musd(
            (Decimal(width) * Decimal(height) / Decimal(1_000_000)) * pricing.usd
        )
    return None


def _estimate_clip_cost(entry: ModelEntry, *, clip_duration_ms: int) -> int | None:
    """Per-clip worst case in micro-USD (locked duration)."""
    from decimal import Decimal

    from studio.config.registry import PerSecond, PerVideo

    pricing = entry.pricing
    if isinstance(pricing, PerSecond):
        return usd_to_musd(Decimal(clip_duration_ms) / Decimal(1000) * pricing.usd)
    if isinstance(pricing, PerVideo):
        return usd_to_musd(Decimal(1) * pricing.usd)
    return None


def _sheet_spec_hash(
    bible: Any, character_name: str, role: str, style_tokens: list[str]
) -> str:
    character = _bible_character(bible, character_name)
    payload = {
        "name": character_name,
        "description": character.description if character is not None else "",
        "visual_tokens": list(character.visual_tokens) if character is not None else [],
        "style_tokens": list(style_tokens),
        "role": role,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _sheet_precheck_subjects(ctx: RunContext) -> list[tuple[str, str]]:
    """Missing (character_id, role) pairs only. Zero calls, zero gate when
    everything is already approved."""
    episode = _episode(ctx)
    if episode is None:
        return []
    bible = _pinned_bible(ctx, episode.id)
    if bible is None:
        return []
    roles = list(ctx.config.settings.character_sheets.roles)
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []
    for character in bible.characters:
        name = character.name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        for role in roles:
            approved = ctx.repos.sheets.approved(episode.series_id, name, role)
            if approved is None:
                missing.append((name, role))
    return missing


class CharacterSheetHandler:
    _STAGE = "s8a_character_sheets"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        episode = _episode(ctx)
        if episode is None:
            return []
        missing = _sheet_precheck_subjects(ctx)
        if not missing:
            approved = ctx.repos.sheets.approved_for_series(episode.series_id)
            if approved:
                ctx.repos.audit.append(
                    actor="system",
                    entity=f"series:{episode.series_id}",
                    event="character_sheets_all_approved",
                    payload={
                        "series_id": episode.series_id,
                        "approved": [f"{row.character_id}:{row.role}" for row in approved],
                    },
                )
            return []
        return [
            SubjectRef(
                scope=Scope.SERIES,
                id=f"{character_id}::{role}",
                lineage=(
                    (Scope.SERIES, episode.series_id),
                    (Scope.EPISODE, episode.id),
                ),
            )
            for character_id, role in missing
        ]

    def _parts(self, subject: SubjectRef) -> tuple[str, str]:
        character_id, sep, role = subject.id.partition("::")
        if not sep or not character_id or not role:
            raise StudioError(f"malformed sheet subject: {subject.id!r}")
        return character_id, role

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        character_id, role = self._parts(subject)
        bible = _pinned_bible(ctx, episode.id)
        settings = ctx.config.settings
        roles = list(settings.character_sheets.roles)
        is_primary = bool(roles) and role == roles[0]
        if is_primary:
            model_key, _entry = _model_entry(ctx, "sheets")
        else:
            model_key, _entry = _model_entry(ctx, "keyframes")
        home = Path(settings.paths.home)
        return _sha(
            {
                "stage": self._STAGE,
                "series_id": episode.series_id,
                "character": character_id,
                "character_spec": {
                    "description": (
                        _bible_character(bible, character_id).description
                        if _bible_character(bible, character_id) is not None
                        else ""
                    ),
                    "visual_tokens": (
                        list(_bible_character(bible, character_id).visual_tokens)
                        if _bible_character(bible, character_id) is not None
                        else []
                    ),
                },
                "role": role,
                "is_primary_role": is_primary,
                "style_tokens": _style_tokens(bible),
                "aspect": settings.character_sheets.aspect,
                "model_key": model_key,
                "prompt_version_sha": prompt_version_sha(home),
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        character_id, role = self._parts(subject)
        bible = _pinned_bible(ctx, episode.id)
        if bible is None:
            raise StudioError(
                "s8a_character_sheets requires the series bible; run the text stages first"
            )
        if _bible_character(bible, character_id) is None:
            raise PermanentProviderError(
                f"character {character_id!r} is not in the pinned series bible"
            )
        roles = list(settings.character_sheets.roles)
        is_primary = bool(roles) and role == roles[0]
        style_tokens = _style_tokens(bible)
        visual_spec = _character_visual_spec(bible, character_id)
        home = Path(settings.paths.home)

        # ---- ref conditioning for later roles (ref_full's sha)
        ref_assets: list[AssetRef] = []
        if not is_primary and roles:
            primary = ctx.repos.sheets.approved(episode.series_id, character_id, roles[0])
            if primary is None:
                raise PreflightError(
                    f"cannot generate {role!r} for {character_id!r}: no approved "
                    f"{roles[0]!r} sheet exists yet"
                )
            asset_row = ctx.repos.assets.get(primary.asset_id)
            if asset_row is None:
                raise PermanentProviderError(
                    f"approved {roles[0]} sheet {primary.id} lost its asset {primary.asset_id}"
                )
            ref_assets.append(
                AssetRef(
                    sha256=asset_row.sha256,
                    rel_path=asset_row.rel_path,
                    kind=asset_row.kind,
                    mime=asset_row.mime,
                    size_bytes=asset_row.size_bytes,
                    duration_ms=asset_row.duration_ms,
                    width=asset_row.width,
                    height=asset_row.height,
                )
            )

        if is_primary:
            model_key, entry = _model_entry(ctx, "sheets")
        else:
            model_key, entry = _model_entry(ctx, "keyframes")
        ref_clause_template = _ref_clause(entry)

        input_assets: list[AssetRef] = []
        prepared: list[Any] = []
        if ref_assets:
            input_assets, prepared = await _prepare_input_assets(ctx, ref_assets)

        # ---- budget: one sheet, worst case with the assumed retry rate
        worst = _estimate_image_cost(entry, aspect=Aspect(settings.character_sheets.aspect))
        if worst is not None:
            uplift = float(settings.media.assumed_retry_rate.get("image", 0.25))
            worst = int(worst * (1 + uplift)) + 1
            _worst_case_pause(
                ctx,
                episode,
                worst_case_musd=worst,
                label=f"character sheet {character_id}/{role}",
            )

        def make_prompt(scene_override: str | None) -> PromptSpec:
            spec = compose_sheet_prompt(
                character_name=character_id,
                visual_spec=visual_spec,
                role=role,
                style_tokens=style_tokens,
                ref_clause_template=ref_clause_template if ref_assets else None,
                ref_clauses=[(character_id, 1)] if ref_assets else None,
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
                aspect=Aspect(settings.character_sheets.aspect),
                seed=seed,
                nonce=nonce,
                purpose="character_sheet",
            )

        async def qc_run(path: Path) -> QcOutcome:
            checks = await qc_image(
                path, aspect=Aspect(settings.character_sheets.aspect), settings=settings
            )
            return checks, None, {}

        def persist_audit(payload: dict[str, Any]) -> None:
            ctx.repos.audit.append(
                actor="system",
                entity=f"sheet:{character_id}:{role}",
                event="character_sheet_attempt",
                payload={
                    "episode_id": episode.id,
                    "character_id": character_id,
                    "role": role,
                    "stage_run_id": ctx.stage_run_id,
                    **payload,
                },
            )

        subject_key = f"sheet:{character_id}:{role}"
        base_nonce = ctx.ids.new()
        try:
            outcome = await _generate_with_retries(
                ctx,
                episode_id=episode.id,
                subject_key=subject_key,
                purpose="character_sheet",
                kind="image",
                model_key=model_key,
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce=base_nonce,
                max_paid_attempts=int(settings.media.max_paid_attempts_per_shot),
                auto_retries=int(settings.media.qc.image.auto_retries),
                qc_run=qc_run,
                persist=persist_audit,
            )
        except (FailedPolicy, VisualQcError, MediaCircuitOpen, MediaJobFailedError) as exc:
            _media_failure_audit(
                ctx, stage=self._STAGE, episode_id=episode.id,
                subject_id=subject.id, error=exc,
            )
            raise

        version = ctx.repos.sheets.latest_version(character_id, role) + 1
        spec_hash = _sheet_spec_hash(bible, character_id, role, style_tokens)
        sheet = ctx.repos.sheets.create(
            series_id=episode.series_id,
            character_id=character_id,
            role=role,
            version=version,
            asset_id=outcome.asset.sha256,
            spec_hash=spec_hash,
            nonce=outcome.nonce,
            status="candidate",
        )
        ctx.repos.audit.append(
            actor="system",
            entity=f"sheet:{sheet.id}",
            event="character_sheet_generated",
            payload={
                "episode_id": episode.id,
                "series_id": episode.series_id,
                "character_id": character_id,
                "role": role,
                "sheet_id": sheet.id,
                "version": version,
                "asset_sha256": outcome.asset.sha256,
                "spec_hash": spec_hash,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc": [c.as_dict() for c in outcome.checks],
            },
        )
        return StageOutput(
            output={
                "series_id": episode.series_id,
                "character_id": character_id,
                "role": role,
                "sheet_id": sheet.id,
                "version": version,
                "asset_sha256": outcome.asset.sha256,
                "spec_hash": spec_hash,
                "sanitize_level": outcome.sanitize_level,
                "cost_musd": outcome.cost_musd,
                "qc_status": _qc_status(outcome.checks),
                "model_key": model_key,
            }
        )
