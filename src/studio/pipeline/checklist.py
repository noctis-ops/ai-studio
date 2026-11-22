"""Gate G3 approval checklist (Phase 5 §J).

The checklist is operator attestation, not telemetry. It is stored verbatim on
the gate row (``approval_gates.checklist_json``), copied into the export sidecar
and re-read by S13, which refuses a final export when any item is missing.

Values are ``true`` / ``false`` or the string ``"auto"`` (only for a
machine-filled draft approval). Unknown keys are refused so a typo cannot
silently weaken the attestation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from studio.domain.errors import GatePolicyError

REVIEWED_FULL_RENDER = "reviewed_full_render"
AUDIENCE_MADE_FOR_KIDS = "audience_made_for_kids"
AI_DISCLOSURE = "ai_disclosure"
THIRD_PARTY_IP_CONFIRMED = "third_party_ip_confirmed"
LICENSES_CONFIRMED = "licenses_confirmed"

REQUIRED_ITEMS: tuple[str, ...] = (
    REVIEWED_FULL_RENDER,
    AUDIENCE_MADE_FOR_KIDS,
    AI_DISCLOSURE,
    THIRD_PARTY_IP_CONFIRMED,
    LICENSES_CONFIRMED,
)

# Items that default to true when a human names only the rest.
_DEFAULT_TRUE = (AI_DISCLOSURE,)


def _is_auto(value: Any) -> bool:
    return bool(value == "auto")


def normalize_checklist(
    raw: Mapping[str, Any] | None, *, profile: str, auto: bool = False
) -> dict[str, Any]:
    """Return a complete checklist or raise ``GatePolicyError``.

    ``final`` never accepts a machine-filled or partial checklist. In
    ``sandbox`` / ``draft`` the operator may omit items (they become ``auto``)
    and an auto-approved gate is filled entirely with ``auto`` markers.
    """
    data: dict[str, Any] = dict(raw or {})
    unknown = sorted(set(data) - set(REQUIRED_ITEMS))
    if unknown:
        raise GatePolicyError(
            f"unknown checklist item(s): {', '.join(unknown)}; "
            f"allowed: {', '.join(REQUIRED_ITEMS)}"
        )
    if auto:
        return {item: "auto" for item in REQUIRED_ITEMS}
    if profile == "final":
        missing = [item for item in REQUIRED_ITEMS if item not in data]
        if missing:
            raise GatePolicyError(
                "final approval requires every checklist item; missing: "
                + ", ".join(missing)
            )
        for item in REQUIRED_ITEMS:
            value = data[item]
            if not isinstance(value, bool):
                raise GatePolicyError(
                    f"checklist item {item!r} must be true or false in the final profile"
                )
        if data[REVIEWED_FULL_RENDER] is not True:
            raise GatePolicyError("reviewed_full_render must be true to approve a final export")
        return {item: data[item] for item in REQUIRED_ITEMS}
    for item in _DEFAULT_TRUE:
        data.setdefault(item, True)
    return {item: data.get(item, "auto") for item in REQUIRED_ITEMS}


def checklist_problems(checklist: Mapping[str, Any] | None) -> list[str]:
    """Items missing, false or machine-filled (S13 final blocker)."""
    if not checklist:
        return ["no checklist recorded on the G3 round"]
    problems: list[str] = []
    for item in REQUIRED_ITEMS:
        if item not in checklist:
            problems.append(f"{item}: missing")
            continue
        value = checklist[item]
        if _is_auto(value):
            problems.append(f"{item}: auto-filled, not reviewed")
        elif item == REVIEWED_FULL_RENDER and value is not True:
            problems.append(f"{item}: not confirmed")
        elif not isinstance(value, bool):
            problems.append(f"{item}: invalid value {value!r}")
    return problems
