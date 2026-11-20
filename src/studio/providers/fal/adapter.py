"""fal.ai queue adapter for image/video jobs (Phase 4 §F).

One implementation serves both kinds (``kind="image"`` / ``"video"``); the
registry ``MediaSpec`` drives request building, so mock and fal providers
share the same validation surface.

⚠ UNVERIFIED wire details (operator must confirm on fal.ai before spending):
  - submit ``POST {queue_base_url}/{endpoint_id}`` returning
    ``{request_id, status_url, response_url, cancel_url}``
  - poll ``GET status_url`` with ``status`` in
    ``IN_QUEUE | IN_PROGRESS | COMPLETED`` and an optional ``error`` field
  - fetch ``GET response_url`` returning JSON addressed by ``result_map``
    dotted paths (e.g. ``images.0.url``), with ``has_nsfw_concepts`` flags
  - auth header ``Authorization: Key <FAL_KEY>``
  - upload flow (initiate → PUT → file URL) for ``input_transport: upload``
  - the error taxonomy in :func:`map_http_error`

Secrets: the API key and any signed-URL query string never appear in
exception messages, logs, or persisted payloads. URLs are scrubbed of their
query component before being interpolated into messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from studio.config.registry import MediaSpec, ModelEntry
from studio.config.settings import Settings
from studio.domain.enums import ErrorClass
from studio.domain.errors import (
    ConfigError,
    ContentPolicyError,
    MediaConfigError,
    MediaJobFailedError,
    PermanentProviderError,
    ProviderQuotaExceeded,
    TransientProviderError,
    ZeroByteAssetError,
)
from studio.providers.base import JobHandle, JobState, MediaPayload, MediaRequest

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_BASE_URL = "https://queue.fal.run"
_CHUNK = 1024 * 256
_QUOTA_HINTS = ("balance", "locked", "exhausted", "quota", "credit")


def _scrub_url(url: str) -> str:
    """Drop the query string (signed URLs carry secrets) for messages/logs."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return "<url>"


_URL_QUERY_RE = re.compile(r"(https?://[^\s'<>]+)[?][^\s'<>]*")


