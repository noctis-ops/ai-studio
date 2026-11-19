"""Registry-driven media model resolution (§E1 / §F).

Shipped profile role maps are intentionally unchanged (Discovery Report
§Contradictions 2). Media code resolves the three Phase 4 roles from an
explicit, profile-keyed table instead, so `registry_snapshot()` and every
Phase 1-3 assertion keep their exact shape. Operator-confirmed endpoints can
also be wired through the optional `image_t2i` / `image_ref` / `video_i2v`
roles; those win when present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from studio.config.registry import MediaSpec, ModelEntry
from studio.domain.enums import Aspect
from studio.domain.errors import MediaConfigError

if TYPE_CHECKING:
    from studio.config.loader import AppConfig
    from studio.config.registry import ResolvedProfile

MediaRole = Literal["sheets", "keyframes", "clips"]

_OPTIONAL_ROLES: dict[MediaRole, tuple[str, ...]] = {
    "sheets": ("image_t2i",),
    "keyframes": ("image_ref",),
    "clips": ("video_i2v",),
}

_DEFAULT_KEYS: dict[str, dict[MediaRole, str]] = {
    "sandbox": {
        "sheets": "mock.image",
        "keyframes": "mock.image",
        "clips": "mock.video",
    },
    "draft": {
        "sheets": "image.t2i.draft",
        "keyframes": "image.ref.draft",
        "clips": "video.i2v.draft",
    },
    "final": {
        "sheets": "image.t2i.draft",
        "keyframes": "image.ref.final",
        "clips": "video.i2v.final",
    },
}

_EXPECTED_CAPABILITY: dict[MediaRole, str] = {
    "sheets": "image_reference_generate",
    "keyframes": "image_reference_generate",
    "clips": "image_to_video",
}


def media_model_key(config: AppConfig, resolved: ResolvedProfile, role: MediaRole) -> str:
    """Return the registry key for `role`, preferring operator-wired roles."""
    for optional in _OPTIONAL_ROLES[role]:
        wired = resolved.model_keys.get(optional)
        if wired is not None:
            return wired
    profile = resolved.name
    defaults = _DEFAULT_KEYS.get(profile)
    if defaults is None:
        raise MediaConfigError(f"no media model table for profile {profile!r}")
    key = defaults[role]
    if key not in config.registry.models:
        raise MediaConfigError(f"profile {profile!r} references unknown media model {key!r}")
    return key


def media_model_entry(
    config: AppConfig, resolved: ResolvedProfile, role: MediaRole
) -> tuple[str, ModelEntry]:
    key = media_model_key(config, resolved, role)
    entry = config.registry.models[key]
    if entry.capability != _EXPECTED_CAPABILITY[role]:
        raise MediaConfigError(
            f"media role {role!r} resolved to {key!r} with capability "
            f"{entry.capability!r}, expected {_EXPECTED_CAPABILITY[role]!r}"
        )
    return key, entry


def media_spec(entry: ModelEntry) -> MediaSpec:
    if entry.media is None:
        raise MediaConfigError(
            f"model {entry.model_ref!r} has no media wire contract; confirm it in "
            "model_registry.yaml before spending"
        )
    return entry.media


def model_confirmation_problems(entry: ModelEntry, *, final: bool = False) -> list[str]:
    """Every reason a model must not be trusted with money, never abbreviated."""
    problems: list[str] = []
    if entry.placeholder:
        problems.append(f"unresolved placeholder endpoint {entry.model_ref!r}")
    if entry.needs_confirmation:
        problems.append("needs_confirmation is true")
    if entry.pricing_verified_on is None and not entry.simulated:
        problems.append("price never verified")
    if entry.is_zero_price and not entry.simulated and not entry.pricing_acknowledged_free:
        problems.append("zero price without pricing_acknowledged_free")
    if final and not entry.commercial_use_verified:
        problems.append("commercial_use_verified is false for final")
    return problems


def supported_aspects(entry: ModelEntry) -> tuple[Aspect, ...]:
    spec = media_spec(entry)
    values = spec.supported_aspects or list(entry.supported_aspects)
    if not values:
        raise MediaConfigError(f"model {entry.model_ref!r} declares no supported aspects")
    out: list[Aspect] = []
    for value in values:
        try:
            out.append(Aspect(str(value)))
        except ValueError as exc:
            raise MediaConfigError(
                f"model {entry.model_ref!r} has unsupported aspect {value!r}"
            ) from exc
    return tuple(dict.fromkeys(out))


def aspect_value(entry: ModelEntry, aspect: Aspect) -> str:
    spec = media_spec(entry)
    if aspect not in supported_aspects(entry):
        raise MediaConfigError(
            f"model {entry.model_ref!r} does not support aspect {aspect.value}"
        )
    value = spec.aspect_values.get(aspect.value, aspect.value)
    if "<CONFIRM" in value:
        raise MediaConfigError(
            f"model {entry.model_ref!r} aspect value for {aspect.value} is unconfirmed"
        )
    return value


def supported_durations_s(entry: ModelEntry) -> tuple[int, ...]:
    spec = media_spec(entry)
    values = spec.supported_durations_s or list(entry.supported_durations_s)
    if not values:
        raise MediaConfigError(f"model {entry.model_ref!r} declares no supported durations")
    return tuple(sorted({int(v) for v in values}))


def max_refs(entry: ModelEntry) -> int:
    return media_spec(entry).max_refs


def validate_ref_capable(entry: ModelEntry, *, required: int = 1) -> None:
    if required <= 0:
        return
    if entry.supports_refs is False or max_refs(entry) < required:
        raise MediaConfigError(
            f"model {entry.model_ref!r} is not verified reference-capable "
            f"(max_refs={max_refs(entry)}, supports_refs={entry.supports_refs})"
        )
