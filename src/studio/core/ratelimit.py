"""Per-provider concurrency limiter with minimum start spacing.

One instance per provider name, created lazily by the gateway from
`registry.limits` (falling back to settings defaults).
"""

from __future__ import annotations

import asyncio

from studio.core.clock import Clock
from studio.core.retry import SleepFn


class ProviderLimiter:
    def __init__(
        self,
        max_concurrency: int,
        min_interval_s: float,
        clock: Clock,
        sleep: SleepFn,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")
        self.max_concurrency = max_concurrency
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    async def __aenter__(self) -> ProviderLimiter:
        await self._sem.acquire()
        try:
            async with self._lock:
                now = self._clock.monotonic()
                if self._last_start is not None:
                    wait = self._last_start + self.min_interval_s - now
                    if wait > 0:
                        await self._sleep(wait)
                        now = self._clock.monotonic()
                self._last_start = now
        except Exception:
            self._sem.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self._sem.release()
