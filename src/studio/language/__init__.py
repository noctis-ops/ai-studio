"""Language utilities (Arabic)."""

from studio.language.tashkeel import (
    TASHKEEL_RANGES,
    TASHKEEL_SET,
    TashkeelCheck,
    check_tashkeel,
    normalize_nfc,
    strip_tashkeel,
)

__all__ = [
    "TASHKEEL_RANGES",
    "TASHKEEL_SET",
    "TashkeelCheck",
    "check_tashkeel",
    "normalize_nfc",
    "strip_tashkeel",
]
