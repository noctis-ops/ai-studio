"""Worker: claims jobs, runs them with a heartbeat, recovers stale work.

Concurrency is `worker.max_concurrency` (default 1) enforced by a semaphore.
`SimulatedCrash` is not caught — state is left exactly as a killed process
would leave it; `recovery` requeues on the next worker start.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress

from studio.config.loader import AppConfig
from studio.core.clock import Clock
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.domain.models import JobRow
from studio.pipeline.engine import Engine
from studio.pipeline.recovery import recover

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        db: Database,
        engine: Engine,
        repos: StudioRepos,
        config: AppConfig,
        clock: Clock,
        *,
        worker_id: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.db = db
        self.engine = engine
        self.repos = repos
        self.config = config
        self.clock = clock
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self._heartbeat_s = float(config.settings.worker.heartbeat_s)
        self._stale_after_s = float(config.settings.worker.stale_after_s)
        self._idle_poll_s = float(config.settings.worker.idle_poll_s)
        self._max_concurrency = int(config.settings.worker.max_concurrency)
        self._sem = asyncio.Semaphore(self._max_concurrency)
        self._sleep = sleep

    # ==================================================================

    async def run(
        self,
        *,
        until_idle: bool = False,
        max_jobs: int | None = None,
    ) -> int:
        """Run until `until_idle` and the queue is drained, or `max_jobs`
        jobs were executed. Returns the number of jobs executed."""
        recover(self.db, self.repos, self.clock, self._stale_after_s)
        completed = 0
        idle_polls = 0
        inflight: set[asyncio.Task[None]] = set()

        while True:
            if inflight and len(inflight) >= self._max_concurrency:
                done, inflight = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    completed += self._harvest(task)
                continue

            job = await asyncio.to_thread(self.repos.jobs.claim, self.worker_id)
            if job is None:
                if inflight:
                    done, inflight = await asyncio.wait(
                        inflight, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        completed += self._harvest(task)
                    continue
                if until_idle:
                    idle_polls += 1
                    if idle_polls >= 2:
                        return completed
                await self._sleep(self._idle_poll_s)
                if not until_idle and max_jobs is None:
                    # `run()` without bounds is the long-running daemon path;
                    # callers that want one pass use until_idle=True.
                    continue
                continue

            idle_polls = 0
            inflight.add(asyncio.create_task(self._run_job(job)))
            if max_jobs is not None and len(inflight) + completed >= max_jobs:
                done, inflight = await asyncio.wait(inflight, return_when=asyncio.ALL_COMPLETED)
                for task in done:
                    completed += self._harvest(task)
                return completed

    def _harvest(self, task: asyncio.Task[None]) -> int:
        exc = task.exception()
        if exc is not None:
            raise exc
        return 1

    async def _run_job(self, job: JobRow) -> None:
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(job.id, stop))
        try:
            async with self._sem:
                await self.engine.handle_job(job.id)
        finally:
            stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat_loop(self, job_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_s)
                return
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.repos.jobs.heartbeat, job_id, self.worker_id)
                except Exception:
                    logger.warning("heartbeat failed for job %s", job_id)
