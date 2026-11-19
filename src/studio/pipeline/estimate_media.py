"""Gate-1 media estimate (H2 / R3).

Pure, offline and fail-closed: when any required model is unconfirmed, its
price unverified, or a price-affecting parameter unavailable, the estimate is
marked ``available: false`` with a reason and **no zero-cost numbers** are
emitted. S4 adds the result as additive output keys; S6 reads exactly
``gate1_video_estimate_musd`` / ``projected_video_cost_musd`` for drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from studio.audio.timeline import choose_supported_duration_ms
from studio.config.registry import ModelEntry
from studio.core.canonical import canonical_json
from studio.core.pricing import estimate_cost_musd
from studio.domain.enums import Aspect, Language, Profile
from studio.domain.errors import StudioError
from studio.language import count_words, normalize_for_tts
from studio.media.registry import (
    media_model_entry,
    media_spec,
    model_confirmation_problems,
    supported_durations_s,
)
from studio.providers.base import MediaRequest, TTSRequest

if TYPE_CHECKING:
    from studio.pipeline.context import RunContext

_ESTIMATE_VERSION = 1


@dataclass(frozen=True)
class MediaEstimate:
    available: bool
    reason: str
    tts: dict[str, Any]
    sheets: dict[str, Any]
    keyframes: dict[str, Any]
    video: dict[str, Any]
    total: dict[str, Any]
    gate1_video_estimate_musd: int | None = None
    projected_video_cost_musd: int | None = None

    def additive_output(self) -> dict[str, Any]:
        out: dict[str, Any] = {"media_estimate": self.as_dict()}
        if self.available and self.gate1_video_estimate_musd is not None:
            # Both names are preserved: S6 reads either one.
            out["gate1_video_estimate_musd"] = self.gate1_video_estimate_musd
            out["projected_video_cost_musd"] = self.projected_video_cost_musd
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": _ESTIMATE_VERSION,
            "available": self.available,
            "reason": self.reason,
            "tts": self.tts,
            "sheets": self.sheets,
            "keyframes": self.keyframes,
            "video": self.video,
            "total": self.total,
        }


def _band(low: int, expected: int, high: int) -> dict[str, Any]:
    return {
        "available": True,
        "reason": "",
        "low_musd": low,
        "expected_musd": expected,
        "high_musd": high,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    # Deliberately no numeric keys: never emit $0 as if it were an estimate.
    return {"available": False, "reason": reason}


def _unavailable_estimate(reason: str) -> MediaEstimate:
    part = _unavailable(reason)
    return MediaEstimate(
        available=False,
        reason=reason,
        tts=dict(part),
        sheets=dict(part),
        keyframes=dict(part),
        video=dict(part),
        total=dict(part),
    )


def _tts_entry(ctx: RunContext) -> tuple[str, ModelEntry]:
    try:
        key = ctx.resolved_profile.model_key_for("tts")
    except StudioError:
        key = "mock.tts" if ctx.resolved_profile.name == Profile.SANDBOX.value else "tts.main"
    entry = ctx.config.registry.models.get(key)
    if entry is None:
        raise StudioError(f"TTS model {key!r} missing from model_registry.yaml")
    return key, entry


def _media_price_musd(
    entry: ModelEntry,
    *,
    aspect: Aspect,
    duration_s: float | None = None,
    n_images: int = 1,
    width: int | None = None,
    height: int | None = None,
) -> int:
    spec = media_spec(entry)
    params: dict[str, Any] = {"n_images": int(n_images)}
    params.update(spec.fixed_params)
    params.update(spec.aspect_extra_params.get(aspect.value, {}))
    if width is not None:
        params["width"] = int(width)
    if height is not None:
        params["height"] = int(height)
    req = MediaRequest(
        capability=entry.capability,
        params=params,
        aspect=aspect,
        duration_s=duration_s,
        seed=0,
    )
    return estimate_cost_musd(entry, req)


def _pixel_dims(entry: ModelEntry, aspect: Aspect) -> tuple[int, int] | None:
    """Dimensions only from registry-confirmed fixed/aspect params."""
    spec = media_spec(entry)
    raw: dict[str, Any] = {}
    raw.update(spec.fixed_params)
    raw.update(spec.aspect_extra_params.get(aspect.value, {}))
    w, h = raw.get("width"), raw.get("height")
    if w is None or h is None or "<CONFIRM" in str(w) or "<CONFIRM" in str(h):
        return None
    try:
        return int(w), int(h)
    except (TypeError, ValueError):
        return None


def media_estimate_hash_parts(ctx: RunContext) -> dict[str, Any]:
    """Registry prices + settings that change the Gate-1 estimate (H2)."""
    settings = ctx.config.settings
    entries: dict[str, Any] = {}
    for role in ("sheets", "keyframes", "clips"):
        try:
            key, entry = media_model_entry(ctx.config, ctx.resolved_profile, role)
            entries[role] = {
                "key": key,
                "pricing": entry.pricing.model_dump(mode="json"),
                "needs_confirmation": entry.needs_confirmation,
                "pricing_verified_on": (
                    entry.pricing_verified_on.isoformat() if entry.pricing_verified_on else None
                ),
                "supported_durations_s": list(entry.supported_durations_s),
                "supported_aspects": list(entry.supported_aspects),
                "fixed_params": media_spec(entry).fixed_params,
            }
        except StudioError as exc:
            entries[role] = {"error": str(exc)}
    try:
        tts_key, tts_entry = _tts_entry(ctx)
        entries["tts"] = {
            "key": tts_key,
            "pricing": tts_entry.pricing.model_dump(mode="json"),
            "needs_confirmation": tts_entry.needs_confirmation,
            "pricing_verified_on": (
                tts_entry.pricing_verified_on.isoformat() if tts_entry.pricing_verified_on else None
            ),
        }
    except StudioError as exc:
        entries["tts"] = {"error": str(exc)}
    return {
        "version": _ESTIMATE_VERSION,
        "models": entries,
        "settings": {
            "keep_diacritics": settings.tts.keep_diacritics,
            "words_per_second": settings.timing.words_per_second,
            "lead_in_ms": settings.timing.lead_in_ms,
            "tail_ms": settings.timing.tail_ms,
            "line_gap_ms": settings.timing.line_gap_ms,
            "max_stretch": settings.timing.max_stretch,
            "max_hold_ms": settings.timing.max_hold_ms,
            "prefer_stretch_to_save_cost": settings.timing.prefer_stretch_to_save_cost,
            "fps": settings.render.fps,
            "assumed_retry_rate": settings.media.assumed_retry_rate,
            "character_sheet_roles": settings.character_sheets.roles,
        },
    }


def media_estimate(ctx: RunContext, deliverable_id: str) -> MediaEstimate:
    """Compute the additive Gate-1 media estimate for one deliverable."""
    deliverable = ctx.repos.content.get_deliverable(deliverable_id)
    if deliverable is None:
        return _unavailable_estimate(f"deliverable {deliverable_id!r} not found")
    track = ctx.repos.content.get_track(deliverable.track_id)
    if track is None:
        return _unavailable_estimate(f"track {deliverable.track_id!r} not found")
    episode = ctx.repos.content.get_episode(track.episode_id)
    if episode is None:
        return _unavailable_estimate(f"episode {track.episode_id!r} not found")
    settings = ctx.config.settings
    final = ctx.resolved_profile.name == Profile.FINAL.value

    try:
        tts_key, tts_entry = _tts_entry(ctx)
        sheets_key, sheets_entry = media_model_entry(
            ctx.config, ctx.resolved_profile, "sheets"
        )
        keyframes_key, keyframes_entry = media_model_entry(
            ctx.config, ctx.resolved_profile, "keyframes"
        )
        clips_key, clips_entry = media_model_entry(ctx.config, ctx.resolved_profile, "clips")
        durations = supported_durations_s(clips_entry)
        aspect = Aspect(track.aspect)
        pixel_dims = _pixel_dims(sheets_entry, aspect)
    except StudioError as exc:
        return _unavailable_estimate(str(exc))

    reasons: list[str] = []
    for label, key, entry in (
        ("tts", tts_key, tts_entry),
        ("sheets", sheets_key, sheets_entry),
        ("keyframes", keyframes_key, keyframes_entry),
        ("clips", clips_key, clips_entry),
    ):
        for problem in model_confirmation_problems(entry, final=final):
            reasons.append(f"{label} model {key!r}: {problem}")
    if pixel_dims is None and sheets_entry.pricing.unit == "per_megapixel":
        reasons.append(
            f"sheets model {sheets_key!r}: per_megapixel price needs confirmed width/height"
        )
    if reasons:
        return _unavailable_estimate("; ".join(sorted(set(reasons))))

    # ---- data ------------------------------------------------------------
    language = Language(deliverable.language)
    lines = ctx.repos.content.narration_lines(deliverable.id)
    shots = sorted(ctx.repos.content.shots_for_track(track.id), key=lambda s: s.shot_no)
    bible = ctx.repos.bible.latest(episode.series_id)
    characters = [c.name for c in bible.characters] if bible is not None else []
    approved_sheets = {
        str(row["character_id"])
        for row in ctx.db.connect()
        .execute(
            "SELECT character_id FROM character_sheets WHERE status = 'approved' AND series_id = ?",
            (episode.series_id,),
        )
        .fetchall()
    }
    missing_sheets = len([name for name in characters if name not in approved_sheets])
    roles = list(settings.character_sheets.roles)
    retry_image = float(settings.media.assumed_retry_rate.get("image", 0.25))
    retry_video = float(settings.media.assumed_retry_rate.get("video", 0.30))

    try:
        # ---- TTS is exact over len(text_to_send) --------------------------
        tts_cost = 0
        for line in lines:
            text = normalize_for_tts(language, line.approved_text, settings)
            tts_cost += estimate_cost_musd(
                tts_entry, TTSRequest(text=text, voice_id="", language=language)
            )

        # ---- sheets: missing characters x roles x price -------------------
        sheet_unit = _media_price_musd(
            sheets_entry, aspect=aspect, width=pixel_dims[0] if pixel_dims else None,
            height=pixel_dims[1] if pixel_dims else None,
        )
        sheets_low = missing_sheets * len(roles) * sheet_unit
        sheets_expected = math.ceil(sheets_low * (1.0 + retry_image))
        sheets_high = math.ceil(sheets_low * (1.0 + 2.0 * retry_image))

        # ---- keyframes: shots x retry rate x price ------------------------
        keyframe_unit = _media_price_musd(
            keyframes_entry, aspect=aspect, width=pixel_dims[0] if pixel_dims else None,
            height=pixel_dims[1] if pixel_dims else None,
        )
        keyframes_low = len(shots) * keyframe_unit
        keyframes_expected = math.ceil(keyframes_low * (1.0 + retry_image))
        keyframes_high = math.ceil(keyframes_low * (1.0 + 2.0 * retry_image))

        # ---- video: words / wps -> the S6 pure duration chooser -----------
        wps = float(settings.timing.words_per_second[language.value])
        fps = int(settings.render.fps)
        plain_timing = settings.timing.model_copy(
            update={
                "prefer_stretch_to_save_cost": False,
                "max_stretch": 1.0,
                "max_hold_ms": 0,
            }
        )
        low_seconds = 0.0
        expected_seconds = 0.0
        high_seconds = 0.0
        overflow = False
        for shot in shots:
            shot_lines = [line for line in lines if line.shot_id == shot.id]
            words = sum(
                count_words(language, normalize_for_tts(language, line.approved_text, settings))
                for line in shot_lines
            )
            needed_ms = (
                words / wps * 1000.0
                + float(settings.timing.lead_in_ms)
                + float(settings.timing.tail_ms)
                + max(0, len(shot_lines) - 1) * float(settings.timing.line_gap_ms)
            )
            needed_frames = math.ceil(needed_ms * fps / 1000.0)
            low_ms = choose_supported_duration_ms(
                needed_frames=needed_frames,
                supported_durations_s=durations,
                fps=fps,
                timing=plain_timing,
            )
            expected_ms = choose_supported_duration_ms(
                needed_frames=needed_frames,
                supported_durations_s=durations,
                fps=fps,
                timing=settings.timing,
            )
            if low_ms is None or expected_ms is None:
                overflow = True
                low_ms = expected_ms = max(durations) * 1000
            next_ms = next(
                (d * 1000 for d in durations if d * 1000 > int(expected_ms)), None
            )
            high_ms = (
                next_ms
                if next_ms is not None
                else math.ceil(expected_ms * (1.0 + retry_video))
            )
            low_seconds += low_ms / 1000.0
            expected_seconds += expected_ms / 1000.0
            high_seconds += high_ms / 1000.0

        video_unit = _media_price_musd(
            clips_entry, aspect=aspect, duration_s=1.0
        )
        video_low = math.ceil(Decimal(str(low_seconds)) * Decimal(video_unit))
        video_expected = math.ceil(Decimal(str(expected_seconds)) * Decimal(video_unit))
        video_high = math.ceil(
            max(
                Decimal(str(high_seconds)) * Decimal(video_unit),
                Decimal(video_expected) * Decimal(str(1.0 + retry_video)),
            )
        )
    except (StudioError, ValueError, KeyError, ArithmeticError) as exc:
        return _unavailable_estimate(f"media estimate inputs invalid: {exc}")

    if min(tts_cost, sheet_unit, keyframe_unit, video_unit) <= 0:
        return _unavailable_estimate(
            "one or more confirmed media prices is zero; refusing to emit a $0 estimate"
        )

    reason = ""
    if overflow:
        reason = "narration exceeds the longest supported clip; S6 will report overflow"
    estimate = MediaEstimate(
        available=True,
        reason=reason,
        tts=_band(tts_cost, tts_cost, tts_cost),
        sheets=_band(sheets_low, sheets_expected, sheets_high),
        keyframes=_band(keyframes_low, keyframes_expected, keyframes_high),
        video=_band(video_low, video_expected, video_high),
        total=_band(
            tts_cost + sheets_low + keyframes_low + video_low,
            tts_cost + sheets_expected + keyframes_expected + video_expected,
            tts_cost + sheets_high + keyframes_high + video_high,
        ),
        gate1_video_estimate_musd=video_expected,
        projected_video_cost_musd=video_expected,
    )
    return estimate


def media_estimate_sha(ctx: RunContext) -> str:
    """Stable hash of price/settings inputs for S4's input_hash."""
    import hashlib

    return hashlib.sha256(canonical_json(media_estimate_hash_parts(ctx))).hexdigest()
