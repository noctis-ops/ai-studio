"""Arabic tashkeel (diacritics) validation.

Contract (PHASE2_IMPLEMENTATION_PROMPT §F3) — binding:

- The tashkeel set is exactly the codepoints in TASHKEEL_RANGES.
- `strip_tashkeel` removes those codepoints and nothing else: it changes no
  whitespace, inserts nothing, and reorders nothing.
- Comparison is on the *stripped* strings, both sides NFC-normalized first.
  Tashkeel is presentation, never information: two strings that differ only
  in tashkeel are the same text.
- `check_tashkeel` never "fixes" anything; it reports equality and, when the
  underlying letters were altered, the index of the first mismatch in the
  stripped strings. Callers decide what to do.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Codepoint ranges (inclusive), lowercase hex, exactly as specified.
TASHKEEL_RANGES: tuple[tuple[str, str], ...] = (
    ("0610", "061a"),
    ("064b", "065f"),
    ("0670", "0670"),
    ("06d6", "06ed"),
    ("0640", "0640"),  # tatweel / kashida
)


def _build_set() -> frozenset[str]:
    chars: list[str] = []
    for lo, hi in TASHKEEL_RANGES:
        for codepoint in range(int(lo, 16), int(hi, 16) + 1):
            chars.append(chr(codepoint))
    return frozenset(chars)


TASHKEEL_SET: frozenset[str] = _build_set()


def normalize_nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def strip_tashkeel(text: str) -> str:
    """Remove tashkeel codepoints. Whitespace and order are untouched."""
    return "".join(ch for ch in text if ch not in TASHKEEL_SET)


@dataclass(frozen=True)
class TashkeelCheck:
    ok: bool
    first_mismatch: int | None
    stripped_source: str
    stripped_output: str


def check_tashkeel(source: str, output: str) -> TashkeelCheck:
    """Strip-compare `output` against `source`.

    `ok` is True iff the stripped strings are identical. `first_mismatch`
    is the index (into the stripped strings) of the first differing
    codepoint, or len(shorter) when one side is a prefix of the other.
    """
    stripped_source = strip_tashkeel(normalize_nfc(source))
    stripped_output = strip_tashkeel(normalize_nfc(output))
    if stripped_source == stripped_output:
        return TashkeelCheck(
            ok=True,
            first_mismatch=None,
            stripped_source=stripped_source,
            stripped_output=stripped_output,
        )
    mismatch = min(len(stripped_source), len(stripped_output))
    for index, (a, b) in enumerate(zip(stripped_source, stripped_output, strict=False)):
        if a != b:
            mismatch = index
            break
    return TashkeelCheck(
        ok=False,
        first_mismatch=mismatch,
        stripped_source=stripped_source,
        stripped_output=stripped_output,
    )
