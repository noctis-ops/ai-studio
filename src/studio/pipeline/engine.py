"""Pipeline engine: stage barriers, subject enumeration, gates, invalidation,
crash-safe job handling.

Selection model: all work is persisted (pipeline_runs / stage_runs / jobs).
A worker claims jobs and calls `Engine.handle_job`. The engine is
persistence-first and idempotent everywhere.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from studio.config.languages import resolve_languages
from studio.config.loader import AppConfig
from studio.core.budget import LedgerService
from studio.core.canonical import canonical_json, canonical_text
from studio.core.cas import ContentStore
from studio.core.clock import Clock, format_ts
from studio.core.faults import SimulatedCrash
from studio.core.ids import Ids, UlidIds
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.db.repos.pipeline import subject_from_stage_run
from studio.domain.enums import RunStatus
from studio.domain.errors import (
    BudgetExceeded,
    ProviderQuotaExceeded,
    StudioError,
    TransientProviderError,
)
from studio.domain.models import JobRow, PipelineRunRow, StageRunRow
from studio.domain.refs import SubjectRef, subjects_related
from studio.pipeline.dag import Dag
from studio.pipeline.gates import GateService
from studio.pipeline.stage import StageOutput, StageSpec

if TYPE_CHECKING:
    from studio.core.gateway import ProviderGateway
    from studio.pipeline.context import RunContext

logger = logging.getLogger(__name__)

_TERMINAL_STAGE_STATES = ("succeeded", "failed")
_STAGE_LIVE = ("pending", "running", "stale")


class Engine:
    def __init__(
        self,
        *,
        db: Database,
        repos: StudioRepos,
        dag: Dag,
        clock: Clock,
        config: AppConfig,
        ids: Ids | None = None,
        gates: GateService | None = None,
        gateway: ProviderGateway | None = None,
        worker_id: str = "worker",
        llm_subset_only: bool = False,
    ) -> None:
        self.db = db
        self.repos = repos
        self.dag = dag
        self.clock = clock
        self.config = config
        self.ids: Ids = ids if ids is not None else UlidIds()
        self.gates = gates or GateService(db, repos, clock, dag, self.ids)
        self._gateway = gateway
        self.worker_id = worker_id
        # Text-pipeline (S1-S4) runs resolve only the LLM roles: the media
        # registry entries are placeholders until Phase 4.
        self.llm_subset_only = llm_subset_only

    # ==================================================================
    # context / gateway
    # ==================================================================

    @property
    def gateway(self) -> ProviderGateway:
        if self._gateway is None:
            from studio.core.gateway import ProviderGateway

            settings = self.config.settings
            self._gateway = ProviderGateway(
                db=self.db,
                settings=settings,
                registry=self.config.registry,
                cas=ContentStore(Path(settings.paths.cas), self.db, self.clock),
                ledger=LedgerService(self.db, self.clock, settings, self.ids),
                clock=self.clock,
                ids=self.ids,
                poll_interval_s=0.05,
                max_polls=1000,
            )
        return self._gateway

    def _context(self, run: PipelineRunRow, stage_run_id: str | None) -> RunContext:
        from studio.pipeline.context import RunContext

        settings = self.config.settings
        if self.llm_subset_only:
            resolved = self.config.registry.resolve_llm_subset(run.profile)
        else:
            resolved = self.config.registry.resolve_profile(run.profile)
        # Per-episode overrides (the `studio run --languages/--aspects` selection
        # is persisted on the episode at seed time) win over the settings
        # defaults; fall back to settings for series-setup runs.
        languages = resolve_languages(settings)
        aspects = list(settings.aspects)
        if run.episode_id:
            episode = self.repos.content.get_episode(run.episode_id)
            if episode is not None:
                try:
                    episode_languages = json.loads(episode.languages_json)
                except (TypeError, json.JSONDecodeError):
                    episode_languages = None
                if isinstance(episode_languages, list) and episode_languages:
                    languages = resolve_languages(settings, episode_languages)
                try:
                    episode_aspects = json.loads(episode.aspects_json)
                except (TypeError, json.JSONDecodeError):
                    episode_aspects = None
                if isinstance(episode_aspects, list) and episode_aspects:
                    from studio.domain.enums import Aspect

                    seen: set[str] = set()
                    resolved_aspects = []
                    for item in episode_aspects:
                        value = str(item)
                        if value in seen:
                            continue
                        seen.add(value)
                        resolved_aspects.append(Aspect(value))
                    aspects = resolved_aspects
        return RunContext(
            db=self.db,
            repos=self.repos,
            gateway=self.gateway,
            clock=self.clock,
            ids=self.ids,
            config=self.config,
            resolved_profile=resolved,
            run_id=run.id,
            episode_id=run.episode_id,
            languages=languages,
            aspects=aspects,
            stage_run_id=stage_run_id,
        )

    # ==================================================================
    # advance_run: eligibility, enumeration, gates, completion
    # ==================================================================

    def advance_run(self, run_id: str) -> None:
        run = self.repos.pipeline.get_run(run_id)
        if run is None:
            logger.warning("advance_run: unknown run %s", run_id)
            return
        if run.status not in (RunStatus.RUNNING.value, RunStatus.AWAITING_APPROVAL.value):
            return

        # A pending gate pauses the whole run: everything downstream of a
        # gated stage waits for human approval (BEGIN_PROMPT §H7: "enqueue
        # nothing"). Approval-enqueued advance_run jobs proceed again.
        if self.repos.gates.list_pending(run_id):
            if run.status != RunStatus.AWAITING_APPROVAL.value:
                self.repos.pipeline.set_run_status(run_id, RunStatus.AWAITING_APPROVAL.value)
            return

        # Rule 4: recompute hashes for succeeded rows; cascade staleness.
        self._invalidate_stale(run)
        # Rows staled externally (GateService.request_changes) also re-execute.
        self._requeue_stale(run_id)

        # Enumerate in DAG order. When a gated stage is complete and no valid
        # gate exists yet, create it and STOP: downstream stages wait for the
        # human decision (BEGIN_PROMPT §H7: "enqueue nothing").
        for spec in self.dag.order:
            if not self._deps_complete(run_id, spec):
                continue
            self._enumerate_stage(run_id, spec)
            if self._maybe_gate_stage(run_id, spec):
                self._maybe_complete(run_id)
                return

        self._maybe_complete(run_id)

    def resume_run(self, run_id: str) -> None:
        """`studio resume`: clear halted_budget/failed, re-enqueue work."""
        run = self.repos.pipeline.get_run(run_id)
        if run is None:
            raise StudioError(f"unknown run {run_id}")
        if run.status not in (RunStatus.HALTED_BUDGET.value, RunStatus.FAILED.value):
            raise StudioError(
                f"run {run_id} is {run.status}; only halted_budget or failed runs can resume"
            )
        for row in self.repos.pipeline.list_stage_runs(run_id):
            if row.status == "failed":
                self.repos.pipeline.set_stage_run(row.id, status="pending")
        self.repos.pipeline.set_run_status(run_id, RunStatus.RUNNING.value)
        if run.episode_id:
            self.repos.content.set_episode_status(run.episode_id, RunStatus.RUNNING.value)
        self.repos.audit.append(
            actor="operator",
            entity=f"run:{run_id}",
            event="run_resumed",
            payload={"run_id": run_id, "previous_status": run.status},
        )
        self.repos.jobs.enqueue(
            kind="advance_run", payload={"run_id": run_id}, dedupe_key=f"advance:{run_id}"
        )

    # ---- eligibility + enumeration ---------------------------------------

    def _enumerate_stage(self, run_id: str, spec: StageSpec) -> bool:
        if not self._deps_complete(run_id, spec):
            return False
        if self.repos.pipeline.is_enumerated(run_id, spec.name):
            self._enqueue_pending_for_stage(run_id, spec)
            return False
        run = self.repos.pipeline.get_run(run_id)
        assert run is not None
        ctx = self._context(run, None)
        try:
            subjects = spec.handler.subjects(ctx)
        except Exception:
            logger.exception("subjects() failed for %s/%s", run_id, spec.name)
            return False
        created = 0
        for subject in subjects:
            existing = self.repos.pipeline.get_stage_run_for(run_id, spec.name, subject)
            if existing is not None:
                continue
            self.repos.pipeline.create_stage_run(
                run_id=run_id, stage=spec.name, subject=subject, status="pending"
            )
            created += 1
        self.repos.pipeline.mark_enumerated(run_id, spec.name)
        self._enqueue_pending_for_stage(run_id, spec)
        return created > 0

    def _enqueue_pending_for_stage(self, run_id: str, spec: StageSpec) -> None:
        for row in self.repos.pipeline.list_stage_runs(run_id, stage=spec.name):
            if row.status != "pending":
                continue
            self.repos.jobs.enqueue(
                kind="run_stage",
                payload={"stage_run_id": row.id},
                dedupe_key=f"{run_id}:{spec.name}:{row.subject_id}",
                stage_run_id=row.id,
            )

    def _deps_complete(self, run_id: str, spec: StageSpec) -> bool:
        for dep in spec.depends_on:
            if not self.repos.pipeline.is_enumerated(run_id, dep):
                return False
            rows = self.repos.pipeline.list_stage_runs(run_id, stage=dep)
            for row in rows:
                if row.status in _STAGE_LIVE:
                    return False
                if row.status == "failed":
                    return False
        return True

    # ---- staleness (rule 4) ----------------------------------------------

    def _invalidate_stale(self, run: PipelineRunRow) -> None:
        run_id = run.id
        stale: dict[str, str] = {}  # stage_run_id -> stage name
        stale_subjects: dict[str, SubjectRef] = {}
        by_stage: dict[str, list[StageRunRow]] = {}
        for spec in self.dag.order:
            by_stage[spec.name] = self.repos.pipeline.list_stage_runs(run_id, stage=spec.name)
        for spec in self.dag.order:
            for row in by_stage[spec.name]:
                if row.status != "succeeded":
                    continue
                subject = subject_from_stage_run(row)
                ctx = self._context(run, row.id)
                try:
                    new_hash = spec.handler.input_hash(ctx, subject)
                except Exception:
                    logger.warning("input_hash() failed for %s; keeping current state", row.id)
                    continue
                if row.input_hash and new_hash != row.input_hash:
                    stale[row.id] = spec.name
                    stale_subjects[row.id] = subject
        if not stale:
            return
        # Fixpoint: related rows in downstream stages go stale too.
        grew = True
        while grew:
            grew = False
            for spec in self.dag.order:
                for row in by_stage[spec.name]:
                    if row.id in stale or row.status != "succeeded":
                        continue
                    subject = subject_from_stage_run(row)
                    for seed_id, seed_stage in stale.items():
                        if spec.name != seed_stage and spec.name not in self.dag.downstream(
                            seed_stage
                        ):
                            continue
                        if subjects_related(stale_subjects[seed_id], subject):
                            stale[row.id] = spec.name
                            stale_subjects[row.id] = subject
                            grew = True
                            break
        marked = self.repos.pipeline.mark_stale(sorted(stale))
        logger.info("run %s: invalidated %d stage runs", run_id, marked)
        self.repos.audit.append(
            actor="engine",
            entity=f"run:{run_id}",
            event="staleness_invalidated",
            payload={"run_id": run_id, "stage_runs": sorted(stale)},
        )

    def _requeue_stale(self, run_id: str) -> None:
        """Flip any `stale` rows of the run back to `pending` (hash cleared)."""
        for spec in self.dag.order:
            for row in self.repos.pipeline.list_stage_runs(run_id, stage=spec.name):
                if row.status == "stale":
                    self.repos.pipeline.set_stage_run(
                        row.id, status="pending", clear_input_hash=True
                    )

    # ---- gates ---------------------------------------------------------------

    def _maybe_gate_stage(self, run_id: str, spec: StageSpec) -> bool:
        """Create a gate round for a completed gated stage.

        Returns True when enumeration must stop (a pending gate exists for
        this stage); False when the pipeline may continue.
        """
        gate_kind = spec.gate_after
        if gate_kind is None:
            return False
        rows = self.repos.pipeline.list_stage_runs(run_id, stage=spec.name)
        if any(r.status in _STAGE_LIVE for r in rows):
            return False
        if any(r.status == "failed" for r in rows):
            return False
        succeeded = [r for r in rows if r.status == "succeeded"]
        if not succeeded:
            # Zero-subject stage: nothing to approve; keep moving.
            return False
        snapshot = self._gate_snapshot_hash(succeeded)
        latest = self.repos.gates.latest(run_id, spec.name)
        if latest is not None and latest.snapshot_hash == snapshot:
            if latest.status == "pending":
                self.repos.pipeline.set_run_status(run_id, RunStatus.AWAITING_APPROVAL.value)
                return True
            return False
        allow = list(self.config.settings.gates.allow_auto_approve_in)
        run = self.repos.pipeline.get_run(run_id)
        assert run is not None
        try:
            _gate, auto_approved = self.gates.create_gate_round(
                run_id=run_id,
                stage=spec.name,
                gate=gate_kind,
                snapshot_hash=snapshot,
                profile=run.profile,
                auto_approve=bool(run.auto_approve),
                allow_auto_approve_in=allow,
            )
        except StudioError:
            logger.exception("gate creation failed for %s/%s", run_id, spec.name)
            return False
        if auto_approved:
            self.repos.pipeline.set_run_status(run_id, RunStatus.RUNNING.value)
            return False
        self.repos.pipeline.set_run_status(run_id, RunStatus.AWAITING_APPROVAL.value)
        return True

    @staticmethod
    def _gate_snapshot_hash(rows: list[StageRunRow]) -> str:
        items = sorted((r.id, r.output_json or "") for r in rows)
        return hashlib.sha256(canonical_json(items)).hexdigest()

    def _maybe_complete(self, run_id: str) -> None:
        run = self.repos.pipeline.get_run(run_id)
        assert run is not None
        if run.status in (RunStatus.HALTED_BUDGET.value, RunStatus.COMPLETED.value):
            return
        if run.status == RunStatus.FAILED.value:
            return
        pending_gates = self.repos.gates.list_pending(run_id)
        if pending_gates:
            if run.status != RunStatus.AWAITING_APPROVAL.value:
                self.repos.pipeline.set_run_status(run_id, RunStatus.AWAITING_APPROVAL.value)
            return
        all_done = True
        for spec in self.dag.order:
            if not self.repos.pipeline.is_enumerated(run_id, spec.name):
                all_done = False
                break
            rows = self.repos.pipeline.list_stage_runs(run_id, stage=spec.name)
            if any(r.status in (*_STAGE_LIVE, "failed") for r in rows):
                all_done = False
                break
        if all_done:
            self.repos.pipeline.set_run_status(run_id, RunStatus.COMPLETED.value)
            if run.episode_id:
                self.repos.content.set_episode_status(run.episode_id, RunStatus.COMPLETED.value)
            self.repos.audit.append(
                actor="engine",
                entity=f"run:{run_id}",
                event="run_completed",
                payload={"run_id": run_id},
            )

    # ==================================================================
    # jobs
    # ==================================================================

    async def handle_job(self, job_id: str) -> None:
        job = self.repos.jobs.get(job_id)
        if job is None or job.status != "running":
            return
        payload = json.loads(job.payload_json) if job.payload_json else {}
        kind = job.kind
        if kind == "run_stage":
            await self._run_stage_job(job, payload)
        elif kind == "advance_run":
            self.advance_run(str(payload["run_id"]))
            self.repos.jobs.complete(job_id)
        else:
            logger.error("unknown job kind %r", kind)
            self.repos.jobs.complete(job_id)

    async def _run_stage_job(self, job: JobRow, payload: dict[str, Any]) -> None:
        stage_run_id = str(payload["stage_run_id"])
        row = self.repos.pipeline.get_stage_run(stage_run_id)
        if row is None:
            logger.warning("run_stage: unknown stage_run %s", stage_run_id)
            self.repos.jobs.complete(job.id)
            return
        if row.status != "pending":
            self.repos.jobs.complete(job.id)
            return
        run = self.repos.pipeline.get_run(row.run_id)
        if run is None:
            self.repos.jobs.complete(job.id)
            return
        if run.status not in (RunStatus.RUNNING.value, RunStatus.AWAITING_APPROVAL.value):
            # A halted/failed/completed run must not spend: jobs enqueued
            # before the halt are dropped on the floor.
            logger.info(
                "run_stage skipped: run %s is %s (refusing to execute %s)",
                run.id, run.status, row.stage,
            )
            self.repos.jobs.complete(job.id)
            return
        spec = self.dag.spec(row.stage)
        handler = spec.handler
        subject = subject_from_stage_run(row)
        ctx = self._context(run, stage_run_id)

        # Rule 4 again: don't execute against changed inputs.
        try:
            current_hash = handler.input_hash(ctx, subject)
        except Exception:
            current_hash = None
            logger.warning("input_hash() failed for %s; proceeding", stage_run_id)
        if row.input_hash and current_hash and current_hash != row.input_hash:
            self.repos.pipeline.set_stage_run(stage_run_id, status="pending")
            self.repos.jobs.complete(job.id)
            self.repos.jobs.enqueue(
                kind="advance_run",
                payload={"run_id": run.id},
                dedupe_key=f"advance:{run.id}",
            )
            return

        self.repos.pipeline.set_stage_run(stage_run_id, status="running")
        try:
            output = await handler.run(ctx, subject)
        except SimulatedCrash:
            raise  # not caught: state left as a killed process would leave it
        except BudgetExceeded as exc:
            await self._budget_halt(job, row, run, exc)
            return
        except ProviderQuotaExceeded as exc:
            await self._quota_halt(job, row, run, exc)
            return
        except TransientProviderError as exc:
            await self._transient_failure(job, row, run, exc)
            return
        except StudioError as exc:
            await self._terminal_failure(job, row, run, exc)
            return
        except Exception as exc:
            logger.exception("stage %s crashed", stage_run_id)
            await self._terminal_failure(job, row, run, exc)
            return

        if not isinstance(output, StageOutput):
            output = (
                StageOutput(output=dict(output))
                if isinstance(output, dict)
                else StageOutput(output={})
            )
        try:
            final_hash = handler.input_hash(ctx, subject)
        except Exception:
            final_hash = None
        self.repos.pipeline.set_stage_run(
            stage_run_id,
            status="succeeded",
            input_hash=final_hash,
            output_json=canonical_text(output.output),
        )
        self.repos.jobs.complete(job.id)
        self.repos.jobs.enqueue(
            kind="advance_run",
            payload={"run_id": run.id},
            dedupe_key=f"advance:{run.id}",
        )

    async def _budget_halt(
        self, job: JobRow, row: StageRunRow, run: PipelineRunRow, exc: BudgetExceeded
    ) -> None:
        self.repos.pipeline.set_stage_run(row.id, status="pending")
        self.repos.pipeline.set_run_status(run.id, RunStatus.HALTED_BUDGET.value)
        if run.episode_id:
            self.repos.content.set_episode_status(run.episode_id, RunStatus.HALTED_BUDGET.value)
        self.repos.jobs.complete(job.id)
        self.repos.audit.append(
            actor="engine",
            entity=f"run:{run.id}",
            event="budget_halted",
            payload={"run_id": run.id, "stage_run_id": row.id, "error": str(exc)[:500]},
        )
        logger.warning("run %s halted on budget: %s", run.id, exc)

    async def _quota_halt(
        self, job: JobRow, row: StageRunRow, run: PipelineRunRow, exc: ProviderQuotaExceeded
    ) -> None:
        # Same shape as a budget halt: stage back to pending, run halted_budget,
        # audit with reason provider_quota, never retried.
        self.repos.pipeline.set_stage_run(row.id, status="pending")
        self.repos.pipeline.set_run_status(run.id, RunStatus.HALTED_BUDGET.value)
        if run.episode_id:
            self.repos.content.set_episode_status(run.episode_id, RunStatus.HALTED_BUDGET.value)
        self.repos.jobs.complete(job.id)
        self.repos.audit.append(
            actor="engine",
            entity=f"run:{run.id}",
            event="budget_halted",
            payload={
                "run_id": run.id,
                "stage_run_id": row.id,
                "error": str(exc)[:500],
                "reason": "provider_quota",
            },
        )
        logger.warning("run %s halted on provider quota: %s", run.id, exc)

    async def _transient_failure(
        self, job: JobRow, row: StageRunRow, run: PipelineRunRow, exc: TransientProviderError
    ) -> None:
        if job.attempt >= job.max_attempts:
            await self._terminal_failure(job, row, run, exc)
            return
        run_after = format_ts(
            self.clock.now() + timedelta(seconds=self.config.settings.worker.job_retry_backoff_s)
        )
        self.repos.pipeline.set_stage_run(row.id, status="pending")
        self.repos.jobs.requeue(job.id, run_after)
        self.repos.audit.append(
            actor="engine",
            entity=f"stage_run:{row.id}",
            event="stage_run_retry_scheduled",
            payload={
                "stage_run_id": row.id,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "run_after": run_after,
                "error": str(exc)[:500],
            },
        )

    async def _terminal_failure(
        self, job: JobRow, row: StageRunRow, run: PipelineRunRow, exc: BaseException
    ) -> None:
        self.repos.pipeline.set_stage_run(row.id, status="failed")
        self.repos.pipeline.set_run_status(run.id, RunStatus.FAILED.value)
        if run.episode_id:
            self.repos.content.set_episode_status(run.episode_id, RunStatus.FAILED.value)
        self.repos.jobs.mark_failed(job.id)
        self.repos.audit.append(
            actor="engine",
            entity=f"stage_run:{row.id}",
            event="stage_run_failed",
            payload={"stage_run_id": row.id, "run_id": run.id, "error": str(exc)[:500]},
        )
