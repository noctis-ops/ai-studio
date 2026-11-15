"""ProviderGateway: the ONLY path through which paid provider calls flow.

Crash-safe lifecycle (BEGIN_PROMPT §H6), implemented exactly once here:

1. Compute cache_key. A cache hit (entry exists + all referenced assets live)
   returns a zero-cost result and never touches a provider — even with a kill
   switch tripped.
2. Adopt an in-flight call with the same cache_key instead of paying twice:
   - submitted with remote_request_id → skip straight to polling (never resubmit)
   - reserved without submit_started_at → reuse the reservation, submit below
   - reserved with submit_started_at and attempt >= 1 but no remote id → reuse + audit
     `potential_orphan`, then submit (documented duplicate risk); a marker with
     attempt == 0 is a crash between marker and first attempt — nothing was sent,
     so the reservation is reused with no orphan audit
3. Otherwise reserve atomically via LedgerService.begin_call, then `after_reserve`.
4. Acquire the per-provider limiter. Job providers persist submit_started_at
   (a single statement) and crash at `before_submit`; submit runs under retry
   inside the same call row (`attempt` bumps). `after_submit_before_persist`,
   then remote_request_id + status=submitted persist in ONE statement, then
   `after_persist_id`.
6. Poll. Transient poll errors retried; a failed state maps by error_class.
7. Fetch → `after_fetch_before_store` → store into CAS immediately
   (`after_store_before_complete` afterwards) → complete_call atomically.
8. ANY exception (typed or not) releases the reservation via fail_call, except
   ZeroByteAssetError which keeps estimate settled (provider likely billed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from studio.config.registry import ModelEntry, ModelRegistry
from studio.config.settings import Settings
from studio.core.budget import LedgerService
from studio.core.cache_key import make_cache_key
from studio.core.canonical import canonical_text
from studio.core.cas import ContentStore
from studio.core.clock import Clock
from studio.core.faults import NOOP_FAULTS, FaultInjector
from studio.core.ids import Ids, UlidIds
from studio.core.pricing import actual_cost_musd, estimate_cost_musd
from studio.core.ratelimit import ProviderLimiter
from studio.core.retry import SleepFn, with_retry
from studio.db.connection import Database
from studio.db.repos.assets import SqliteAssetsRepo
from studio.db.repos.providers import NewCacheEntry, NewCall, SqliteProvidersRepo
from studio.domain.enums import ErrorClass
from studio.domain.errors import (
    PermanentProviderError,
    PolicyRefusalError,
    ProviderError,
    RegistryError,
    TransientProviderError,
    ZeroByteAssetError,
)
from studio.domain.refs import AssetRef
from studio.providers.base import (
    JobHandle,
    LLMRequest,
    LLMResult,
    MediaRequest,
    TTSRequest,
    TTSResult,
    redact,
)
from studio.providers.factory import build_provider

logger = logging.getLogger(__name__)

Downloader = Callable[[str, Path], Awaitable[None]]

_EXPECTED_CAPABILITY = {
    "llm": "llm",
    "tts": "tts",
    "image": "image_reference_generate",
    "video": "image_to_video",
}
_ASSET_KIND = {"llm": "asset", "tts": "audio", "image": "image", "video": "video"}
CallKind = Literal["llm", "tts", "image", "video"]


@dataclass(frozen=True)
class CallContext:
    episode_id: str | None
    stage_run_id: str | None
    nonce: str = "0"
    purpose: str = ""


@dataclass(frozen=True)
class GatewayResult:
    assets: list[AssetRef] = field(default_factory=list)
    inline_json: dict[str, Any] | None = None
    cost_musd: int = 0
    cache_hit: bool = False
    provider_call_id: str | None = None


async def default_downloader(url: str, dest: Path) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120.0)) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 256):
                    handle.write(chunk)


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, TransientProviderError)


class ProviderGateway:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        registry: ModelRegistry,
        cas: ContentStore,
        ledger: LedgerService,
        clock: Clock,
        ids: Ids | None = None,
        faults: FaultInjector | None = None,
        sleep: SleepFn | None = None,
        rng: random.Random | None = None,
        downloader: Downloader | None = None,
        poll_interval_s: float = 0.5,
        max_polls: int = 1000,
        providers: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.registry = registry
        self.cas = cas
        self.ledger = ledger
        self.clock = clock
        self.ids: Ids = ids if ids is not None else UlidIds()
        self.faults = faults if faults is not None else NOOP_FAULTS
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 - jitter for backoff, not crypto
        self._downloader: Downloader = downloader if downloader is not None else default_downloader
        self._poll_interval_s = poll_interval_s
        self._max_polls = max_polls
        self._injected_providers = dict(providers or {})
        self._provider_cache: dict[str, Any] = {}
        self._limiters: dict[str, ProviderLimiter] = {}
        self._providers_repo = SqliteProvidersRepo(db, clock, self.ids)
        self._assets_repo = SqliteAssetsRepo(db, clock, self.ids)

    # =====================================================================
    # public API
    # =====================================================================

    async def llm(self, model_key: str, req: LLMRequest, ctx: CallContext) -> GatewayResult:
        return await self._run("llm", model_key, req, ctx)

    async def tts(self, model_key: str, req: TTSRequest, ctx: CallContext) -> GatewayResult:
        return await self._run("tts", model_key, req, ctx)

    async def image(self, model_key: str, req: MediaRequest, ctx: CallContext) -> GatewayResult:
        return await self._run("image", model_key, req, ctx)

    async def video(self, model_key: str, req: MediaRequest, ctx: CallContext) -> GatewayResult:
        return await self._run("video", model_key, req, ctx)

    # =====================================================================
    # lifecycle
    # =====================================================================

    async def _run(
        self,
        kind: CallKind,
        model_key: str,
        req: LLMRequest | TTSRequest | MediaRequest,
        ctx: CallContext,
    ) -> GatewayResult:
        entry = self._entry(model_key, _EXPECTED_CAPABILITY[kind])
        cache_key = self._cache_key(kind, model_key, entry, req, ctx)

        # -- step 1: cache hit? -------------------------------------------
        cached = await asyncio.to_thread(self._providers_repo.cache_get, cache_key)
        if cached is not None:
            hit = await asyncio.to_thread(self._cache_hit, cached)
            if hit is not None:
                return hit

        # -- step 2: adopt in-flight work ----------------------------------
        est = estimate_cost_musd(
            entry,
            req,
            tokens_per_char_worst_case=self.settings.pricing.llm_tokens_per_char_worst_case,
        )
        inflight = await asyncio.to_thread(self._providers_repo.find_inflight, cache_key)
        submitted = next(
            (c for c in inflight if c.status == "submitted" and c.remote_request_id), None
        )
        if submitted is not None:
            return await self._pay_once(
                kind,
                model_key,
                entry,
                req,
                ctx,
                call_id=submitted.id,
                est=submitted.est_cost_musd,
                remote_request_id=submitted.remote_request_id,
                already_submitted=True,
            )

        reserved = next((c for c in inflight if c.status == "reserved"), None)
        if reserved is not None:
            if reserved.submit_started_at is not None and reserved.attempt >= 1:
                # The submit marker was written and at least one provider op ran,
                # but no remote id was persisted: the provider may have accepted
                # the work. submit_started_at with attempt == 0 instead means we
                # crashed between the marker and the first attempt — nothing was
                # sent, so no orphan is possible.
                await asyncio.to_thread(
                    self.ledger.audit.append,
                    actor="gateway",
                    entity=f"provider_call:{reserved.id}",
                    event="potential_orphan",
                    payload={
                        "call_id": reserved.id,
                        "provider": entry.provider,
                        "model_key": model_key,
                        "cache_key": cache_key,
                        "reason": "reserved call has submit_started_at but no remote_request_id",
                    },
                )
            call_id = reserved.id
            est = reserved.est_cost_musd
        else:
            # -- step 3: reserve atomically ----------------------------------
            call = NewCall(
                episode_id=ctx.episode_id,
                stage_run_id=ctx.stage_run_id,
                capability=entry.capability,
                model_key=model_key,
                provider=entry.provider,
                purpose=ctx.purpose,
                cache_key=cache_key,
                request_json=canonical_text(redact(self._request_dict(kind, req))),
                est_cost_musd=est,
            )
            inserted = await asyncio.to_thread(self.ledger.begin_call, call)
            call_id = inserted.id
            est = inserted.est_cost_musd
            self.faults.crash_at("after_reserve")

        return await self._pay_once(
            kind,
            model_key,
            entry,
            req,
            ctx,
            call_id=call_id,
            est=est,
            remote_request_id=None,
            already_submitted=False,
        )

    async def _pay_once(
        self,
        kind: CallKind,
        model_key: str,
        entry: ModelEntry,
        req: LLMRequest | TTSRequest | MediaRequest,
        ctx: CallContext,
        *,
        call_id: str,
        est: int,
        remote_request_id: str | None,
        already_submitted: bool,
    ) -> GatewayResult:
        """Steps 4-8. Any exception here releases the reservation via fail_call."""
        provider = await asyncio.to_thread(self._provider_for, model_key, entry)
        job_based = kind in ("image", "video")
        try:
            if job_based:
                if already_submitted:
                    handle = JobHandle(remote_request_id=str(remote_request_id))
                else:
                    limiter = self._limiter(entry.provider)
                    async with limiter:
                        handle = await self._submit_job(call_id, kind, provider, req)
                await self._poll_until_done(provider, handle)
                refs = await self._fetch_and_store(kind, provider, handle)
                actual = actual_cost_musd(entry, req, None)
                cache_rows = self._cache_rows(
                    call_id, entry, req, refs, inline_json=None, actual=actual
                )
                await asyncio.to_thread(self.ledger.complete_call, call_id, actual, cache_rows)
                return GatewayResult(
                    assets=refs, inline_json=None, cost_musd=actual, provider_call_id=call_id
                )

            limiter = self._limiter(entry.provider)
            async with limiter:
                result = await self._submit_single(call_id, kind, provider, req)
            return await self._finish_single(kind, call_id, est, entry, req, result)
        except Exception as exc:
            await asyncio.to_thread(self._fail_from_exception, call_id, est, exc)
            raise

    # =====================================================================
    # steps 4-7 internals
    # =====================================================================

    async def _submit_job(
        self,
        call_id: str,
        kind: CallKind,
        provider: Any,
        req: LLMRequest | TTSRequest | MediaRequest,
    ) -> JobHandle:
        assert isinstance(req, MediaRequest)
        await asyncio.to_thread(self._providers_repo.mark_submit_started, call_id)
        self.faults.crash_at("before_submit")

        async def op() -> JobHandle:
            await asyncio.to_thread(self._providers_repo.bump_attempt, call_id)
            handle: JobHandle = await provider.submit(req)
            return handle

        handle: JobHandle = await with_retry(
            op,
            classify=_is_transient,
            max_attempts=self.settings.retry.transient_max,
            base_s=self.settings.retry.backoff_base_s,
            cap_s=self.settings.retry.backoff_cap_s,
            sleep=self._sleep,
            rng=self._rng,
        )
        self.faults.crash_at("after_submit_before_persist")
        # Persist remote id + submitted status in ONE statement.
        await asyncio.to_thread(
            self._providers_repo.mark_submitted, call_id, handle.remote_request_id
        )
        self.faults.crash_at("after_persist_id")
        return handle

    async def _submit_single(
        self,
        call_id: str,
        kind: CallKind,
        provider: Any,
        req: LLMRequest | TTSRequest | MediaRequest,
    ) -> LLMResult | TTSResult:
        async def op() -> LLMResult | TTSResult:
            await asyncio.to_thread(self._providers_repo.bump_attempt, call_id)
            if kind == "llm":
                assert isinstance(req, LLMRequest)
                completed: LLMResult = await provider.complete(req)
                return completed
            assert isinstance(req, TTSRequest)
            synthesized: TTSResult = await provider.synthesize(req)
            return synthesized

        result: LLMResult | TTSResult = await with_retry(
            op,
            classify=_is_transient,
            max_attempts=self.settings.retry.transient_max,
            base_s=self.settings.retry.backoff_base_s,
            cap_s=self.settings.retry.backoff_cap_s,
            sleep=self._sleep,
            rng=self._rng,
        )
        return result

    async def _poll_until_done(self, provider: Any, handle: JobHandle) -> None:
        first = True
        for _ in range(self._max_polls):
            if first:
                # Crash after all persistence but before the first poll.
                self.faults.crash_at("during_poll")
                first = False

            async def op() -> Any:
                return await provider.poll(handle)

            state = await with_retry(
                op,
                classify=_is_transient,
                max_attempts=self.settings.retry.transient_max,
                base_s=self.settings.retry.backoff_base_s,
                cap_s=self.settings.retry.backoff_cap_s,
                sleep=self._sleep,
                rng=self._rng,
            )
            if state.state == "succeeded":
                return
            if state.state == "failed":
                error_class = state.error_class or ErrorClass.PERMANENT
                message = state.message or "provider job failed"
                mapping = {
                    ErrorClass.TRANSIENT: TransientProviderError,
                    ErrorClass.POLICY: PolicyRefusalError,
                    ErrorClass.PERMANENT: PermanentProviderError,
                }
                raise mapping[error_class](message)
            await self._sleep(self._poll_interval_s)
        raise PermanentProviderError(
            f"provider job {handle.remote_request_id} did not finish after {self._max_polls} polls"
        )

    async def _fetch_and_store(
        self,
        kind: CallKind,
        provider: Any,
        handle: JobHandle,
    ) -> list[AssetRef]:
        payloads = await provider.fetch(handle)
        self.faults.crash_at("after_fetch_before_store")
        refs: list[AssetRef] = []
        for payload in payloads:
            local_path, tmp_download = await self._materialize_payload(payload)
            try:
                ref = await asyncio.to_thread(
                    self.cas.put_file,
                    local_path,
                    kind=_ASSET_KIND[kind],
                    ext=payload.ext,
                    move=True,
                )
            finally:
                if tmp_download is not None:
                    tmp_download.unlink(missing_ok=True)
            refs.append(ref)
        self.faults.crash_at("after_store_before_complete")
        return refs

    async def _finish_single(
        self,
        kind: CallKind,
        call_id: str,
        est: int,
        entry: ModelEntry,
        req: LLMRequest | TTSRequest | MediaRequest,
        result: LLMResult | TTSResult,
    ) -> GatewayResult:
        refs: list[AssetRef] = []
        inline_json: dict[str, Any] | None = None
        if kind == "llm":
            assert isinstance(result, LLMResult)
            inline_json = (
                result.parsed_json
                if isinstance(result.parsed_json, dict)
                else {"text": result.text}
            )
        elif kind == "tts":
            assert isinstance(result, TTSResult)
            payload = result.payload
            local_path, tmp_download = await self._materialize_payload(payload)
            try:
                refs = [
                    await asyncio.to_thread(
                        self.cas.put_file, local_path, kind="audio", ext=payload.ext, move=True
                    )
                ]
            finally:
                if tmp_download is not None:
                    tmp_download.unlink(missing_ok=True)
        actual = actual_cost_musd(entry, req, result)
        cache_rows = self._cache_rows(
            call_id, entry, req, refs, inline_json=inline_json, actual=actual
        )
        await asyncio.to_thread(self.ledger.complete_call, call_id, actual, cache_rows)
        return GatewayResult(
            assets=refs, inline_json=inline_json, cost_musd=actual, provider_call_id=call_id
        )

    async def _materialize_payload(self, payload: Any) -> tuple[Path, Path | None]:
        if payload.local_path is not None:
            return Path(payload.local_path), None
        if payload.url is None:
            raise PermanentProviderError("provider payload has neither url nor local_path")
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="studio-dl-", suffix=f".{_clean_ext(payload.ext)}", delete=False
            ) as handle:
                tmp = Path(handle.name)
            await self._downloader(payload.url, tmp)
        except Exception as exc:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            # The job is already paid; a download hiccup must stay retryable.
            raise TransientProviderError(f"download failed: {exc}") from exc
        return tmp, tmp

    def _fail_from_exception(self, call_id: str, est: int, exc: BaseException) -> None:
        settle = est if isinstance(exc, ZeroByteAssetError) else 0
        error_class = (
            exc.error_class.value if isinstance(exc, ProviderError) else ErrorClass.PERMANENT.value
        )
        try:
            self.ledger.fail_call(call_id, error_class, str(exc)[:1000], settle_musd=settle)
        except Exception:
            # fail_call must never mask the original provider failure.
            logger.warning("could not persist fail_call for %s", call_id, exc_info=True)

    # =====================================================================
    # helpers
    # =====================================================================

    def _entry(self, model_key: str, expected_capability: str) -> ModelEntry:
        entry = self.registry.models.get(model_key)
        if entry is None:
            raise RegistryError(f"unknown model_key {model_key!r} in registry")
        if entry.capability != expected_capability:
            raise RegistryError(
                f"model_key {model_key!r} has capability {entry.capability!r}, "
                f"expected {expected_capability!r}"
            )
        return entry

    def _provider_for(self, model_key: str, entry: ModelEntry) -> Any:
        cached = self._provider_cache.get(model_key)
        if cached is not None:
            return cached
        injected = self._injected_providers.get(model_key)
        if injected is not None:
            self._provider_cache[model_key] = injected
            return injected
        instance = build_provider(entry, settings=self.settings, ids=self.ids)
        self._provider_cache[model_key] = instance
        return instance

    def _limiter(self, provider_name: str) -> ProviderLimiter:
        limiter = self._limiters.get(provider_name)
        if limiter is None:
            cfg = self.registry.limits.get(provider_name)
            max_concurrency = (
                cfg.max_concurrency
                if cfg is not None
                else self.settings.worker.per_provider_concurrency
            )
            min_interval = cfg.min_interval_s if cfg is not None else 0.0
            limiter = ProviderLimiter(max_concurrency, min_interval, self.clock, self._sleep)
            self._limiters[provider_name] = limiter
        return limiter

    def _cache_key(
        self,
        kind: CallKind,
        model_key: str,
        entry: ModelEntry,
        req: LLMRequest | TTSRequest | MediaRequest,
        ctx: CallContext,
    ) -> str:
        params, input_assets, prompt_version_sha, seed = self._key_parts(kind, req)
        return make_cache_key(
            capability=entry.capability,
            model_key=model_key,
            model_ref=entry.model_ref,
            params=params,
            input_asset_sha256s=input_assets,
            prompt_version_sha=prompt_version_sha,
            seed=seed,
            nonce=ctx.nonce,
        )

    @staticmethod
    def _key_parts(
        kind: CallKind,
        req: LLMRequest | TTSRequest | MediaRequest,
    ) -> tuple[dict[str, Any], list[str], str | None, int | None]:
        if kind == "llm":
            assert isinstance(req, LLMRequest)
            return (
                {
                    "system": req.system,
                    "prompt": req.prompt,
                    "schema_name": req.schema_name,
                    "max_tokens": req.max_tokens,
                    "temperature": req.temperature,
                },
                [],
                req.prompt_version_sha,
                None,
            )
        if kind == "tts":
            assert isinstance(req, TTSRequest)
            return (
                {
                    "text": req.text,
                    "voice_id": req.voice_id,
                    "language": req.language.value,
                    "settings": req.settings,
                    "previous_text": req.previous_text,
                    "next_text": req.next_text,
                },
                [],
                None,
                None,
            )
        assert isinstance(req, MediaRequest)
        return (
            {
                "capability": req.capability,
                "params": req.params,
                "aspect": req.aspect.value if req.aspect is not None else None,
                "duration_s": req.duration_s,
            },
            [asset.sha256 for asset in req.input_assets],
            req.prompt_version_sha,
            req.seed,
        )

    @staticmethod
    def _request_dict(
        kind: CallKind,
        req: LLMRequest | TTSRequest | MediaRequest,
    ) -> dict[str, Any]:
        if kind == "llm":
            assert isinstance(req, LLMRequest)
            return req.model_dump(mode="json")
        if kind == "tts":
            assert isinstance(req, TTSRequest)
            return {
                "text": req.text,
                "voice_id": req.voice_id,
                "language": req.language.value,
                "settings": req.settings,
                "previous_text": req.previous_text,
                "next_text": req.next_text,
            }
        assert isinstance(req, MediaRequest)
        return {
            "capability": req.capability,
            "params": req.params,
            "input_assets": [asset.sha256 for asset in req.input_assets],
            "aspect": req.aspect.value if req.aspect is not None else None,
            "duration_s": req.duration_s,
            "seed": req.seed,
        }

    def _cache_hit(self, cached: Any) -> GatewayResult | None:
        try:
            shas = json.loads(cached.assets_json)
            inline = json.loads(cached.inline_json) if cached.inline_json else None
        except json.JSONDecodeError:
            return None
        refs: list[AssetRef] = []
        for sha in shas:
            if not self.cas.exists_live(sha):
                return None
            row = self._assets_repo.get(sha)
            if row is not None:
                refs.append(
                    AssetRef(
                        sha256=row.sha256,
                        rel_path=row.rel_path,
                        kind=row.kind,
                        mime=row.mime,
                        size_bytes=row.size_bytes,
                        duration_ms=row.duration_ms,
                        width=row.width,
                        height=row.height,
                    )
                )
        return GatewayResult(assets=refs, inline_json=inline, cost_musd=0, cache_hit=True)

    def _cache_rows(
        self,
        call_id: str,
        entry: ModelEntry,
        req: LLMRequest | TTSRequest | MediaRequest,
        refs: list[AssetRef],
        *,
        inline_json: dict[str, Any] | None,
        actual: int,
    ) -> list[NewCacheEntry]:
        call = self._providers_repo.get_call(call_id)
        return [
            NewCacheEntry(
                cache_key=call.cache_key if call else "",
                capability=entry.capability,
                model_key=call.model_key if call else "",
                assets_json=json.dumps([ref.sha256 for ref in refs], separators=(",", ":")),
                inline_json=(
                    json.dumps(inline_json, ensure_ascii=False, separators=(",", ":"))
                    if inline_json is not None
                    else None
                ),
                cost_musd=actual,
            )
        ]


def _clean_ext(ext: str) -> str:
    ext = ext.strip().lower().lstrip(".")
    return ext if ext.isalnum() and len(ext) <= 10 else "bin"
