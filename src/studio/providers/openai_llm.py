"""OpenAI LLM adapter (PHASE2 §F5). Mirror of anthropic_llm.py.

Hard rules identical to the Anthropic adapter: lazy SDK import, no
streaming, no tools, TLS verified, 60s timeout, max_retries=0 (the
gateway/worker own retries), error mapping by status code, API key from
OPENAI_API_KEY and never echoed into payloads. `expect_json` sets
response_format={"type": "json_object"}; invalid JSON from the model is a
PermanentProviderError carrying a 200-char excerpt (never any secrets).
"""

from __future__ import annotations

import json
import os
from typing import Any

from studio.config.registry import ModelEntry
from studio.domain.errors import (
    ConfigError,
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
)
from studio.providers.base import LLMRequest, LLMResult

_TIMEOUT_S = 60.0
_INSTALL_HINT = "LLM adapter 'openai' requires ai-studio[providers]"


def _load_sdk() -> Any:
    try:
        import openai
    except ImportError as exc:
        raise ConfigError(_INSTALL_HINT) from exc
    return openai


def _map_status(status_code: int, message: str) -> ProviderError:
    if status_code in (400, 401, 403, 404, 413):
        return PermanentProviderError(f"openai {status_code}: {message}")
    if status_code == 429 or status_code >= 500:
        return TransientProviderError(f"openai {status_code}: {message}")
    if 400 <= status_code < 500:
        return PermanentProviderError(f"openai {status_code}: {message}")
    return TransientProviderError(f"openai error {status_code}: {message}")


def _translate_exception(exc: BaseException) -> ProviderError | None:
    if isinstance(exc, ProviderError):
        return exc
    name = type(exc).__name__
    if name in ("APITimeoutError", "APIConnectionError"):
        return TransientProviderError(f"openai {name}")
    status = getattr(exc, "status_code", None)
    if status is not None:
        return _map_status(int(status), f"{name}: {exc}"[:300])
    return None


class OpenAILLMProvider:
    """Thin async wrapper around openai.AsyncOpenAI.chat.completions.create."""

    def __init__(
        self,
        entry: ModelEntry,
        *,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if entry.model_id is None:
            raise ConfigError("openai adapter requires model_id in the registry entry")
        self.model_id: str = entry.model_id
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        sdk = _load_sdk()
        key = self._api_key if self._api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ConfigError(
                "OPENAI_API_KEY is not set (required for provider 'openai')"
            )
        self._client = sdk.AsyncOpenAI(api_key=key, timeout=_TIMEOUT_S, max_retries=0)
        return self._client

    async def complete(self, req: LLMRequest) -> LLMResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.prompt},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.expect_json:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            mapped = _translate_exception(exc)
            if mapped is None:
                raise
            raise mapped from exc
        return self._to_result(req, response)

    def _to_result(self, req: LLMRequest, response: Any) -> LLMResult:
        choices = list(getattr(response, "choices", None) or [])
        content: str | None = None
        if choices:
            content = getattr(getattr(choices[0], "message", None), "content", None)
        text = (content or "").strip()
        if not text:
            raise TransientProviderError("empty completion")
        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        model = getattr(response, "model", self.model_id)
        raw_ref = f"{model}:{getattr(response, 'id', '')}"
        if not req.expect_json:
            return LLMResult(
                text=text,
                parsed_json=None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                raw_ref=raw_ref,
            )
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            excerpt = text[:200]
            raise PermanentProviderError(
                f"model returned invalid JSON ({exc}); excerpt: {excerpt!r}"
            ) from exc
        return LLMResult(
            text=text,
            parsed_json=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw_ref=raw_ref,
        )
