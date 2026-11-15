"""Provider contracts.

Protocols + pydantic request/response models. LLM and TTS are single-call;
image/video are job-based (`submit → poll → fetch`) so the Gateway can persist
the remote request id before waiting (Amendment C1).

`redact` lives here so every persistence path shares one secret scrubber.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from studio.domain.enums import Aspect, ErrorClass, Language
from studio.domain.refs import AssetRef


class MediaPayload(BaseModel):
    """What a provider hands back; the Gateway downloads/copies immediately."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str | None = None
    local_path: Path | None = None
    ext: str
    mime: str


class JobHandle(BaseModel):
    remote_request_id: str
    meta: dict[str, Any] = {}


class JobState(BaseModel):
    state: Literal["queued", "running", "succeeded", "failed"]
    error_class: ErrorClass | None = None
    message: str | None = None


class LLMRequest(BaseModel):
    system: str = ""
    prompt: str
    schema_name: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.0
    prompt_version_sha: str | None = None


class LLMResult(BaseModel):
    text: str
    parsed_json: Any | None = None
    tokens_in: int = 0
    tokens_out: int = 0


class TTSRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    voice_id: str
    language: Language
    settings: dict[str, Any] = {}
    previous_text: str | None = None
    next_text: str | None = None


class TTSResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    payload: MediaPayload
    billed_chars: int


class MediaRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    capability: str
    params: dict[str, Any] = {}
    input_assets: list[AssetRef] = []
    aspect: Aspect | None = None
    duration_s: float | None = None
    seed: int | None = None
    prompt_version_sha: str | None = None


class LLMProvider(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResult: ...


class TTSProvider(Protocol):
    async def synthesize(self, req: TTSRequest) -> TTSResult: ...


class JobProvider(Protocol):
    async def submit(self, req: MediaRequest) -> JobHandle: ...

    async def poll(self, handle: JobHandle) -> JobState: ...

    async def fetch(self, handle: JobHandle) -> list[MediaPayload]: ...


# --------------------------------------------------------------------------
# Secret redaction for anything persisted (provider_calls.request_json et al.)
# --------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|authorization|credential|private[_-]?key)",
    re.IGNORECASE,
)
_ENV_SECRET_RE = re.compile(
    r"(_API_KEY$|_TOKEN$|_SECRET$|_PASSWORD$|^FAL_KEY$|^ANTHROPIC_API_KEY$|^OPENAI_API_KEY$|^ELEVENLABS_API_KEY$)",
    re.IGNORECASE,
)
_REDACTED = "***"


def _known_secret_values(extra: tuple[str, ...]) -> list[str]:
    values = [v for v in extra if isinstance(v, str) and len(v) >= 8]
    for name, value in os.environ.items():
        if _ENV_SECRET_RE.search(name) and isinstance(value, str) and len(value) >= 8:
            values.append(value)
    return values


def redact(obj: Any, *, extra_secret_values: tuple[str, ...] = ()) -> Any:
    """Deep-copy `obj`, scrubbing secret-looking keys and env secret values.

    Two passes, matching how leaks actually happen: keys whose name looks
    sensitive (drop the value entirely) and string values anywhere in the
    payload that equal a known secret (substring-scrubbed).
    """
    secrets = _known_secret_values(extra_secret_values)

    def scrub_string(text: str) -> str:
        out = text
        for secret in secrets:
            if secret in out:
                out = out.replace(secret, _REDACTED)
        return out

    def walk(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                key_str = str(raw_key)
                if _SECRET_KEY_RE.search(key_str):
                    out[key_str] = _REDACTED
                else:
                    out[key_str] = walk(raw_value, key_str)
            return out
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, tuple):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return scrub_string(value)
        return value

    return walk(obj)
