"""Turning an accepted proposal into a new bible version (Phase 5 §L).

`apply_proposal` is pure: it takes the current `BibleContent` and a validated
`BibleUpdate` and returns the next content. Nothing here writes to the DB — the
service calls `BibleRepo.create_version`, which is the only write path and
already dedupes by content sha.

Where each proposal list lands (the bible model has no dedicated field for
some of them, and inventing fields would break `BibleContent`'s strict schema):

- `new_characters`  → `characters` (appended; existing order is preserved),
- `character_changes` → appended to that character's `arc`, one line each,
- `locations`       → appended to `world`, one line each,
- `plot_events`, `unresolved_threads`, `continuity_notes`
                    → appended to `continuity_notes` as dated, cited lines.

Every appended line carries its evidence ids, so a reader can trace a bible
fact back to the scenes that produced it.
"""

from __future__ import annotations

from studio.domain.series_bible import BibleContent, CharacterBible

from .schema import BibleUpdate

_SEPARATOR = "\n"


def _evidence_suffix(ids: list[str]) -> str:
    return f" [{', '.join(ids)}]" if ids else ""


def _append(existing: str, lines: list[str]) -> str:
    body = existing.rstrip("\n")
    addition = _SEPARATOR.join(line for line in lines if line)
    if not addition:
        return body
    return f"{body}{_SEPARATOR}{addition}" if body else addition


def apply_proposal(content: BibleContent, update: BibleUpdate) -> BibleContent:
    """The next bible content for an accepted proposal."""
    characters = list(content.characters)
    by_name = {character.name: index for index, character in enumerate(characters)}

    for proposal in update.new_characters:
        tokens = [token.strip() for token in proposal.visual_tokens if token.strip()]
        characters.append(
            CharacterBible(
                name=proposal.name.strip(),
                description=proposal.description.strip(),
                visual_tokens=tokens,
                arc=proposal.arc.strip(),
            )
        )
        by_name[proposal.name.strip()] = len(characters) - 1

    for change in update.character_changes:
        index = by_name.get(change.name.strip())
        if index is None:  # validator guarantees this cannot happen
            raise KeyError(f"character {change.name!r} is not in the bible")
        character = characters[index]
        line = f"{change.change.strip()}{_evidence_suffix([e.scene_id for e in change.evidence])}"
        characters[index] = character.model_copy(
            update={"arc": _append(character.arc, [line])}
        )

    world_lines = [
        f"- {location.name.strip()}: {location.description.strip()}"
        f"{_evidence_suffix([e.scene_id for e in location.evidence])}"
        for location in update.locations
    ]
    continuity_lines = [
        *(
            f"- حدث: {event.summary.strip()}"
            f"{_evidence_suffix([e.scene_id for e in event.evidence])}"
            for event in update.plot_events
        ),
        *(
            f"- خيط مفتوح: {thread.name.strip()}"
            + (f" — {thread.note.strip()}" if thread.note.strip() else "")
            + _evidence_suffix([e.scene_id for e in thread.evidence])
            for thread in update.unresolved_threads
        ),
        *(
            f"- ملاحظة استمرارية: {note.text.strip()}"
            f"{_evidence_suffix([e.scene_id for e in note.evidence])}"
            for note in update.continuity_notes
        ),
    ]

    return content.model_copy(
        update={
            "characters": characters,
            "world": _append(content.world, world_lines),
            "continuity_notes": _append(content.continuity_notes, continuity_lines),
        }
    )
