"""Worst-case cost estimation and actual-cost reconciliation.

All arithmetic is Decimal-based; the only conversion to int happens at the
micro-USD boundary via `usd_to_musd` (ROUND_HALF_UP). Nothing here touches
float money.
"""

from __future__ import annotations

import math
from decimal import Decimal

from studio.config.registry import ModelEntry, Per1kChars, Per1mTokens, PerImage, PerSecond
from studio.domain.errors import StudioError
from studio.domain.money import usd_to_musd
from studio.providers.base import LLMRequest, LLMResult, MediaRequest, TTSRequest, TTSResult

_MILLION = Decimal(1_000_000)
_THOUSAND = Decimal(1_000)
_ONE_SECOND = Decimal(1)


def estimate_cost_musd(
    entry: ModelEntry,
    req: LLMRequest | TTSRequest | MediaRequest,
    *,
    tokens_per_char_worst_case: float = 0.5,
) -> int:
    pricing = entry.pricing
    if isinstance(pricing, Per1mTokens):
        if not isinstance(req, LLMRequest):
            raise StudioError(
                f"per_1m_tokens pricing requires an LLM request, got {type(req).__name__}"
            )
        chars = len(req.system) + len(req.prompt)
        input_tokens = math.ceil(chars * tokens_per_char_worst_case)
        usd = (
            Decimal(input_tokens) * pricing.in_usd + Decimal(req.max_tokens) * pricing.out_usd
        ) / _MILLION
        return usd_to_musd(usd)
    if isinstance(pricing, Per1kChars):
        if not isinstance(req, TTSRequest):
            raise StudioError(
                f"per_1k_chars pricing requires a TTS request, got {type(req).__name__}"
            )
        usd = (Decimal(len(req.text)) * pricing.usd) / _THOUSAND
        return usd_to_musd(usd)
    if isinstance(pricing, PerImage):
        if not isinstance(req, MediaRequest):
            raise StudioError(
                f"per_image pricing requires a media request, got {type(req).__name__}"
            )
        n_images = _n_images(req)
        usd = Decimal(n_images) * pricing.usd
        return usd_to_musd(usd)
    if isinstance(pricing, PerSecond):
        if not isinstance(req, MediaRequest):
            raise StudioError(
                f"per_second pricing requires a media request, got {type(req).__name__}"
            )
        if req.duration_s is None:
            raise StudioError("per_second pricing requires duration_s on the media request")
        usd = Decimal(str(req.duration_s)) * pricing.usd
        return usd_to_musd(usd)
    raise StudioError(f"unsupported pricing unit: {pricing.unit!r}")


def actual_cost_musd(
    entry: ModelEntry,
    req: LLMRequest | TTSRequest | MediaRequest,
    result: object,
    *,
    tokens_per_char_worst_case: float = 0.5,
) -> int:
    """Provider-reported usage when present, else the worst-case estimate."""
    pricing = entry.pricing
    if isinstance(pricing, Per1mTokens):
        if isinstance(result, LLMResult):
            usd = (
                Decimal(result.tokens_in) * pricing.in_usd
                + Decimal(result.tokens_out) * pricing.out_usd
            ) / _MILLION
            return usd_to_musd(usd)
    elif isinstance(pricing, Per1kChars):
        if isinstance(result, TTSResult):
            usd = (Decimal(result.billed_chars) * pricing.usd) / _THOUSAND
            return usd_to_musd(usd)
    # image/video providers report no usage in Phase 1 → estimate stands.
    return estimate_cost_musd(entry, req, tokens_per_char_worst_case=tokens_per_char_worst_case)


def _n_images(req: MediaRequest) -> int:
    raw = req.params.get("n_images", 1)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise StudioError(f"n_images must be an integer, got {raw!r}") from None
    if n < 1:
        raise StudioError(f"n_images must be >= 1, got {n}")
    return n
