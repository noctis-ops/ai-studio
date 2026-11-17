"""Character voices: settings model, resolution, and TTS cost projection.

`character_voices.settings_json` is the persisted, validated payload for one
character's voice in one language. It is parsed through :class:`VoiceSettings`
on every read — never trusted raw — so a hand-edited row fails closed with a
typed error instead of a silent mis-synthesis.

``license_attested_at`` (the operator's commercial-use attestation, stamped by
``studio voice set --attest-commercial``) is what the ``final`` profile checks
in S5a preflight.

``worst_case_tts_musd`` is the single source of truth for the TTS worst-case
reservation: Σ ``len(text_to_send)`` x price, mirroring
``core.pricing.estimate_cost_musd`` for Per1kChars pricing without requiring a
fabricated request. Both S5a (budget check, pre-spend) and any S4/Gate-1 cost
projection must route through it so the numbers can never drift apart
(T-PRICE-02). Note Phase 2 stored no TTS projection at all — see the report.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.config.registry import ModelEntry, Per1kChars
from studio.domain.enums import Language
from studio.domain.errors import StudioError
from studio.domain.money import usd_to_musd

if TYPE_CHECKING:
    from collections.abc import Sequence



class VoiceSettings(BaseModel):
    """Validated payload of `character_voices.settings_json` (Phase 3, §E)."""

    model_config = ConfigDict(extra="forbid")

    model_key: str = "tts.main"
    stability: float | None = Field(default=None, ge=0.0, le=1.0)
    similarity_boost: float | None = Field(default=None, ge=0.0, le=1.0)
    style: float | None = Field(default=None, ge=0.0, le=1.0)
    speaker_boost: bool | None = None
    speed: float | None = Field(default=None, gt=0.0)
    mix_gain_db: float = 0.0
    license_attested_at: str | None = None  # TS_FORMAT Zulu string or null


def parse_voice_settings(raw: str) -> VoiceSettings:
    """Parse `settings_json`, raising a typed error on any violation."""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StudioError(f"voice settings_json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StudioError("voice settings_json must be a JSON object")
    try:
        return VoiceSettings.model_validate(data)
    except ValidationError as exc:
        raise StudioError(f"voice settings_json failed validation: {exc}") from exc


def voice_settings_json(settings: VoiceSettings) -> str:
    return json.dumps(
        settings.model_dump(exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def request_settings(settings: VoiceSettings) -> dict[str, Any]:
    """Subset forwarded to the provider on every synthesis call.

    Only provider-relevant knobs are sent (the adapter additionally maps
    `speaker_boost` → `use_speaker_boost` on the wire). `model_key` selects
    the model and `license_attested_at` is bookkeeping — neither is a wire
    field.
    """
    out: dict[str, Any] = {}
    for key in ("stability", "similarity_boost", "style", "speaker_boost", "speed"):
        value = getattr(settings, key)
        if value is not None:
            out[key] = value
    return out


def settings_fingerprint(settings: VoiceSettings) -> str:
    """Stable hash of the provider-relevant settings (for cache/snapshots)."""
    return hashlib.sha256(voice_settings_json(settings).encode("utf-8")).hexdigest()


def voice_snapshot(
    *,
    language: Language,
    voice_id: str,
    settings: VoiceSettings,
    model_key: str,
    model_id: str | None,
) -> str:
    """JSON stored on `narration_lines.voice_snapshot_json` after voicing."""
    return json.dumps(
        {
            "language": language.value,
            "voice_id": voice_id,
            "model_key": model_key,
            "model_id": model_id,
            "settings": json.loads(voice_settings_json(settings)),
            "settings_fingerprint": settings_fingerprint(settings),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def worst_case_tts_musd(entry: ModelEntry, texts: Sequence[str]) -> int:
    """Σ per-line worst-case reservation, in micro-USD (integer).

    Mirrors `core.pricing.estimate_cost_musd` for Per1kChars pricing so the
    preflight total, the S5 reservation sum, and any Gate-1 projection are
    guaranteed to agree on the character count (including diacritics — the
    caller passes the exact `text_to_send` strings, post keep_diacritics).
    """
    pricing = entry.pricing
    if not isinstance(pricing, Per1kChars):
        raise StudioError(
            f"TTS model {entry.model_ref!r} is not per_1k_chars priced; "
            "the worst-case TTS reservation is undefined for it"
        )
    total = 0
    for text in texts:
        total += usd_to_musd(Decimal(len(text)) * pricing.usd / Decimal(1000))
    return total
