"""Opt-in LIVE smoke test — real, paid LLM calls.

NEVER run in CI. Requires ``STUDIO_LIVE_LLM=1`` plus real credentials in the
environment (``ANTHROPIC_API_KEY`` and ``OPENAI_API_KEY``). Makes exactly two
small calls:

1. ``bible_upsert``  → llm.creative (Anthropic)
2. ``tashkeel_verify`` → llm.light (OpenAI)

and prints token usage and integer micro-USD for each. Any failure aborts
with a non-zero exit code. Use ``scripts/live_llm_smoke.py`` or the CLI
``studio doctor --llm`` for the same effect.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from studio.config.loader import load_all
from studio.core.pricing import actual_cost_musd
from studio.providers.base import LLMRequest, LLMResult
from studio.providers.factory import build_provider

LIVE_FLAG = "STUDIO_LIVE_LLM"
ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"

SAMPLE_TASHKEEL = "فَانوسُ الفَجْرِ يُضيءُ الطَّريقَ لِلجَميعِ"


@dataclass(frozen=True)
class SmokeResult:
    purpose: str
    model_id: str
    tokens_in: int
    tokens_out: int
    musd: int
    excerpt: str


def smoke_requests() -> dict[str, LLMRequest]:
    """The two minimal live requests (shared by script and smoke test)."""
    return {
        "bible_upsert": LLMRequest(
            system=(
                "You are the AI Director of an Arabic-first cinematic studio. "
                "Reply with compact JSON only."
            ),
            prompt=(
                'Reply with exactly: {"title": "فانوس الفجر", '
                '"premise": "حارس فانوس يتعلم أن الضوء يقتسم"}'
            ),
            max_tokens=128,
            temperature=0.7,
            expect_json=True,
        ),
        "tashkeel_verify": LLMRequest(
            system=(
                "Re-emit the Arabic line exactly as given, with identical letters "
                "and identical tashkeel. Reply with the line only."
            ),
            prompt=SAMPLE_TASHKEEL,
            max_tokens=256,
            temperature=0.2,
        ),
    }


def missing_env() -> list[str]:
    return [name for name in (LIVE_FLAG, ANTHROPIC_KEY, OPENAI_KEY) if not os.environ.get(name)]


async def run_smoke(home: Path | None = None) -> list[SmokeResult]:
    missing = missing_env()
    if missing:
        raise RuntimeError(
            "live smoke refused; missing environment: " + ", ".join(missing)
        )
    resolved_home = Path(home) if home else Path(os.environ.get("STUDIO_HOME", "."))
    config = await asyncio.to_thread(load_all, resolved_home)
    results: list[SmokeResult] = []
    for purpose, request in smoke_requests().items():
        role = "llm_creative" if purpose == "bible_upsert" else "llm_light"
        profile = config.registry.resolve_llm_subset("draft")
        entry = profile.entry_for(role)
        adapter = build_provider(entry, settings=config.settings)
        result: LLMResult = await adapter.complete(request)
        musd = actual_cost_musd(entry, request, result)
        results.append(
            SmokeResult(
                purpose=purpose,
                model_id=str(entry.model_id),
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                musd=musd,
                excerpt=result.text.strip()[:120],
            )
        )
    return results


def main() -> int:
    missing = missing_env()
    if missing:
        print(
            "live smoke refused; missing environment: " + ", ".join(missing)
            + "\nSet STUDIO_LIVE_LLM=1 and real API keys to opt in (never in CI)."
        )
        return 2
    results = asyncio.run(run_smoke())
    total = 0
    for item in results:
        total += item.musd
        print(
            f"{item.purpose:16s} {item.model_id:28s} "
            f"in={item.tokens_in} out={item.tokens_out} musd={item.musd} :: {item.excerpt}"
        )
    print(f"TOTAL musd={total}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
