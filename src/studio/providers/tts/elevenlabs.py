"""ElevenLabs TTS adapter (PHASE3 §H1).

Single-call `TTSProvider` behind the ProviderGateway. Hard rules:

- Raw `httpx.AsyncClient` with an **injectable transport**; tests use
  `httpx.MockTransport`. No SDK import anywhere in `src/` except the
  Phase 2 LLM adapters, and this is not one of them.
- The key is read from `ELEVENLABS_API_KEY` at call time and is never
  logged, stored, or placed in exception messages.
- Everything about the wire protocol below (endpoint, field names, headers,
  error mapping) is from the prompt author's memory and is UNVERIFIED
  against live ElevenLabs docs — see PHASE3_REPORT.md "Known limitations".

⚠ UNVERIFIED (do not rely on these without checking the current docs):
  - endpoint `POST {base_url}/v1/text-to-speech/{voice_id}` with
    `?output_format=` query parameter
  - request headers (`xi-api-key`), JSON body field names
    (`text`, `model_id`, `voice_settings` with `use_speaker_boost`,
    `previous_text`/`next_text`, `language_code`, `seed`)
  - response headers `character-cost` (billed chars) and `request-id`
  - error payload shape `{"detail": {"status": "...", "message": "..."}}`
    and the status vocabulary used for the mapping table
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from studio.config.registry import ModelEntry
from studio.domain.errors import (
    ConfigError,
    PermanentProviderError,
    ProviderQuotaExceeded,
    TransientProviderError,
    ZeroByteAssetError,
)
from studio.providers.base import MediaPayload, TTSRequest, TTSResult

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
_KEY_ENV = "ELEVENLABS_API_KEY"
_TIMEOUT_DEFAULT_S = 120.0
_CHUNK = 1024 * 256

# detail.status values treated as transient (provider busy, not the account).
_TRANSIENT_DETAIL_STATUS = frozenset({"too_many_concurrent_requests", "system_busy"})
# detail.status value that means the account is out of characters/credits.
_QUOTA_DETAIL_STATUS = "quota_exceeded"

# TTSRequest.settings key -> voice_settings field name (only fields present are sent).
_VOICE_SETTING_FIELDS = {
    "stability": "stability",
    "similarity_boost": "similarity_boost",
    "style": "style",
    "speaker_boost": "use_speaker_boost",
    "use_speaker_boost": "use_speaker_boost",
    "speed": "speed",
}


def _extract_detail_status(status_code: int, body: bytes) -> tuple[str | None, str]:
    """Robustly pull `detail.status` and a compact message out of the error body."""
    detail_status: str | None = None
    message = ""
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace")) if body else None
    except json.JSONDecodeError:
        parsed = None
    detail = None
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
    elif isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, dict):
            detail = first.get("detail")
    if isinstance(detail, dict):
        raw_status = detail.get("status")
        if isinstance(raw_status, str):
            detail_status = raw_status
        raw_message = detail.get("message")
        if isinstance(raw_message, str):
            message = raw_message
    elif isinstance(detail, str):
        message = detail
    if not message:
        message = body.decode("utf-8", errors="replace").strip()[:300] if body else ""
    return detail_status, message


class ElevenLabsTTSProvider:
    """Thin async wrapper over the ElevenLabs text-to-speech REST endpoint."""

    def __init__(
        self,
        entry: ModelEntry,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = _TIMEOUT_DEFAULT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if entry.capability != "tts":
            raise ConfigError(
                f"elevenlabs adapter only supports capability 'tts', got {entry.capability!r}"
            )
        if entry.model_id is None:
            raise ConfigError("elevenlabs adapter requires model_id in the registry entry")
        self.model_id: str = entry.model_id
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._transport = transport
        tts_params = entry.tts_params
        self._output_format = tts_params.output_format if tts_params else "mp3_44100_128"
        self._supports_context = bool(tts_params and tts_params.supports_context)
        self._supports_language_code = bool(tts_params and tts_params.supports_language_code)

    def _key(self) -> str:
        key = self._api_key if self._api_key is not None else os.environ.get(_KEY_ENV)
        if not key:
            raise ConfigError(f"{_KEY_ENV} is not set (required for provider 'elevenlabs')")
        return key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout_s),
        )

    def _voice_settings(self, req: TTSRequest) -> dict[str, Any]:
        settings: dict[str, Any] = {}
        for source, target in _VOICE_SETTING_FIELDS.items():
            value = req.settings.get(source)
            if value is None:
                continue
            settings[target] = value
        return settings

    def _body(self, req: TTSRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "text": req.text,
            "model_id": self.model_id,
        }
        voice_settings = self._voice_settings(req)
        if voice_settings:
            body["voice_settings"] = voice_settings
        seed = req.settings.get("seed")
        if seed is not None:
            try:
                body["seed"] = int(seed)
            except (TypeError, ValueError) as exc:
                raise PermanentProviderError(f"seed must be an integer, got {seed!r}") from exc
        if self._supports_context:
            if req.previous_text:
                body["previous_text"] = req.previous_text
            if req.next_text:
                body["next_text"] = req.next_text
        if self._supports_language_code:
            body["language_code"] = req.language.value
        return body

    @staticmethod
    def _map_error(status_code: int, detail_status: str | None, message: str) -> Exception:
        """§H1 error-mapping table. Message carries status + detail.status, never the key."""
        detail = detail_status or "unknown"
        suffix = f"detail.status={detail}" if detail_status else "detail.status=unknown"
        # quota beats the generic 429/5xx transient bucket: the account is out,
        # retrying cannot help; the engine halts the run instead of retrying.
        if detail_status == _QUOTA_DETAIL_STATUS:
            return ProviderQuotaExceeded(f"elevenlabs {status_code}: {suffix}; {message[:200]}")
        if detail_status in _TRANSIENT_DETAIL_STATUS:
            return TransientProviderError(f"elevenlabs {status_code}: {suffix}; {message[:200]}")
        if status_code == 429 or status_code >= 500:
            return TransientProviderError(f"elevenlabs {status_code}: {suffix}; {message[:200]}")
        if 400 <= status_code < 500:
            return PermanentProviderError(f"elevenlabs {status_code}: {suffix}; {message[:200]}")
        return TransientProviderError(f"elevenlabs {status_code}: {suffix}; {message[:200]}")

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        key = self._key()
        body = self._body(req)
        params = {"output_format": self._output_format}
        headers = {"xi-api-key": key}

        tmp_path: Path | None = None
        try:
            async with self._client() as client:
                try:
                    async with client.stream(
                        "POST",
                        f"/v1/text-to-speech/{req.voice_id}",
                        params=params,
                        json=body,
                        headers=headers,
                    ) as response:
                        response_headers = dict(response.headers)
                        if response.status_code != 200:
                            await response.aread()
                            detail_status, message = _extract_detail_status(
                                response.status_code, response.content
                            )
                            raise self._map_error(response.status_code, detail_status, message)
                        with tempfile.NamedTemporaryFile(
                            prefix="studio-elevenlabs-", suffix=".mp3", delete=False
                        ) as handle:
                            tmp_path = Path(handle.name)
                            total = 0
                            async for chunk in response.aiter_bytes(_CHUNK):
                                handle.write(chunk)
                                total += len(chunk)
                        if total == 0:
                            raise ZeroByteAssetError(
                                f"elevenlabs returned HTTP 200 with an empty body for voice "
                                f"{req.voice_id!r}; the provider may have billed {len(req.text)} chars"  # noqa: E501
                            )
                except httpx.TimeoutException as exc:
                    raise TransientProviderError(
                        f"elevenlabs timeout: {type(exc).__name__}"
                    ) from None
                except httpx.TransportError as exc:
                    raise TransientProviderError(
                        f"elevenlabs connection error: {type(exc).__name__}"
                    ) from None
        except Exception:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

        billed_chars = self._billed_chars(response_headers, req)
        request_id = response_headers.get("request-id") or response_headers.get("x-request-id")
        meta: dict[str, Any] = {}
        if isinstance(request_id, str) and request_id:
            meta["request_id"] = request_id
        meta["model_id"] = self.model_id
        return TTSResult(
            payload=MediaPayload(local_path=tmp_path, ext="mp3", mime="audio/mpeg"),
            billed_chars=billed_chars,
            meta=meta,
        )

    @staticmethod
    def _billed_chars(headers: dict[str, str], req: TTSRequest) -> int:
        raw = headers.get("character-cost") or headers.get("x-character-cost")
        if raw is not None:
            try:
                value = int(str(raw).strip())
            except ValueError:
                value = -1
            if value >= 0:
                return value
        return len(req.text)


def build_elevenlabs(
    entry: ModelEntry,
    *,
    timeout_s: float = _TIMEOUT_DEFAULT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ElevenLabsTTSProvider:
    """Factory hook; keeps the construction in one place for tests."""
    return ElevenLabsTTSProvider(entry, timeout_s=timeout_s, transport=transport)


__all__ = [
    "DEFAULT_BASE_URL",
    "ElevenLabsTTSProvider",
    "build_elevenlabs",
]