class _QueryScrubFilter(logging.Filter):
    """Redact http(s) URL query strings in log records.

    httpx logs every request line at INFO including the full URL; fal's
    status/response/download URLs carry signed query strings, so without a
    filter those signatures land in any log sink (including caplog/CI logs).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        scrubbed = _URL_QUERY_RE.sub(lambda m: m.group(1) + "?<redacted>", message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


for _name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    if not any(isinstance(f, _QueryScrubFilter) for f in _logger.filters):
        _logger.addFilter(_QueryScrubFilter())


_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(access_token|token|api_key|key|sig|signature|expires)=[^&\s'<>]{4,}"
)


def _scrub_secrets(text: str) -> str:
    """Redact query strings of any http(s) URLs embedded in free text, plus
    bare secret assignments (``token=...``).

    The API key never enters messages by construction; this keeps a server
    error body that echoes a *signed* URL or a raw token from leaking it
    into exceptions, logs and persisted error messages.
    """
    text = _URL_QUERY_RE.sub(lambda match: match.group(1) + "?<redacted>", text)
    return _SECRET_PARAM_RE.sub(
        lambda match: match.group(0).split("=", 1)[0] + "=<redacted>", text
    )


def _detail_text(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace")) if body else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        detail = parsed.get("detail", parsed.get("error", ""))
        if isinstance(detail, str):
            return detail[:300]
        return json.dumps(detail, ensure_ascii=False)[:300]
    return body.decode("utf-8", errors="replace").strip()[:300] if body else ""


def _is_policy_422(body: bytes) -> bool:
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace")) if body else None
    except json.JSONDecodeError:
        return False
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    items = detail if isinstance(detail, list) else [detail]
    for item in items:
        if isinstance(item, dict) and item.get("type") == "content_policy_violation":
            return True
        if isinstance(item, str) and "content_policy_violation" in item:
            return True
    return False


def map_http_error(status_code: int, body: bytes) -> Exception:
    """§F error taxonomy. Messages carry status + detail, never secrets."""
    detail = _scrub_secrets(_detail_text(body))
    suffix = f"; {detail}" if detail else ""
    if status_code in (408, 429) or status_code >= 500:
        return TransientProviderError(f"fal {status_code}{suffix}")
    if status_code == 403 and any(hint in detail.lower() for hint in _QUOTA_HINTS):
        return ProviderQuotaExceeded(f"fal {status_code} provider quota{suffix}")
    if status_code in (401, 403):
        return PermanentProviderError(f"fal {status_code} auth{suffix}")
    if status_code == 422 and _is_policy_422(body):
        return ContentPolicyError(f"fal 422 content policy{suffix}", billed=False)
    if 400 <= status_code < 500:
        return PermanentProviderError(f"fal {status_code}{suffix}")
    return TransientProviderError(f"fal {status_code}{suffix}")


def _dig(payload: Any, dotted: str) -> Any:
    current = payload
    for part in dotted.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def _format_value(value: Any, fmt: str | None) -> Any:
    if fmt == "int":
        return int(value)
    if fmt == "str":
        return str(value)
    if fmt == "str_s":
        return f"{int(float(value))}s" if isinstance(value, (int, float)) else f"{value}s"
    return value


class FalJobProvider:
    """JobProvider over the fal queue for one registry entry."""

    def __init__(
        self,
        entry: ModelEntry,
        *,
        kind: Literal["image", "video"],
        settings: Settings | None = None,
        api_key: str | None = None,
        queue_base_url: str = DEFAULT_QUEUE_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        expected = "image_reference_generate" if kind == "image" else "image_to_video"
        if entry.capability != expected:
            raise ConfigError(
                f"fal adapter kind={kind!r} requires capability {expected!r}, "
                f"got {entry.capability!r}"
            )
        if entry.endpoint_id is None or "<CONFIRM" in entry.endpoint_id:
            raise MediaConfigError(
                f"fal model {entry.model_ref!r} has no confirmed endpoint_id; "
                "confirm it in model_registry.yaml before spending"
            )
        if entry.media is None:
            raise MediaConfigError(
                f"fal model {entry.model_ref!r} has no media wire contract"
            )
        self.entry = entry
        self.kind = kind
        self.spec: MediaSpec = entry.media
        self.endpoint_id = entry.endpoint_id
        self._api_key = api_key
        self._queue_base_url = queue_base_url.rstrip("/")
        self._transport = transport
        self._timeout_s = timeout_s
        self._settings = settings
        poll = settings.media.poll if settings is not None else None
        self.poll_interval_s = float(poll.initial_s) if poll is not None else 3.0
        self._download_retries = (
            int(settings.fal.download_retries) if settings is not None else 3
        )
        self._max_inline_bytes = (
            int(settings.media.max_inline_bytes) if settings is not None else 8_000_000
        )

    # ---- helpers ---------------------------------------------------------

    def _key(self) -> str:
        env_name = "FAL_KEY"
        if self._settings is not None:
            env_name = self._settings.fal.key_env
        key = self._api_key if self._api_key is not None else os.environ.get(env_name)
        if not key:
            raise ConfigError(f"{env_name} is not set (required for provider 'fal')")
        return key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout_s),
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self._key()}"}

    def build_body(self, req: MediaRequest) -> dict[str, Any]:
        """Registry-driven request body (§F). Pure; unit-tested."""
        spec = self.spec
        body: dict[str, Any] = {}
        # fixed_params are price-affecting and applied last (never overridden).
        body.update(spec.fixed_params)
        if req.aspect is not None:
            aspect_wire = spec.aspect_values.get(req.aspect.value, req.aspect.value)
            if "<CONFIRM" in aspect_wire:
                raise MediaConfigError(
                    f"fal model {self.entry.model_ref!r} aspect value for "
                    f"{req.aspect.value} is unconfirmed"
                )
            body[spec.param_map.get("aspect", "aspect_ratio")] = aspect_wire
            body.update(spec.aspect_extra_params.get(req.aspect.value, {}))
        for logical, value in req.params.items():
            if value is None:
                continue
            wire = spec.param_map.get(logical, logical)
            fmt = spec.param_formats.get(wire, spec.param_formats.get(logical))
            body[wire] = _format_value(value, fmt)
        if req.duration_s is not None:
            wire = spec.param_map.get("duration", "duration")
            fmt = spec.param_formats.get(wire, spec.param_formats.get("duration"))
            body[wire] = _format_value(req.duration_s, fmt)
        if req.seed is not None:
            wire = spec.param_map.get("seed", "seed")
            body[wire] = _format_value(req.seed, spec.param_formats.get(wire))
        # Re-apply fixed params so a request can never override pricing knobs.
        body.update(spec.fixed_params)
        for value in body.values():
            if isinstance(value, str) and value.startswith("data:"):
                if len(value.encode("utf-8")) > self._max_inline_bytes:
                    raise MediaConfigError(
                        f"inline data URI exceeds media.max_inline_bytes "
                        f"({self._max_inline_bytes} bytes)"
                    )
        return body

    def _fallback_urls(self, request_id: str) -> dict[str, str]:
        base = f"{self._queue_base_url}/{self.endpoint_id}/requests/{request_id}"
        return {
            "status_url": f"{base}/status",
            "response_url": f"{base}/response",
            "cancel_url": f"{base}/cancel",
        }

    async def _upload(self, client: httpx.AsyncClient, path: str) -> str:
        """⚠ unverified upload flow: POST bytes, expect a file URL back."""
        data = await asyncio.to_thread(Path(path).read_bytes)
        response = await client.post(
            f"{self._queue_base_url}/upload",
            headers=self._headers(),
            content=data,
            params={"file_name": Path(path).name},
        )
        if response.status_code != 200:
            raise map_http_error(response.status_code, response.content)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PermanentProviderError("fal upload returned invalid JSON") from exc
        url = payload.get("url") or payload.get("file_url") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url:
            raise PermanentProviderError("fal upload returned no file URL")
        return url

    async def _prepare_uploads(self, client: httpx.AsyncClient, body: dict[str, Any]) -> None:
        refs_wire = self.spec.param_map.get("refs", "image_urls")
        image_wire = self.spec.param_map.get("image", "image_url")
        for wire in (refs_wire, image_wire):
            value = body.get(wire)
            if isinstance(value, list):
                body[wire] = [
                    (
                        await self._upload(client, item)
                        if isinstance(item, str) and not item.startswith(("http", "data:"))
                        else item
                    )
                    for item in value
                ]
            elif isinstance(value, str) and not value.startswith(("http", "data:")):
                body[wire] = await self._upload(client, value)

    # ---- JobProvider -------------------------------------------------------

    async def submit(self, req: MediaRequest) -> JobHandle:
        body = self.build_body(req)
        url = f"{self._queue_base_url}/{self.endpoint_id}"
        try:
            async with self._client() as client:
                if self.spec.input_transport == "upload":
                    await self._prepare_uploads(client, body)
                response = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException as exc:
            raise TransientProviderError(
                f"fal submit timeout: {type(exc).__name__}"
            ) from None
        except httpx.TransportError as exc:
            raise TransientProviderError(
                f"fal submit connection error: {type(exc).__name__}"
            ) from None
        if response.status_code != 200:
            raise map_http_error(response.status_code, response.content)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PermanentProviderError("fal submit returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PermanentProviderError("fal submit returned a non-object payload")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise PermanentProviderError("fal submit returned no request_id")
        meta = {
            "status_url": payload.get("status_url"),
            "response_url": payload.get("response_url"),
            "cancel_url": payload.get("cancel_url"),
        }
        if not all(isinstance(v, str) and v for v in meta.values()):
            logger.warning(
                "fal submit for %s omitted queue URLs; constructing fallbacks",
                self.entry.model_ref,
            )
            meta.update(self._fallback_urls(request_id))
        return JobHandle(remote_request_id=request_id, meta=meta)

    async def poll(self, handle: JobHandle) -> JobState:
        status_url = handle.meta.get("status_url") if isinstance(handle.meta, dict) else None
        if not isinstance(status_url, str) or not status_url:
            status_url = self._fallback_urls(handle.remote_request_id)["status_url"]
        try:
            async with self._client() as client:
                response = await client.get(status_url, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise TransientProviderError(
                f"fal poll timeout: {type(exc).__name__}"
            ) from None
        except httpx.TransportError as exc:
            raise TransientProviderError(
                f"fal poll connection error: {type(exc).__name__}"
            ) from None
        if response.status_code != 200:
            raise map_http_error(response.status_code, response.content)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PermanentProviderError("fal poll returned invalid JSON") from exc
        status = payload.get("status") if isinstance(payload, dict) else None
        if status == "COMPLETED":
            error = payload.get("error") if isinstance(payload, dict) else None
            if error:
                raise MediaJobFailedError(
                    f"fal job failed: {_scrub_secrets(str(error))[:200]}"
                )
            return JobState(state="succeeded")
        if status == "IN_QUEUE":
            return JobState(state="queued")
        if status == "IN_PROGRESS":
            return JobState(state="running")
        return JobState(
            state="failed",
            error_class=ErrorClass.PERMANENT,
            message=f"fal poll returned unknown status {status!r}",
        )

    async def fetch(self, handle: JobHandle) -> list[MediaPayload]:
        response_url = handle.meta.get("response_url") if isinstance(handle.meta, dict) else None
        if not isinstance(response_url, str) or not response_url:
            response_url = self._fallback_urls(handle.remote_request_id)["response_url"]
        asset_url: str | None = None
        last_error: Exception | None = None
        # One re-GET for a fresh URL, then download retries against it.
        for attempt in range(self._download_retries + 1):
            try:
                async with self._client() as client:
                    response = await client.get(response_url, headers=self._headers())
                if response.status_code != 200:
                    raise map_http_error(response.status_code, response.content)
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise PermanentProviderError("fal fetch returned invalid JSON") from exc
                if isinstance(payload, dict):
                    nsfw_key = self.spec.result_map.get("nsfw_flags", "has_nsfw_concepts")
                    flags = _dig(payload, nsfw_key)
                    if isinstance(flags, list) and any(bool(f) for f in flags):
                        raise ContentPolicyError(
                            "fal result flagged nsfw; asset not stored", billed=True
                        )
                    if isinstance(flags, bool) and flags:
                        raise ContentPolicyError(
                            "fal result flagged nsfw; asset not stored", billed=True
                        )
                raw_url = _dig(payload, self.spec.result_map.get("asset_url", ""))
                if isinstance(raw_url, list):
                    raw_url = raw_url[0] if raw_url else None
                if not isinstance(raw_url, str) or not raw_url:
                    raise PermanentProviderError("fal fetch returned no asset URL")
                asset_url = raw_url
                return [await self._download(asset_url)]
            except (TransientProviderError, PermanentProviderError, ContentPolicyError) as exc:
                last_error = exc
                if isinstance(exc, (ContentPolicyError, PermanentProviderError)):
                    raise
                if attempt >= self._download_retries:
                    raise
                logger.warning("fal download attempt %d failed: %s", attempt + 1, exc)
        assert last_error is not None
        raise last_error

    async def _download(self, asset_url: str) -> MediaPayload:
        tmp_path: Path | None = None
        try:
            async with self._client() as client:
                async with client.stream("GET", asset_url) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise map_http_error(response.status_code, response.content)
                    content_type = response.headers.get("content-type", "")
                    with tempfile.NamedTemporaryFile(
                        prefix="studio-fal-", suffix=".bin", delete=False
                    ) as handle:
                        tmp_path = Path(handle.name)
                        total = 0
                        async for chunk in response.aiter_bytes(_CHUNK):
                            handle.write(chunk)
                            total += len(chunk)
                    if total == 0:
                        raise ZeroByteAssetError(
                            f"fal asset download was empty ({_scrub_url(asset_url)})"
                        )
                    ext, mime = _ext_mime(content_type, asset_url)
                    if self.kind == "image" and not mime.startswith("image/"):
                        raise PermanentProviderError(
                            f"fal asset content-type {content_type!r} is not image/*"
                        )
                    if self.kind == "video" and not mime.startswith("video/"):
                        raise PermanentProviderError(
                            f"fal asset content-type {content_type!r} is not video/*"
                        )
                    renamed = tmp_path.with_suffix(f".{ext}")
                    tmp_path.replace(renamed)
                    tmp_path = renamed
                    return MediaPayload(local_path=tmp_path, ext=ext, mime=mime)
        except httpx.TimeoutException as exc:
            raise TransientProviderError(f"fal download timeout: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise TransientProviderError(f"fal download error: {type(exc).__name__}") from None
        except Exception:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise


def _ext_mime(content_type: str, url: str) -> tuple[str, str]:
    mime = content_type.split(";")[0].strip().lower()
    suffix = Path(urlsplit(url).path).suffix.lower().lstrip(".")
    table = {
        "png": ("png", "image/png"),
        "jpg": ("jpg", "image/jpeg"),
        "jpeg": ("jpg", "image/jpeg"),
        "webp": ("webp", "image/webp"),
        "mp4": ("mp4", "video/mp4"),
        "webm": ("webm", "video/webm"),
        "mov": ("mov", "video/quicktime"),
    }
    if suffix in table:
        ext, default_mime = table[suffix]
        return ext, mime or default_mime
    if mime.startswith("image/"):
        return mime.split("/")[1].replace("jpeg", "jpg"), mime
    if mime.startswith("video/"):
        return mime.split("/")[1], mime
    return "bin", mime or "application/octet-stream"


def build_fal(
    entry: ModelEntry,
    *,
    kind: Literal["image", "video"],
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FalJobProvider:
    """Factory hook; keeps construction in one place for tests."""
    queue_base_url = settings.fal.queue_base_url if settings is not None else DEFAULT_QUEUE_BASE_URL
    timeout_s = settings.fal.request_timeout_s if settings is not None else 60.0
    return FalJobProvider(
        entry,
        kind=kind,
        settings=settings,
        queue_base_url=queue_base_url,
        transport=transport,
        timeout_s=float(timeout_s),
    )
