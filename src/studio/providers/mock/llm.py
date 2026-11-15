"""Deterministic mock LLM.

Same prompt (+schema) ⇒ same bytes out. `demo_shots` returns a valid shot
list so the demo pipeline runs end to end with zero spend.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from studio.domain.enums import Language
from studio.providers.base import LLMRequest, LLMResult

from .common import MockBase, worst_case_tokens

DEMO_SHOT_COUNT = 3


def _mock_bible(digest: str) -> dict[str, Any]:
    """Deterministic bible payload (two recurring characters)."""
    return {
        "title": "فانوسُ الفجرِ",
        "premise": f"رحلةُ حارسِ الفانوسِ معَ العاصفةِ ({digest[:12]})",
        "world": ("ميناءٌ صخريٌّ قديمٌ عندَ طرفِ الخريطةِ؛ "
                  "بيوتٌ حجريةٌ وفوانيسُ نحاسيةٌ في الضباب."),
        "characters": [
            {
                "name": "نُور",
                "description": "حارسُ الفانوسِ الجديد؛ هادئٌ يراقبُ الأفقَ كلَّ ليلةٍ",
                "visual_tokens": ["معطفٌ كهرماني", "فانوسٌ نحاسي"],
                "arc": "يتعلّمُ أنَّ الضوءَ يُقتسمُ ولا يُخبأ",
            },
            {
                "name": "العاصفة",
                "description": "عاصفةٌ مُجسَّدةٌ تختبرُ تمركزَ الحارسِ كلَّ شتاء",
                "visual_tokens": ["غيماتٌ دوّادة", "ومضاتٌ زرقاء"],
                "arc": "تذوبُ أمامَ مَن لا يخافُها",
            },
        ],
        "style_tokens": ["دفءٌ سينمائي", "حبيباتُ فيلم", "تباينٌ هادئ"],
        "tone": ["حنون", "ملحميٌّ هادئ"],
        "continuity_notes": "نُور لا يغادرُ الميناءَ حتى الحلقةُ الأخيرة.",
    }


def _mock_script(req: LLMRequest, digest: str) -> dict[str, Any]:
    ar = Language.AR.value
    en = Language.EN.value
    lines: list[dict[str, Any]] = [
        {"no": 1, "language": ar,
         "text_tashkeel": "كَانَ يا ما كَانَ، فِي قَديمِ الزَّمانِ، فَانوسٌ يَرقُبُ الفَجرَ."},
        {"no": 2, "language": ar,
         "text_tashkeel": "وَقَفَ نُورٌ حارِسَ ذٰلِكَ الفانوسِ، فَأَحَبَّ العاصِفَةَ حينَ هَبَّت."},
        {"no": 3, "language": ar,
         "text_tashkeel": "وَحينَ انجَلَتِ الغُيومُ، عَلِمَ أَنَّ الضَّوءَ يُقتَسَمُ وَلا يُخبَأ."},
    ]
    if '"en"' in req.prompt:
        lines.extend(
            [
                {"no": 1, "language": en,
                 "text_tashkeel": "Long ago, a lighthouse watched the dawn."},
                {"no": 2, "language": en,
                 "text_tashkeel": "Noor, its keeper, learned to love the storm."},
                {"no": 3, "language": en,
                 "text_tashkeel": "When the clouds broke, light proved divisible."},
            ]
        )
    return {"lines": lines, "digest": digest[:12]}


def _mock_verify(req: LLMRequest) -> dict[str, Any]:
    """Echo the trailing lines-JSON payload back unchanged (passes F3)."""
    marker = '{"lines":'
    index = req.prompt.rfind(marker)
    if index == -1:
        return {"lines": []}
    payload = json.loads(req.prompt[index:])
    lines = [
        {"no": int(item["no"]),
         "text": str(item["text"]),
         "language": str(item.get("language", ""))}
        for item in payload.get("lines", [])
        if isinstance(item, dict)
    ]
    return {"lines": lines}


def _mock_shot_plan(req: LLMRequest, digest: str) -> dict[str, Any]:
    import re

    character = "نُور"
    bible_section = req.system.split("# SERIES_BIBLE", 1)[-1].split("# EPISODE", 1)[0]
    match = re.search(r"^- ([^:\n]{1,40}): ", bible_section, re.MULTILINE)
    if match:
        character = match.group(1).strip()
    return {
        "shots": [
            {"no": 1, "duration_s": 5, "characters": [character],
             "visual_prompt": "نور يقترِب من الفانوس في ضبابٍ ذهبيٍّ وفانوسٌ نحاسيٌّ يلوحُ في الأفق",
             "camera": "push-in بطيء"},
            {"no": 2, "duration_s": 6, "characters": [character, "العاصفة"],
             "visual_prompt": "العاصفة تتجمع في الأفق وتلتف حول برج الفانوس بومضاتٍ زرقاء",
             "camera": "سحبةٌ بطيئة للخلف"},
            {"no": 3, "duration_s": 5, "characters": [character],
             "visual_prompt": "انبلاج الفجر؛ نور يفتح باب الفانوس وينشر الضوء على الميناء",
             "camera": "لقطةٌ ثابتة حميمة"},
        ]
    }


def _mock_metadata(digest: str) -> dict[str, Any]:
    return {
        "title": "فانوسُ الفجرِ — الحلقةُ الأولى",
        "description": "حارسُ فانوسٍ يتعلم أن الضوءَ يُقتسمُ ولا يُخبأ حينَ تختبرُه العاصفة.",
        "tags": ["فانتازيا", "فانوس", "حكايةٌ عربية", "فصحى"],
        "safety": "clear",
        "issues": [],
        "digest": digest[:12],
    }


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
        elif req.schema_name == "bible_upsert":
            parsed = _mock_bible(digest)
            text = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        elif req.schema_name == "narration_script":
            parsed = _mock_script(req, digest)
            text = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        elif req.schema_name == "tashkeel_verify":
            parsed = _mock_verify(req)
            text = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        elif req.schema_name == "shot_plan":
            parsed = _mock_shot_plan(req, digest)
            text = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        elif req.schema_name == "metadata_safety":
            parsed = _mock_metadata(digest)
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
