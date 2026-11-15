"""STUDIO_LIVE_LLM is never set in CI (PHASE2_IMPLEMENTATION_PROMPT §H).

If this fails, the suite was launched with live-LLM opt-in; run the live
smoke via `python scripts/live_llm_smoke.py` instead.
"""

from __future__ import annotations

import os


def test_studio_live_llm_not_set_for_suite():
    assert os.environ.get("STUDIO_LIVE_LLM") != "1"
