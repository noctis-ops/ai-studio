"""Injectable UTC clock. All timestamps are UTC-aware and serialize as
`YYYY-MM-DDTHH:MM:SS.mmmZ` everywhere they are persisted."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def format_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    dt = dt.astimezone(UTC)
    return dt.strftime(TS_FORMAT)[:-3] + "Z"


def parse_ts(text: str) -> datetime:
    text = text.strip()
    if not text.endswith("Z"):
        raise ValueError(f"expected Zulu timestamp, got {text!r}")
    naive = datetime.strptime(text[:-1], TS_FORMAT)
    return naive.replace(tzinfo=UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...
    def now_iso(self) -> str: ...
    def month_tag(self) -> str: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_iso(self) -> str:
        return format_ts(self.now())

    def month_tag(self) -> str:
        return self.now().strftime("%Y-%m")

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Deterministic clock for tests; advances only when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def now_iso(self) -> str:
        return format_ts(self._now)

    def month_tag(self) -> str:
        return self._now.strftime("%Y-%m")

    def monotonic(self) -> float:
        return self._mono

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta
        self._mono += delta.total_seconds()

    def set(self, when: datetime) -> None:
        self._now = when


def fake_sleep(clock: FakeClock) -> Callable[[float], Awaitable[None]]:
    """Returns an `async sleep(seconds)` that advances the fake clock."""

    async def _sleep(seconds: float) -> None:
        if seconds > 0:
            clock.advance(timedelta(seconds=seconds))

    return _sleep


async def no_op_sleep(seconds: float) -> None:
    return None
