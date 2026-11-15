"""Shared machinery for mock providers.

Fault plan semantics (every mock exposes `calls` and `submit_count`):
- fail_transient_n: raise TransientProviderError for the next N primary ops
- fail_policy / fail_permanent: every primary op raises the typed error
- zero_byte: renders an empty artifact instead of a real one
- latency_s: sleep before the op (tests set 0)
- fail_on_nth_call: on the Nth primary op of THIS instance, raise
  PermanentProviderError
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.core.ids import Ids, UlidIds
from studio.domain.enums import Aspect, ErrorClass
from studio.domain.errors import (
    PermanentProviderError,
    PolicyRefusalError,
    TransientProviderError,
)
from studio.providers.base import JobHandle, JobState, MediaRequest


@dataclass
class MockFaultPlan:
    fail_transient_n: int = 0
    fail_policy: bool = False
    fail_permanent: bool = False
    zero_byte: bool = False
    latency_s: float = 0.0
    fail_on_nth_call: int | None = None


class MockBase:
    def __init__(
        self,
        fault_plan: MockFaultPlan | None = None,
        *,
        ids: Ids | None = None,
    ) -> None:
        self.fault_plan = fault_plan if fault_plan is not None else MockFaultPlan()
        self.calls: list[dict[str, Any]] = []
        self._op_count = 0
        self._transient_left = self.fault_plan.fail_transient_n
        self._ids: Ids = ids if ids is not None else UlidIds()

    @property
    def submit_count(self) -> int:
        return 0

    async def _tick(self) -> None:
        self._op_count += 1
        plan = self.fault_plan
        if plan.latency_s > 0:
            await asyncio.sleep(plan.latency_s)
        if plan.fail_on_nth_call is not None and self._op_count == plan.fail_on_nth_call:
            raise PermanentProviderError(f"mock: injected failure on call #{self._op_count}")
        if plan.fail_policy:
            raise PolicyRefusalError("mock: content policy refusal")
        if plan.fail_permanent:
            raise PermanentProviderError("mock: injected permanent failure")
        if self._transient_left > 0:
            self._transient_left -= 1
            raise TransientProviderError(
                f"mock: injected transient failure ({self._transient_left} left)"
            )

    def _record(self, op: str, **fields: Any) -> None:
        self.calls.append({"op": op, **fields})


class MockJobBase(MockBase):
    """Base for job-based mocks (image/video).

    The `submit_count` lives in a JSON ledger file on disk so a simulated
    process restart (new Gateway over the same ledger) sees the true count —
    that is what the crash-matrix tests assert against.
    """

    name: str = "mock.job"
    poll_ticks: int = 2

    def __init__(
        self,
        *,
        ledger_path: Path,
        fault_plan: MockFaultPlan | None = None,
        ids: Ids | None = None,
        poll_ticks: int | None = None,
    ) -> None:
        super().__init__(fault_plan, ids=ids)
        self.ledger_path = Path(ledger_path)
        if poll_ticks is not None:
            self.poll_ticks = poll_ticks

    @property
    def submit_count(self) -> int:
        return int(self._load_state().get("submit_count", 0))

    async def submit(self, req: MediaRequest) -> JobHandle:
        await self._tick()
        state = self._load_state()
        state["submit_count"] = int(state.get("submit_count", 0)) + 1
        remote_id = f"mock-{self._ids.new()}"
        jobs = state.setdefault("jobs", {})
        jobs[remote_id] = {
            "ticks": 0,
            "capability": req.capability,
            "aspect": _aspect_value(req.aspect),
            "duration_s": req.duration_s,
            "seed": req.seed,
        }
        self._save_state(state)
        self._record(
            "submit",
            capability=req.capability,
            aspect=_aspect_value(req.aspect),
            duration_s=req.duration_s,
            seed=req.seed,
        )
        return JobHandle(remote_request_id=remote_id, meta={"provider": self.name})

    async def poll(self, handle: JobHandle) -> JobState:
        state = self._load_state()
        job = state.get("jobs", {}).get(handle.remote_request_id)
        if job is None:
            return JobState(
                state="failed",
                error_class=ErrorClass.PERMANENT,
                message=f"unknown remote_request_id {handle.remote_request_id}",
            )
        job["ticks"] = int(job.get("ticks", 0)) + 1
        state_name = "succeeded" if job["ticks"] > self.poll_ticks else "running"
        self._save_state(state)
        self._record("poll", remote_request_id=handle.remote_request_id, state=state_name)
        if state_name == "succeeded":
            return JobState(state="succeeded")
        return JobState(state="running")

    def _render_dir(self) -> Path:
        directory = self.ledger_path.parent / "renders"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _load_state(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {"submit_count": 0, "jobs": {}}
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"submit_count": 0, "jobs": {}}
        if not isinstance(data, dict):
            return {"submit_count": 0, "jobs": {}}
        data.setdefault("submit_count", 0)
        data.setdefault("jobs", {})
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.ledger_path.name}.", suffix=".tmp", dir=str(self.ledger_path.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            os.replace(tmp, self.ledger_path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


def _aspect_value(aspect: Aspect | str | None) -> str | None:
    if aspect is None:
        return None
    return aspect.value if isinstance(aspect, Aspect) else str(aspect)


def aspect_dimensions(
    aspect: Aspect | str | None, *, width_16x9: int, height_16x9: int
) -> tuple[int, int]:
    """9:16 is the transposed 16:9 mock resolution."""
    if _aspect_value(aspect) == "9:16":
        return height_16x9, width_16x9
    return width_16x9, height_16x9


def worst_case_tokens(text: str, *, per_char: float = 0.5) -> int:
    return max(1, math.ceil(len(text) * per_char))
