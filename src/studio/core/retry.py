"""Full-jitter exponential backoff. Only errors accepted by `classify` retry.

`sleep` and `rng` are injected so tests run instantly and deterministically.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
SleepFn = Callable[[float], Awaitable[None]]


async def with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    classify: Callable[[Exception], bool],
    max_attempts: int,
    base_s: float,
    cap_s: float,
    sleep: SleepFn,
    rng: random.Random,
) -> T:
    attempt = 0
    while True:
        attempt += 1
        try:
            return await op()
        except Exception as exc:
            if attempt >= max_attempts or not classify(exc):
                raise
            upper = min(cap_s, base_s * (2 ** (attempt - 1)))
            delay = rng.uniform(0.0, upper)
            await sleep(delay)
