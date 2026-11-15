"""Anthropic LLM adapter (PHASE2 §F5).

Hard rules:
- The `anthropic` SDK is imported lazily; a missing SDK raises
  ConfigError("LLM adapter 'anthropic' requires ai-studio[providers]").
- No streaming, no tool use, TLS verified, explicit 60s timeout,
  max_retries=0 — retries are the gateway/worker's job, never the
  adapter's.
- Errors are mapped to the typed provider hierarchy by status code;
  anything unrecognized propagates to the engine's generic-failure path
  (a single attempt — never a silent retry storm that spends money).
- API keys come from ANTHROPIC_API_KEY (or the constructor) and never
  appear in request bodies, logs, or persisted payloads.
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
_INSTALL_HINT = "LLM adapter 'anthropic' requires ai-studio[providers]"


def _load_sdk() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise ConfigError(_INSTALL_HINT) from exc
    return anthropic


def _map_status(status_code: int, message: str) -> ProviderError:
    if status_code in (400, 401, 403, 404, 413):
        return PermanentProviderError(f"anthropic {status_code}: {message}")
    if status_code == 429 or status_code >= 500:
        return TransientProviderError(f"anthropic {status_code}: {message}")
    if 400 <= status_code < 500:
        return PermanentProviderError(f"anthropic {status_code}: {message}")
    return TransientProviderError(f"anthropic error {status_code}: {message}")


def _translate_exception(exc: BaseException) -> ProviderError | None:
    """Map an SDK exception to a typed provider error, or None to propagate."""
    if isinstance(exc, ProviderError):
        return exc
    name = type(exc).__name__
    if name in ("APITimeoutError", "APIConnectionError"):
        return TransientProviderError(f"anthropic {name}")
    status = getattr(exc, "status_code", None)
    if status is not None:
        return _map_status(int(status), f"{name}: {exc}"[:300])
    return None


class AnthropicLLMProvider:
    """Thin async wrapper around anthropic.AsyncAnthropic.messages.create."""

    def __init__(
        self,
        entry: ModelEntry,
        *,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if entry.model_id is None:
            raise ConfigError("anthropic adapter requires model_id in the registry entry")
        self.model_id: str = entry.model_id
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        sdk = _load_sdk()
        key = self._api_key if self._api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set (required for provider 'anthropic')"
            )
        self._client = sdk.AsyncAnthropic(api_key=key, timeout=_TIMEOUT_S, max_retries=0)
        return self._client

    async def complete(self, req: LLMRequest) -> LLMResult:
        client = self._get_client()
        try:
            response = await client.messages.create(
                model=self.model_id,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                system=req.system,
                messages=[{"role": "user", "content": req.prompt}],
            )
        except Exception as exc:
            mapped = _translate_exception(exc)
            if mapped is None:
                raise
            raise mapped from exc
        return self._to_result(req, response)

    def _to_result(self, req: LLMRequest, response: Any) -> LLMResult:
        blocks = list(getattr(response, "content", None) or [])
        texts = [
            str(getattr(block, "text", ""))
            for block in blocks
            if getattr(block, "type", None) == "text"
        ]
        text = "".join(texts).strip()
        if not text:
            raise TransientProviderError("empty completion")
        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        raw_ref = f"{getattr(response, 'model', self.model_id)}:{getattr(response, 'id', '')}"
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
