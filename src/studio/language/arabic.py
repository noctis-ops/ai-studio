"""Arabic (and English) text preparation for TTS (PHASE3 §F3/§H6).

`normalize_for_tts(language, text, settings)` is the ONLY entry point stages
use. For Arabic it honors ``tts.keep_diacritics``: when false, tashkeel
(diacritics) are stripped via the Phase 2 `strip_tashkeel` contract — never
any other edit. For every language: NFC normalization and nothing else
(no "smart" rewriting).

`count_words` is the diacritic-aware word counter used by S6 QC and
`studio audio calibrate`: tokens are whitespace-separated and must contain at
least one letter or digit after the diacritics are stripped.
"""

from __future__ import annotations

from typing import Any

from studio.domain.enums import Language
from studio.language.tashkeel import normalize_nfc, strip_tashkeel


def normalize_for_tts(language: Language, text: str, settings: Any) -> str:
    """Return the exact string that will be sent to the TTS provider."""
    keep = bool(getattr(settings.tts, "keep_diacritics", True))
    out = normalize_nfc(text)
    if language == Language.AR and not keep:
        out = strip_tashkeel(out)
    return out


def count_words(language: Language, text: str) -> int:
    """Whitespace-token count of words containing a letter or digit."""
    out = normalize_nfc(text)
    if language == Language.AR:
        out = strip_tashkeel(out)
    count = 0
    for token in out.split():
        if any(ch.isalnum() for ch in token):
            count += 1
    return count


__all__ = ["count_words", "normalize_for_tts"]
