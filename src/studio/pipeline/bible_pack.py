"""Deterministic context packer for the text pipeline (PHASE2 §F4).

Sections always appear in the order SERIES_BIBLE → EPISODE → PRIOR_WORK →
STAGE_INPUT. Budget accounting is worst-case Arabic tokens: tokens =
ceil(chars * 0.5). The SERIES_BIBLE section may be truncated to 70% of the
budget but is never dropped entirely (hard floor: title + premise +
characters + style_tokens); the rest is cut with "…[truncated N chars]".
Same inputs produce byte-identical output and therefore a stable pack_sha256,
which the S-stage input_hash carries.

`max_section_chars`, when set, caps every section (with the same marker) —
a debugging aid; the hard token cap still applies.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from studio.domain.series_bible import SeriesBible

_TOKENS_PER_CHAR = 0.5
_BIBLE_BUDGET_FRACTION = 0.7
_EPISODE_BUDGET_FRACTION = 0.25


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EpisodeSummary(_Strict):
    episode_id: str
    series_id: str
    title: str
    concept: str
    languages: list[str]
    aspects: list[str]


class DeliverableSummary(_Strict):
    deliverable_id: str
    track_id: str
    language: str
    channel_id: str
    created_at: str
    title: str
    summary: str


@dataclass(frozen=True)
class ContextPack:
    text: str
    pack_sha256: str
    n_chars: int
    truncated_sections: tuple[str, ...]


def _tokens(text: str) -> int:
    return math.ceil(len(text) * _TOKENS_PER_CHAR)


def _truncate_with_marker(text: str, max_chars: int) -> tuple[str, int]:
    """Fit `text` into `max_chars`, appending '…[truncated N chars]'.

    Returns (result, n_removed). len(result) <= max_chars. N counts the
    removed body characters; iterates to a fixed point because the marker
    width shifts by at most one digit.
    """
    if len(text) <= max_chars:
        return text, 0
    if max_chars <= 0:
        return "", len(text)
    n_removed = max(0, len(text) - max_chars)
    for _ in range(4):
        marker = f"…[truncated {n_removed} chars]"
        keep = max(0, max_chars - len(marker))
        new_n = len(text) - keep
        if new_n == n_removed:
            break
        n_removed = new_n
    marker = f"…[truncated {n_removed} chars]"
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker, n_removed


def _bible_floor(bible: SeriesBible) -> str:
    lines = [
        "# SERIES_BIBLE",
        f"title: {bible.title}",
        f"premise: {bible.premise}",
        "characters:",
    ]
    for character in bible.characters:
        lines.append(f"- {character.name}: {character.description}")
        tokens = ", ".join(character.visual_tokens)
        if tokens:
            lines.append(f"  visual_tokens: {tokens}")
        if character.arc:
            lines.append(f"  arc: {character.arc}")
    lines.append(f"style_tokens: {', '.join(bible.style_tokens)}")
    return "\n".join(lines)


def _bible_section(bible: SeriesBible, token_budget: int) -> tuple[str, bool]:
    floor = _bible_floor(bible)
    rest_lines: list[str] = []
    if bible.world:
        rest_lines.append(f"world: {bible.world}")
    if bible.tone:
        rest_lines.append(f"tone: {', '.join(bible.tone)}")
    if bible.continuity_notes:
        rest_lines.append(f"continuity_notes: {bible.continuity_notes}")
    rest = "\n".join(rest_lines)
    full = f"{floor}\n{rest}" if rest else floor
    cap_chars = int((token_budget / _TOKENS_PER_CHAR) * _BIBLE_BUDGET_FRACTION)
    if len(full) <= cap_chars:
        return full, False
    if len(floor) >= cap_chars:
        # Hard floor wins: premise + characters + style_tokens are never cut.
        return floor, True
    room = cap_chars - len(floor) - 1  # newline joining floor and rest
    body, _ = _truncate_with_marker(rest, room)
    return f"{floor}\n{body}", True


def _episode_section(episode: EpisodeSummary, cap_chars: int) -> tuple[str, bool]:
    section = "\n".join(
        [
            "# EPISODE",
            f"title: {episode.title}",
            f"series: {episode.series_id}",
            f"languages: {', '.join(episode.languages)}",
            f"aspects: {', '.join(episode.aspects)}",
            f"concept: {episode.concept}",
        ]
    )
    if len(section) <= cap_chars:
        return section, False
    clipped, _ = _truncate_with_marker(section, cap_chars)
    return clipped, True


def _prior_work_section(prior: list[DeliverableSummary]) -> str | None:
    if not prior:
        return None
    ordered = sorted(prior, key=lambda item: (item.created_at, item.deliverable_id))
    lines = ["# PRIOR_WORK"]
    for item in ordered:
        lines.append(
            f"- {item.created_at} · deliverable {item.deliverable_id} "
            f"({item.language} · {item.track_id}): {item.title} — {item.summary}"
        )
    return "\n".join(lines)


def pack_context(
    *,
    bible: SeriesBible | None,
    episode: EpisodeSummary,
    prior_deliverables: list[DeliverableSummary],
    stage_input: str,
    token_budget: int = 6000,
    max_section_chars: int | None = None,
) -> ContextPack:
    if token_budget < 500:
        raise ValueError(f"token_budget {token_budget} is below the 500-token floor")

    truncated: list[str] = []
    cap_chars = int(token_budget / _TOKENS_PER_CHAR)

    parts: dict[str, str | None] = {}

    if bible is not None:
        bible_text, bible_cut = _bible_section(bible, token_budget)
        parts["SERIES_BIBLE"] = bible_text
        if bible_cut:
            truncated.append("SERIES_BIBLE")
    else:
        parts["SERIES_BIBLE"] = None

    episode_text, episode_cut = _episode_section(
        episode, max(200, int(cap_chars * _EPISODE_BUDGET_FRACTION))
    )
    parts["EPISODE"] = episode_text
    if episode_cut:
        truncated.append("EPISODE")

    parts["PRIOR_WORK"] = _prior_work_section(prior_deliverables)
    parts["STAGE_INPUT"] = f"# STAGE_INPUT\n{stage_input}"

    if max_section_chars is not None:
        for key, value in list(parts.items()):
            if value is not None and len(value) > max_section_chars:
                clipped, _ = _truncate_with_marker(value, max_section_chars)
                parts[key] = clipped
                if key not in truncated:
                    truncated.append(key)

    def render() -> str:
        return "\n\n".join(
            section
            for section in (
                parts["SERIES_BIBLE"],
                parts["EPISODE"],
                parts["PRIOR_WORK"],
                parts["STAGE_INPUT"],
            )
            if section
        )

    # 1) Shrink PRIOR_WORK by dropping the oldest entries first.
    prior = parts["PRIOR_WORK"]
    if prior is not None and _tokens(render()) > token_budget:
        lines = prior.splitlines()
        header, entries = lines[0], lines[1:]
        dropped = 0
        while entries and _tokens(render()) > token_budget:
            entries.pop(0)  # oldest first
            dropped += 1
            parts["PRIOR_WORK"] = "\n".join([header, *entries]) if entries else None
        if dropped:
            truncated.append("PRIOR_WORK")

    # 2) Truncate STAGE_INPUT at the tail.
    stage = parts["STAGE_INPUT"]
    if stage is not None and _tokens(render()) > token_budget:
        others = "\n\n".join(
            section for key, section in parts.items() if key != "STAGE_INPUT" and section
        )
        room_tokens = max(0, token_budget - _tokens(others))
        # Subtract a fixed allowance for the section separators so the
        # reassembled pack lands strictly below the hard cap.
        room_chars = max(0, int(room_tokens / _TOKENS_PER_CHAR) - 16)
        body = stage.split("\n", 1)[1] if "\n" in stage else ""
        clipped, _ = _truncate_with_marker(body, max(0, room_chars - len("# STAGE_INPUT\n")))
        parts["STAGE_INPUT"] = f"# STAGE_INPUT\n{clipped}" if clipped else "# STAGE_INPUT"
        truncated.append("STAGE_INPUT")

    # 3) Last resort: clamp the whole pack.
    text = render()
    if _tokens(text) > token_budget:
        text, _ = _truncate_with_marker(text, int(token_budget / _TOKENS_PER_CHAR))
        truncated.append("PACK")

    n_chars = len(text)
    return ContextPack(
        text=text,
        pack_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        n_chars=n_chars,
        truncated_sections=tuple(truncated),
    )


def episode_summary_from_row(row: Any) -> EpisodeSummary:
    """Build an EpisodeSummary from an EpisodeRow (decodes *_json fields)."""
    import json

    languages = json.loads(row.languages_json)
    aspects = json.loads(row.aspects_json)
    return EpisodeSummary(
        episode_id=row.id,
        series_id=row.series_id,
        title=row.title,
        concept=row.concept,
        languages=[str(item) for item in languages],
        aspects=[str(item) for item in aspects],
    )
