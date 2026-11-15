"""T-LIVE-01 — opt-in LIVE smoke against real Anthropic + OpenAI.

Not collected unless STUDIO_LIVE_LLM=1 (see conftest collect_ignore). Never
run in CI; the operator invokes it explicitly:

    STUDIO_LIVE_LLM=1 ANTHROPIC_API_KEY=... OPENAI_API_KEY=... \\
        python scripts/live_llm_smoke.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from studio.live_smoke import run_smoke


@pytest.mark.skipif(os.environ.get("STUDIO_LIVE_LLM") != "1", reason="opt-in live smoke")
def test_live_llm_smoke_prints_usage(capsys):
    results = asyncio.run(run_smoke())
    assert [item.purpose for item in results] == ["bible_upsert", "tashkeel_verify"]
    for item in results:
        assert item.tokens_in >= 1 and item.tokens_out >= 1
        assert item.musd >= 0
        print(f"{item.purpose}: in={item.tokens_in} out={item.tokens_out} musd={item.musd}")
    captured = capsys.readouterr()
    assert "musd=" in captured.out
