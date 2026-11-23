"""Export slug sanitation (Phase 5 §K / C7 / T-EXP-08).

`episodes` has no slug column (`series.slug` is the only one, and it identifies
the series rather than the episode), so the export slug is derived from the
episode title. Arabic titles do not transliterate reliably, so a title that is
not already ASCII falls back to `ep-<episode id prefix>` — deterministic, and
stable across re-exports of the same episode.

Rules: lowercase `[a-z0-9-]`, no leading/trailing dash, no double dashes, at
most 48 characters, and never a Windows reserved device name.
"""

from __future__ import annotations

import re
import unicodedata

MAX_SLUG_CHARS = 48

# Windows refuses these as file/directory names (case-insensitive, with or
# without an extension).
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_DASHES = re.compile(r"-{2,}")


def _ascii_fold(text: str) -> str:
    """Fold to ASCII, dropping anything that has no decomposition."""
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def sanitize_slug(text: str, *, max_chars: int = MAX_SLUG_CHARS) -> str:
    """Lowercase ASCII slug; may return an empty string for unusable input."""
    slug = _NON_SLUG.sub("-", _ascii_fold(text).lower())
    slug = _DASHES.sub("-", slug).strip("-")
    if len(slug) > max_chars:
        slug = slug[:max_chars].rstrip("-")
    return slug


def is_windows_reserved(slug: str) -> bool:
    """True for `con`, `nul`, `com1` … including `con.txt`-style tails."""
    head = slug.split(".", 1)[0].strip().lower()
    return head in _WINDOWS_RESERVED


def export_slug(title: str, episode_id: str, *, max_chars: int = MAX_SLUG_CHARS) -> str:
    """Deterministic, filesystem-safe slug for an episode.

    An ASCII title sanitizes well; an Arabic (or otherwise non-ASCII) title
    falls back to `ep-<id[:8]>` rather than producing an empty slug.
    """
    slug = sanitize_slug(title, max_chars=max_chars)
    if not slug:
        slug = sanitize_slug(f"ep-{episode_id[:8]}", max_chars=max_chars)
    if is_windows_reserved(slug):
        # `con` → `ep-con` keeps it recognisable and still deterministic.
        slug = sanitize_slug(f"ep-{slug}", max_chars=max_chars)
    return slug
