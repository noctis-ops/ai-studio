"""Shared media helpers: episode/shot/bible lookups, request builders, reference assets.

Split from ``studio.media.stages`` in Phase 5 (H5.8) as a pure move.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from studio.config.registry import ModelEntry
from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.domain.enums import Aspect, Scope
from studio.domain.errors import (
    MediaConfigError,
)
from studio.domain.refs import AssetRef, SubjectRef
from studio.media.prompts import (
    PromptSpec,
)
from studio.media.refs import prepare_refs
from studio.media.registry import (
    max_refs,
    media_model_entry,
    media_spec,
)
from studio.providers.base import MediaRequest

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from studio.domain.models import EpisodeRow, ShotRow
    from studio.pipeline.context import RunContext

_S4_STAGE = "metadata_safety"


_SHOT_PLAN_STAGE = "shot_plan"


_KEYFRAME_MODEL_ROLE = "keyframes"


_SHEET_MODEL_ROLE = "sheets"


_CLIP_MODEL_ROLE = "clips"


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _episode(ctx: RunContext) -> EpisodeRow | None:
    if not ctx.episode_id:
        return None
    return ctx.repos.content.get_episode(ctx.episode_id)


def _episode_subject(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    return [
        SubjectRef(
            scope=Scope.EPISODE,
            id=episode.id,
            lineage=((Scope.SERIES, episode.series_id),),
        )
    ]


def _track_shots(ctx: RunContext) -> list[ShotRow]:
    episode = _episode(ctx)
    if episode is None:
        return []
    return sorted(
        ctx.repos.content.list_shots(episode.id),
        key=lambda shot: (shot.track_id, shot.shot_no),
    )


def _shot_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    subjects: list[SubjectRef] = []
    for shot in _track_shots(ctx):
        subjects.append(
            SubjectRef(
                scope=Scope.SHOT,
                id=shot.id,
                lineage=(
                    (Scope.SERIES, episode.series_id),
                    (Scope.EPISODE, episode.id),
                    (Scope.TRACK, shot.track_id),
                ),
            )
        )
    return subjects


def _stage_output(
    ctx: RunContext, stage: str, subject_id: str
) -> dict[str, Any] | None:
    for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage=stage):
        if row.subject_id == subject_id and row.status == "succeeded" and row.output_json:
            try:
                data: dict[str, Any] = json.loads(row.output_json)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _pinned_bible(ctx: RunContext, episode_id: str):  # type: ignore[no-untyped-def]
    from studio.domain.series_bible import SeriesBible

    episode = ctx.repos.content.get_episode(episode_id)
    if episode is None:
        return None
    s1_output = _stage_output(ctx, "bible_upsert", episode_id)
    version: int | None = None
    if isinstance(s1_output, dict):
        raw = s1_output.get("version")
        if isinstance(raw, int):
            version = raw
    bible: SeriesBible | None = None
    if version is not None:
        bible = ctx.repos.bible.get(episode.series_id, version)
    if bible is None:
        bible = ctx.repos.bible.latest(episode.series_id)
    return bible


def _style_tokens(bible: Any) -> list[str]:
    return list(bible.style_tokens) if bible is not None else []


def _shot_plan(ctx: RunContext, track_id: str) -> dict[int, dict[str, Any]]:
    output = _stage_output(ctx, _SHOT_PLAN_STAGE, track_id)
    shots = output.get("shots", []) if isinstance(output, dict) else []
    plan: dict[int, dict[str, Any]] = {}
    for item in shots if isinstance(shots, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            no = int(item.get("no") or 0)
        except (TypeError, ValueError):
            continue
        if no >= 1:
            plan[no] = item
    return plan


def _shot_characters(ctx: RunContext, track_id: str) -> dict[int, list[str]]:
    plan = _shot_plan(ctx, track_id)
    characters: dict[int, list[str]] = {}
    for no, item in plan.items():
        raw = item.get("characters")
        names = [str(name) for name in raw if str(name).strip()] if isinstance(raw, list) else []
        characters[no] = names
    return characters


def _shot_camera(ctx: RunContext, track_id: str) -> dict[int, str]:
    plan = _shot_plan(ctx, track_id)
    camera: dict[int, str] = {}
    for no, item in plan.items():
        raw = item.get("camera")
        camera[no] = str(raw).strip() if isinstance(raw, str) else ""
    return camera


def _bible_character(bible: Any, name: str):  # type: ignore[no-untyped-def]
    if bible is None:
        return None
    for character in bible.characters:
        if character.name == name:
            return character
    return None


def _character_visual_spec(bible: Any, name: str) -> str:
    character = _bible_character(bible, name)
    if character is None:
        return ""
    tokens = ", ".join(token.strip() for token in character.visual_tokens if token.strip())
    description = character.description.strip()
    if tokens and description:
        return f"{tokens}. {description}"
    return tokens or description


def _model_entry(
    ctx: RunContext, role: Literal["sheets", "keyframes", "clips"]
) -> tuple[str, ModelEntry]:
    key, entry = media_model_entry(ctx.config, ctx.resolved_profile, role)
    return key, entry


def _ref_clause(entry: ModelEntry) -> str | None:
    ref_clause = media_spec(entry).prompt_templates.get("ref_clause")
    return ref_clause if isinstance(ref_clause, str) and ref_clause.strip() else None


def _shot_plan_camera(ctx: RunContext, track_id: str, shot_no: int) -> str:
    return _shot_camera(ctx, track_id).get(shot_no, "")


def _approved_sheet_assets(
    ctx: RunContext, series_id: str, character_id: str, roles: list[str]
) -> dict[str, AssetRef]:
    assets: dict[str, AssetRef] = {}
    for role in roles:
        row = ctx.repos.sheets.approved(series_id, character_id, role)
        if row is None:
            continue
        asset_row = ctx.repos.assets.get(row.asset_id)
        if asset_row is None:
            continue
        assets[role] = AssetRef(
            sha256=asset_row.sha256,
            rel_path=asset_row.rel_path,
            kind=asset_row.kind,
            mime=asset_row.mime,
            size_bytes=asset_row.size_bytes,
            duration_ms=asset_row.duration_ms,
            width=asset_row.width,
            height=asset_row.height,
        )
    return assets


def _ref_assets_for_shot(
    ctx: RunContext,
    *,
    series_id: str,
    characters: list[str],
    entry: ModelEntry,
) -> tuple[list[tuple[str, str, AssetRef]], bool]:
    """Approved sheet refs for the shot's characters, up to max_refs.

    Returns ((character_id, role, asset) tuples in prominence order,
    refs_truncated). Prominence order = order of first appearance in the
    shot plan; ref_full first, then extra configured roles per character.
    """
    roles = [role for role in ctx.config.settings.character_sheets.roles]
    ordered: list[tuple[str, str, AssetRef]] = []
    for character_id in characters:
        assets = _approved_sheet_assets(ctx, series_id, character_id, roles)
        for role in roles:
            asset = assets.get(role)
            if asset is not None:
                ordered.append((character_id, role, asset))
    limit = max_refs(entry)
    if limit <= 0:
        return [], bool(ordered)
    overflow = ctx.config.settings.media.refs_overflow
    if len(ordered) <= limit:
        return ordered, False
    if overflow == "fail":
        raise MediaConfigError(
            f"shot references {len(ordered)} sheet assets but the model declares "
            f"max_refs={limit} and media.refs_overflow=fail"
        )
    logger.warning(
        "shot references %d sheet assets; keeping the first %d (refs_overflow=primary_only)",
        len(ordered),
        limit,
    )
    return ordered[:limit], True


async def _prepare_input_assets(
    ctx: RunContext, refs: list[AssetRef]
) -> tuple[list[AssetRef], list[Any]]:
    settings = ctx.config.settings
    if not refs:
        return [], []
    cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
    prepared = await prepare_refs(cas, refs, settings)
    input_assets = [item.ref for item in prepared]
    return input_assets, list(prepared)


def _image_request(
    ctx: RunContext,
    *,
    entry: ModelEntry,
    spec: PromptSpec,
    negative: str | None,
    input_assets: list[AssetRef],
    prepared: list[Any],
    aspect: Aspect,
    seed: int,
    nonce: str,
    purpose: str,
) -> MediaRequest:
    params: dict[str, Any] = {"prompt": spec.full_prompt}
    if negative is not None and "negative" in media_spec(entry).param_map:
        params["negative"] = negative
    if input_assets:
        use_data_uri = media_spec(entry).input_transport == "data_uri"

        def wire_ref(prepared_item: Any) -> str:
            return prepared_item.data_uri if use_data_uri else str(prepared_item.local_path)

        if "refs" in media_spec(entry).param_map:
            params["refs"] = [wire_ref(p) for p in prepared]
        elif "image" in media_spec(entry).param_map:
            params["image"] = wire_ref(prepared[0])
    if input_assets:
        total = sum(len(p.data_uri.encode("utf-8")) for p in prepared)
        if media_spec(entry).input_transport == "data_uri" and (
            total > int(ctx.config.settings.media.max_inline_bytes)
        ):
            raise MediaConfigError(
                f"prepared refs exceed media.max_inline_bytes "
                f"({total} > {ctx.config.settings.media.max_inline_bytes}); "
                f"use the upload transport or fewer refs"
            )
    return MediaRequest(
        capability="image_reference_generate",
        params=params,
        input_assets=input_assets,
        aspect=aspect,
        duration_s=None,
        seed=seed,
        prompt_version_sha=spec.prompt_version_sha,
    )


def _video_request(
    ctx: RunContext,
    *,
    entry: ModelEntry,
    prompt: str,
    keyframe: AssetRef,
    keyframe_prepared: Any,
    aspect: Aspect,
    duration_s: float,
    seed: int,
    prompt_sha: str,
) -> MediaRequest:
    params: dict[str, Any] = {"prompt": prompt}
    transport = media_spec(entry).input_transport
    if "image" in media_spec(entry).param_map:
        if transport == "data_uri":
            params["image"] = keyframe_prepared.data_uri
            total = len(keyframe_prepared.data_uri.encode("utf-8"))
            if total > int(ctx.config.settings.media.max_inline_bytes):
                raise MediaConfigError(
                    f"keyframe data URI exceeds media.max_inline_bytes ({total} bytes)"
                )
        else:
            params["image"] = str(keyframe_prepared.local_path)
    if "negative" in media_spec(entry).param_map:
        from studio.media.prompts import load_templates as _load_templates

        params["negative"] = _load_templates(
            Path(ctx.config.settings.paths.home)
        )["negative"].strip()
    return MediaRequest(
        capability="image_to_video",
        params=params,
        input_assets=[keyframe],
        aspect=aspect,
        duration_s=duration_s,
        seed=seed,
        prompt_version_sha=prompt_sha,
    )
