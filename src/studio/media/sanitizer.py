"""PromptSanitizer (Phase 4 §G).

Invoked ONLY for policy refusals (``ContentPolicyError``), never for transient
errors or QC failures (T-SAN-03). Two levels:

- Level 1: ``llm.light`` rewrites the scene text toward implied action.
- Level 2: deterministic lexicon substitution, then a stronger rewrite.

Rewrites are validated (non-empty, within ``max_scene_chars``, no new proper
nouns); a failure falls through to the next level. The LLM call goes through
the gateway as a single-call provider with nonce ``san{level}`` so the cache
never returns the same rewrite twice. After ``max_levels`` refusals the caller
parks the attempt as ``failed_policy``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from studio.core.gateway import CallContext
from studio.providers.base import LLMRequest

if TYPE_CHECKING:
    from studio.core.gateway import ProviderGateway

_WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)
_CAPITALIZED_RE = re.compile(r"\b[A-Z][a-zA-Z']+\b")


def load_lexicon(home: Path | None = None) -> dict[str, str]:
    if home is not None:
        path = Path(home) / "configs" / "prompts" / "media" / "sanitizer_lexicon.yaml"
    else:
        import os

        path = (
            Path(os.environ.get("STUDIO_HOME", ".")).resolve()
            / "configs"
            / "prompts"
            / "media"
            / "sanitizer_lexicon.yaml"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    lexicon = data.get("lexicon", {}) if isinstance(data, dict) else {}
    return {str(k).lower(): str(v) for k, v in lexicon.items()}


def apply_lexicon(text: str, lexicon: dict[str, str]) -> str:
    """Whole-word, case-insensitive substitution (deterministic)."""

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        return lexicon.get(word.lower(), word)

    return _WORD_RE.sub(replace, text)


def _proper_nouns(text: str) -> set[str]:
    return set(_CAPITALIZED_RE.findall(text))


def validate_rewrite(original: str, rewritten: str, *, max_chars: int) -> bool:
    """Non-empty, within cap, and no new proper nouns versus the original."""
    candidate = rewritten.strip()
    if not candidate:
        return False
    if len(candidate) > max_chars:
        return False
    if _proper_nouns(candidate) - _proper_nouns(original):
        return False
    return True


async def _llm_rewrite(
    *,
    gateway: ProviderGateway,
    model_key: str,
    scene_text: str,
    level: int,
    episode_id: str | None,
    stage_run_id: str | None,
    home: Path | None = None,
) -> str | None:
    try:
        from studio.media.prompts import load_templates

        instructions = load_templates(home)["sanitizer"].strip()
    except Exception:
        instructions = (
            "Rewrite the scene description to imply the action without "
            "describing it graphically. Return only the rewritten text."
        )
    prompt = f"{instructions}\n\nScene:\n{scene_text}"
    result = await gateway.llm(
        model_key,
        LLMRequest(
            system="You rewrite scene descriptions for a family animation studio.",
            prompt=prompt,
            max_tokens=512,
            temperature=0.2,
        ),
        CallContext(
            episode_id=episode_id,
            stage_run_id=stage_run_id,
            nonce=f"san{level}",
            purpose="prompt_sanitize",
        ),
    )
    text = (result.inline_json or {}).get("text") if isinstance(result.inline_json, dict) else None
    if not isinstance(text, str) or not text.strip():
        text = None
    return text


async def sanitize_scene_text(
    *,
    gateway: ProviderGateway,
    model_key: str,
    scene_text: str,
    level: int,
    episode_id: str | None,
    stage_run_id: str | None,
    max_chars: int,
    lexicon: dict[str, Any] | None = None,
    home: Path | None = None,
) -> str | None:
    """One sanitize level. Returns the rewritten scene text, or None when
    this level produced no valid rewrite (caller falls through / parks)."""
    if level == 1:
        rewritten = await _llm_rewrite(
            gateway=gateway,
            model_key=model_key,
            scene_text=scene_text,
            level=level,
            episode_id=episode_id,
            stage_run_id=stage_run_id,
            home=home,
        )
        if rewritten is not None and validate_rewrite(
            scene_text, rewritten, max_chars=max_chars
        ):
            return rewritten.strip()
        return None
    # Level 2+: lexicon first, then a stronger rewrite of the result.
    lexicon = lexicon if lexicon is not None else load_lexicon(home)
    lexed = apply_lexicon(scene_text, lexicon)
    rewritten = await _llm_rewrite(
        gateway=gateway,
        model_key=model_key,
        scene_text=lexed,
        level=level,
        episode_id=episode_id,
        stage_run_id=stage_run_id,
        home=home,
    )
    # An invalid LLM rewrite falls through to the deterministic lexicon;
    # each candidate is validated independently.
    for candidate in (rewritten, lexed):
        if candidate is None:
            continue
        if validate_rewrite(scene_text, candidate, max_chars=max_chars):
            return candidate.strip()
    return None
