"""S13 export blockers (Phase 5 §K).

In the **final** profile every problem blocks the export, and *all* of them are
reported at once. In `sandbox`/`draft` the same problems are recorded as
`export_flags` in the sidecar instead, so a draft always says what would stop a
real release.

The commercial-use block is the one override-able rule: an explicit
`--override-commercial-use --reason "…"` turns it into a recorded override
(audit row + sidecar entry) instead of a blocker. Nothing else is overridable —
a missing licence or an unreviewed checklist is exactly what the gate exists
to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from studio.pipeline.checklist import checklist_problems


@dataclass(frozen=True)
class Blocker:
    """One reason an export may not be written in the final profile."""

    code: str
    detail: str
    overridable: bool = False

    def as_flag(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass
class ExportBlocks:
    blockers: list[Blocker] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [blocker.code for blocker in self.blockers]

    def flags(self) -> list[dict[str, Any]]:
        return [blocker.as_flag() for blocker in self.blockers]


@dataclass(frozen=True)
class ExportInputs:
    """Everything the block rules read, gathered by the stage (no I/O here)."""

    profile: str
    models: list[dict[str, Any]] = field(default_factory=list)
    cues: list[dict[str, Any]] = field(default_factory=list)
    source_ref: str | None = None
    source_license: str | None = None
    checklist: dict[str, Any] | None = None
    qc_status: str | None = None


def collect_blocks(
    inputs: ExportInputs,
    *,
    override_commercial_use: bool = False,
    override_reason: str | None = None,
) -> ExportBlocks:
    """Apply §K's five rules, collecting every violation."""
    blocks = ExportBlocks()

    # 1. Commercial use of every settled model (a deliberately conservative
    #    superset: any model the episode called, not just the media ones).
    unverified = sorted(
        {
            str(model.get("model_key"))
            for model in inputs.models
            if not model.get("commercial_use_verified")
        }
    )
    if unverified:
        if override_commercial_use:
            blocks.overrides.append(
                {
                    "rule": "commercial_use",
                    "models": unverified,
                    "reason": override_reason or "",
                }
            )
        else:
            blocks.blockers.append(
                Blocker(
                    "commercial_use_unverified",
                    "commercial use is unverified for: " + ", ".join(unverified),
                    overridable=True,
                )
            )

    # 2. Every music/SFX cue must have a licence record.
    unlicensed = sorted(
        {
            str(cue.get("library_sha256") or cue.get("title") or "<unknown cue>")
            for cue in inputs.cues
            if not cue.get("licensed")
        }
    )
    if unlicensed:
        blocks.blockers.append(
            Blocker(
                "license_missing",
                "no licence record for cue(s): " + ", ".join(unlicensed),
            )
        )

    # 3. An adaptation must name its source and licence.
    missing_source = [
        name
        for name, value in (
            ("source_ref", inputs.source_ref),
            ("source_license", inputs.source_license),
        )
        if not (value or "").strip()
    ]
    if inputs.source_ref or inputs.source_license:
        # Half-filled provenance is as bad as none: it looks like an original
        # work while claiming an adaptation.
        if missing_source:
            blocks.blockers.append(
                Blocker(
                    "adaptation_incomplete",
                    "adaptation is missing: " + ", ".join(missing_source),
                )
            )

    # 4. The G3 checklist must be human-complete.
    problems = checklist_problems(inputs.checklist)
    if problems:
        blocks.blockers.append(
            Blocker("checklist_incomplete", "; ".join(problems))
        )

    # 5. S12 must not have failed (it fails the stage, but a stale success row
    #    must never export a render whose QC reads `fail`).
    if inputs.qc_status is not None and inputs.qc_status == "fail":
        blocks.blockers.append(
            Blocker("render_qc_failed", "the render QC report reads fail")
        )

    return blocks
