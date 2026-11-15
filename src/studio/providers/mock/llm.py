"""Deterministic mock LLM.

Same prompt (+schema) ⇒ same bytes out. `demo_shots` returns a valid shot
list so the demo pipeline runs end to end with zero spend.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from studio.providers.base import LLMRequest, LLMResult

from .common import MockBase, worst_case_tokens

DEMO_SHOT_COUNT = 3


class MockLLMProvider(MockBase):
    async def complete(self, req: LLMRequest) -> LLMResult:
        await self._tick()
        seed_material = f"{req.system}\x00{req.prompt}\x00{req.schema_name or ''}".encode()
        digest = hashlib.sha256(seed_material).hexdigest()
        if req.schema_name == "demo_shots":
            shots = [
                {"shot_no": i + 1, "prompt": f"demo shot {i + 1} ({digest[:8]})"}
                for i in range(DEMO_SHOT_COUNT)
            ]
            parsed: dict[str, Any] | None = {"shots": shots}
            text = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        elif req.schema_name:
            parsed = {"echo_sha256": digest, "schema_name": req.schema_name}
            text = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        else:
            parsed = None
            text = f"mock-llm:{digest}"
        self._record(
            "complete",
            schema_name=req.schema_name,
            prompt_sha256=hashlib.sha256(req.prompt.encode()).hexdigest(),
        )
        return LLMResult(
            text=text,
            parsed_json=parsed,
            tokens_in=worst_case_tokens(req.prompt),
            tokens_out=worst_case_tokens(text),
        )
