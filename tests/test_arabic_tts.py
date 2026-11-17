"""T-LANG (PHASE3 §I): the `language.normalize_for_tts` contract."""

from __future__ import annotations

from studio.domain.enums import Language
from studio.language import count_words, normalize_for_tts

TASHKEEL_SAMPLE = "مَرْحَبًا بِكُمْ فِي اِخْتِبَارِ ٱلِٱسْتِوْدِيُو"


class _FakeTts:
    def __init__(self, keep_diacritics: bool) -> None:
        self.keep_diacritics = keep_diacritics


class _FakeSettings:
    def __init__(self, keep_diacritics: bool) -> None:
        self.tts = _FakeTts(keep_diacritics)


def test_normalize_for_tts_keeps_diacritics_by_default():
    out = normalize_for_tts(Language.AR, TASHKEEL_SAMPLE, _FakeSettings(True))
    assert out == TASHKEEL_SAMPLE


def test_normalize_for_tts_strips_diacritics_when_disabled():
    out = normalize_for_tts(Language.AR, TASHKEEL_SAMPLE, _FakeSettings(False))
    assert "ً" not in out and "َ" not in out and "ُ" not in out
    assert "مرحبا" in out


def test_normalize_for_tts_never_rewrites_other_languages():
    text = "Hello, world! It's 100% fine."
    assert normalize_for_tts(Language.EN, text, _FakeSettings(False)) == text


def test_normalize_for_tts_nfc():
    decomposed = "e\u0301"  # e + combining acute
    out = normalize_for_tts(Language.EN, decomposed, _FakeSettings(True))
    assert out == "é"


def test_count_words_arabic_diacritics_insensitive():
    keep = count_words(Language.AR, TASHKEEL_SAMPLE)
    stripped = count_words(Language.AR, "مرحبا بكم في اختبار الاستوديو")
    assert keep == stripped > 0


def test_count_words_ignores_punctuation_only_tokens():
    assert count_words(Language.EN, "hello — world ... 42!") == 3
