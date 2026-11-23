"""Strict validator for S14 bible proposals (Phase 5 §L).

The model's output is untrusted input. Every rule the spec names is enforced
here, and every violation raises `ProposalInvalid` with the reasons listed —
the caller decides whether to spend once more, never the validator:

1. all six top-level lists are present and strictly typed (no extras),
2. every fact carries `evidence` with at least one `scene_id`,
3. every `scene_id` exists in this episode (scene ids, with shot ids accepted
   as the fallback evidence universe — see `_KNOWN_IDS_NOTE`),
4. a `character_changes` entry names a known bible character or one of the
   proposed `new_characters`,
5. at most `max_facts` facts in total,
6. per-field length caps, and no duplicate/conflicting names.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.domain.errors import ProposalInvalid

# The spec's "S1 scenes" are Phase 3's `scenes` rows. A production run that has
# no scene rows yet (scenes are created by the demo/seed path) would otherwise
# make every proposal unvalidatable, so the episode's shot ids are accepted as
# evidence too: in this pipeline a scene ranges over shots, so both identify a
# place in the episode. Documented as a deviation in PHASE5_REPORT.md.
_KNOWN_IDS_NOTE = "scene or shot id of this episode"

MAX_PROBLEMS_REPORTED = 8

_FACT_LISTS = (
    "new_characters",
    "character_changes",
    "locations",
    "plot_events",
    "unresolved_threads",
    "continuity_notes",
)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Evidence(_Strict):
    scene_id: str = Field(min_length=1, max_length=64)


class NewCharacter(_Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)
    visual_tokens: list[str] = Field(default_factory=list, max_length=8)
    arc: str = Field(default="", max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=16)


class CharacterChange(_Strict):
    name: str = Field(min_length=1, max_length=80)
    change: str = Field(min_length=1, max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=16)


class Location(_Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=16)


class PlotEvent(_Strict):
    summary: str = Field(min_length=1, max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=16)


class UnresolvedThread(_Strict):
    name: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=16)


class ContinuityNote(_Strict):
    text: str = Field(min_length=1, max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=16)


class BibleUpdate(_Strict):
    """The strict shape of one proposal. No field is optional: a model that
    drops a list is a schema violation, not an empty suggestion."""

    new_characters: list[NewCharacter]
    character_changes: list[CharacterChange]
    locations: list[Location]
    plot_events: list[PlotEvent]
    unresolved_threads: list[UnresolvedThread]
    continuity_notes: list[ContinuityNote]

    def fact_count(self) -> int:
        return sum(len(getattr(self, name)) for name in _FACT_LISTS)

    def all_evidence_ids(self) -> list[str]:
        ids: list[str] = []
        for name in _FACT_LISTS:
            for fact in getattr(self, name):
                ids.extend(item.scene_id for item in fact.evidence)
        return ids


def _pydantic_problems(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location}: {error.get('msg', 'invalid')}")
    return problems


def validate_proposal(
    payload: Any,
    *,
    known_ids: Iterable[str],
    character_names: Iterable[str],
    max_facts: int = 40,
) -> BibleUpdate:
    """Parse and cross-check one proposal payload.

    Raises `ProposalInvalid` listing every problem found (up to a cap), so one
    re-prompt can fix all of them at once.
    """
    if not isinstance(payload, dict):
        raise ProposalInvalid("proposal must be a JSON object")
    try:
        update = BibleUpdate.model_validate(payload)
    except ValidationError as exc:
        problems = _pydantic_problems(exc)
        raise ProposalInvalid(
            "proposal schema violation: " + "; ".join(problems[:MAX_PROBLEMS_REPORTED])
        ) from exc

    problems = []
    known = set(known_ids)
    universe: set[str] = set(character_names) | {item.name for item in update.new_characters}

    unknown = sorted({item for item in update.all_evidence_ids() if item not in known})
    if unknown:
        problems.append(
            f"evidence cites unknown {_KNOWN_IDS_NOTE}(s): {', '.join(unknown[:6])}"
        )

    dangling = sorted(
        {item.name for item in update.character_changes if item.name not in universe}
    )
    if dangling:
        problems.append(
            "character_changes may only name a known bible character or a "
            f"new_characters entry: {', '.join(dangling[:6])}"
        )

    reused = sorted(
        {item.name for item in update.new_characters if item.name in set(character_names)}
    )
    if reused:
        problems.append(f"new_characters repeats an existing character: {', '.join(reused)}")

    duplicates = sorted(
        {
            item.name
            for item in update.new_characters
            if [other.name for other in update.new_characters].count(item.name) > 1
        }
    )
    if duplicates:
        problems.append(f"new_characters contains duplicates: {', '.join(duplicates)}")

    if update.fact_count() > max_facts:
        problems.append(f"{update.fact_count()} facts exceed bible.proposals.max_facts={max_facts}")

    empty = [name for name in _FACT_LISTS if not getattr(update, name)]
    if len(empty) == len(_FACT_LISTS):
        problems.append("proposal carries no facts at all")

    if problems:
        raise ProposalInvalid(
            "proposal rejected: " + "; ".join(problems[:MAX_PROBLEMS_REPORTED])
        )
    return update


def proposal_to_json(update: BibleUpdate | dict[str, Any]) -> dict[str, Any]:
    """Canonical dict form (what the `bible_proposals` row stores)."""
    if isinstance(update, BibleUpdate):
        return update.model_dump(mode="json")
    return BibleUpdate.model_validate(update).model_dump(mode="json")
