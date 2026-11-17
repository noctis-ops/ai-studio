"""Fault injection framework.

`SimulatedCrash` is a BaseException on purpose: it mimics a process dying at
an exact point, so it must never be caught by any `except Exception` path and
must propagate out of whatever code triggered it, leaving DB state exactly as
a killed process would leave it.

Production code calls `faults.crash_at("<point>")` at the seven defined
points; with a default (unarmed) injector that call is one dict lookup.
"""

from __future__ import annotations

CRASH_POINTS = (
    "after_reserve",
    "before_submit",
    "after_submit_before_persist",
    "after_persist_id",
    "during_poll",
    "after_fetch_before_store",
    "after_store_before_complete",
    # D2 (Phase 3): single-call providers (LLM, TTS) crash once the response has
    # been received but before it is persisted/stored.
    "during_single_call",
)

_FOREVER = 10**9


class SimulatedCrash(BaseException):
    """Raised when a crash point is armed; test-only."""


class FaultInjector:
    def __init__(self) -> None:
        self._armed: dict[str, int] = {}

    def arm(self, point: str, *, once: bool = True) -> None:
        self._check_point(point)
        self._armed[point] = 1 if once else _FOREVER

    def disarm(self, point: str | None = None) -> None:
        if point is None:
            self._armed.clear()
        else:
            self._armed.pop(point, None)

    def is_armed(self, point: str) -> bool:
        return self._armed.get(point, 0) > 0

    def crash_at(self, point: str) -> None:
        self._check_point(point)
        remaining = self._armed.get(point)
        if not remaining:
            return
        if remaining != _FOREVER:
            self._armed[point] = remaining - 1
            if self._armed[point] <= 0:
                self._armed.pop(point, None)
        raise SimulatedCrash(point)

    @staticmethod
    def _check_point(point: str) -> None:
        if point not in CRASH_POINTS:
            from studio.domain.errors import ConfigError

            raise ConfigError(f"unknown crash point {point!r}; known: {', '.join(CRASH_POINTS)}")


NOOP_FAULTS = FaultInjector()
