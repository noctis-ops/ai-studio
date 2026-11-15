"""T-TASH-* (PHASE2_IMPLEMENTATION_PROMPT §G) — Arabic tashkeel validator."""

from __future__ import annotations

import unicodedata

import pytest

from studio.language.tashkeel import (
    TASHKEEL_SET,
    check_tashkeel,
    normalize_nfc,
    strip_tashkeel,
)

# Canonical sample: fully voweled Fusha line.
SOURCE = "قَالَ الوَزِيرُ: القَانُونُ فَوقَ الجَمِيعِ"


# --------------------------------------------------------------- T-TASH-01


def test_equal_after_strip_passes():
    output = "قالَ الوزيرُ: القانونُ فوقَ الجميعِ"  # same letters, different vowels
    result = check_tashkeel(SOURCE, output)
    assert result.ok is True
    assert result.first_mismatch is None


# --------------------------------------------------------------- T-TASH-02


def test_tatweel_stripped_before_comparison():
    assert "ـ" not in strip_tashkeel("فـاـنـوسُ الفَجر")
    result = check_tashkeel("فانوس الفجر", "فـــانـــوس الفجر")
    assert result.ok is True


# --------------------------------------------------------------- T-TASH-03


def test_superscript_alef_stripped():
    # 0670 ARABIC LETTER SUPERSCRIPT ALEF
    assert "ٰ" in TASHKEEL_SET
    assert strip_tashkeel("هذٰلك") == "هذلك"
    # superscript alef is presentation; the underlying letters must match exactly
    assert check_tashkeel("هذلك", "هذٰلك").ok is True
    assert check_tashkeel("هذاك", "هذٰلك").ok is False


# --------------------------------------------------------------- T-TASH-04


def test_quranic_marks_stripped():
    for codepoint in (0x06D6, 0x06E4, 0x06ED):
        marker = chr(codepoint)
        assert marker in TASHKEEL_SET
        assert check_tashkeel("السَّلَامُ", f"السَّلا{marker}مُ").ok is True


# --------------------------------------------------------------- T-TASH-05


def test_letter_change_fails_with_first_mismatch_index():
    result = check_tashkeel(SOURCE, "قَالَ الوَزِيرُ: القَانُوقُ فَوقَ الجَمِيعِ".replace("ق", "ق", 1))
    assert result.ok is False
    assert result.first_mismatch is not None
    assert result.stripped_source[result.first_mismatch] != result.stripped_output[
        result.first_mismatch
    ]
    # ك → ق substitution at a known position
    altered = SOURCE.replace("قَانُونُ", "قَانُوقُ", 1)
    again = check_tashkeel(SOURCE, altered)
    assert again.ok is False
    assert again.first_mismatch is not None
    assert again.stripped_source[again.first_mismatch] == "ن"


# --------------------------------------------------------------- T-TASH-06


def test_diacritic_only_differences_ok_dropped_letter_fails():
    only_marks = check_tashkeel("كَتَبَ الوَلَدُ الدَّرسَ", "كتبَ الولدُ الدرسَ")
    assert only_marks.ok is True
    dropped = check_tashkeel("كَتَبَ الوَلَدُ الدَّرسَ", "كَتَبَ الوَلَدُ الدَرسَ")
    assert dropped.ok is True  # shadda dropped — presentation only
    missing_letter = check_tashkeel("كَتَبَ الوَلَدُ الدَّرسَ", "كَتَبَ الوَلَدُ الدَّرَ")
    assert missing_letter.ok is False


# --------------------------------------------------------------- T-TASH-07


def test_nfc_composition_compares_equal():
    decomposed = unicodedata.normalize("NFD", "café")
    composed = unicodedata.normalize("NFC", "café")
    assert decomposed != composed
    assert check_tashkeel(composed, decomposed).ok is True
    assert normalize_nfc(decomposed) == composed
    # Arabic letters are NFC-stable; combining sequences normalize cleanly
    arabic = "شَدَّةُ الفَتْحَةِ"
    assert check_tashkeel(arabic, unicodedata.normalize("NFD", arabic)).ok is True


# --------------------------------------------------------------- T-TASH-08


def test_empty_and_whitespace_inputs():
    assert check_tashkeel("", "").ok is True
    assert check_tashkeel("   ", "   ").ok is True
    assert check_tashkeel("", "ا").ok is False
    assert check_tashkeel("ا", "").ok is False
    assert check_tashkeel("   ", "    ").ok is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
