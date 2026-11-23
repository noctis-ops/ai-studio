"""`metadata.json` and `credits_licenses.txt` (Phase 5 §K).

The sidecar is the export's own record: it is written from persisted state
only (the approved metadata, the S11 render report, the S12 QC report, the
ledger and the gates) and regenerates no text. `schema_version: 1` is frozen —
a later change is a new version, not a silent edit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# §K: the tool cannot flip a platform's AI-disclosure toggle, so the text file
# says only that. The exact policy wording belongs to the platform.
_DISCLOSURE_REMINDER = (
    "AI disclosure: this production is AI-generated. Set the platform's own "
    "AI-disclosure toggle when uploading; this tool cannot do it."
)


def build_metadata(
    *,
    channel_key: str,
    language: str,
    aspect: str,
    preset: dict[str, Any],
    profile: str,
    title: str,
    description: str,
    hashtags: list[str],
    audience: dict[str, Any],
    ai_disclosure: bool,
    source: dict[str, Any],
    render: dict[str, Any],
    qc: dict[str, Any],
    credits: list[str],
    models: list[dict[str, Any]],
    cost: dict[str, Any],
    gates: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    export_flags: list[dict[str, Any]],
    files: dict[str, str],
) -> dict[str, Any]:
    """Assemble the sidecar payload (sorted keys are applied on write)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "channel_key": channel_key,
        "language": language,
        "aspect": aspect,
        "preset": preset,
        "profile": profile,
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "audience": audience,
        "ai_disclosure": ai_disclosure,
        "source": source,
        "render": render,
        "qc": qc,
        "credits": credits,
        "models": models,
        "cost": cost,
        "gates": gates,
        "overrides": overrides,
        "export_flags": export_flags,
        "files": files,
    }


def write_metadata_json(path: Path, payload: dict[str, Any]) -> None:
    """UTF-8, `ensure_ascii=False`, sorted keys, indent 2 (§K)."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_credits(
    *,
    title: str,
    credits: list[str],
    models: list[dict[str, Any]],
    attribution: list[str],
) -> str:
    """`credits_licenses.txt`: licence ids, model list, attribution lines."""
    lines = [f"Credits and licences — {title}", ""]
    lines.append("Audio licences:")
    if credits:
        lines.extend(f"  - {item}" for item in credits)
    else:
        lines.append("  - none")
    if attribution:
        lines.append("")
        lines.append("Attribution required:")
        lines.extend(f"  - {item}" for item in attribution)
    lines.append("")
    lines.append("Models:")
    for model in models:
        verified = "verified" if model.get("commercial_use_verified") else "UNVERIFIED"
        lines.append(
            f"  - {model.get('model_key')} ({model.get('provider')}) — commercial use {verified}"
        )
    lines.append("")
    lines.append(_DISCLOSURE_REMINDER)
    lines.append("")
    return "\n".join(lines)
