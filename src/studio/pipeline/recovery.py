"""Crash recovery. Provider calls are NOT touched — the Gateway adopts
in-flight provider work lazily (H6 step 2). Every recovery action is audited.
"""

from __future__ import annotations

from datetime import timedelta

from studio.core.clock import Clock, format_ts
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.domain.enums import RunStatus


def enqueue_pending_stage_runs(repos: StudioRepos) -> int:
    """Enqueue `run_stage` jobs for every pending stage_run of a live run that
    does not already have a queued/running job. Idempotent via dedupe_key."""
    enqueued = 0
    live_stage_runs = repos.jobs.running_stage_run_ids()
    for run_status in (RunStatus.RUNNING.value, RunStatus.AWAITING_APPROVAL.value):
        for run in repos.pipeline.list_runs(status=run_status):
            for row in repos.pipeline.list_stage_runs(run.id):
                if row.status != "pending":
                    continue
                if row.id in live_stage_runs:
                    continue
                job = repos.jobs.enqueue(
                    kind="run_stage",
                    payload={"stage_run_id": row.id},
                    dedupe_key=f"{run.id}:{row.stage}:{row.subject_id}",
                    stage_run_id=row.id,
                )
                if job is not None:
                    enqueued += 1
    return enqueued


def recover_stale_jobs(
    db: Database,
    repos: StudioRepos,
    clock: Clock,
    stale_after_s: float,
) -> list[str]:
    """Reset `running` jobs whose heartbeat went stale.

    Attempts were already incremented at claim time, so:
    - attempts >= max_attempts → mark_failed + stage_run failed (+ run failed)
    - otherwise                → requeued (same row, attempt count preserved)

    Returns the list of affected job ids.
    """
    deadline = format_ts(clock.now() - timedelta(seconds=stale_after_s))
    affected: list[str] = []
    for job in repos.jobs.stale_running(deadline):
        stage_run_id = job.stage_run_id
        if job.attempt >= job.max_attempts:
            repos.jobs.mark_failed(job.id)
            if stage_run_id:
                repos.pipeline.set_stage_run(stage_run_id, status="failed")
                row = repos.pipeline.get_stage_run(stage_run_id)
                if row is not None:
                    run = repos.pipeline.get_run(row.run_id)
                    if run is not None:
                        repos.pipeline.set_run_status(run.id, RunStatus.FAILED.value)
                        if run.episode_id:
                            repos.content.set_episode_status(run.episode_id, RunStatus.FAILED.value)
            event = "job_recovery_failed"
        else:
            repos.jobs.requeue(job.id, clock.now_iso())
            if stage_run_id:
                repos.pipeline.set_stage_run(stage_run_id, status="pending")
            event = "job_recovery_requeued"
        repos.audit.append(
            actor="recovery",
            entity=f"job:{job.id}",
            event=event,
            payload={
                "job_id": job.id,
                "kind": job.kind,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "stage_run_id": stage_run_id,
                "stale_deadline": deadline,
            },
        )
        affected.append(job.id)
    return affected


def recover_stage_runs(
    db: Database,
    repos: StudioRepos,
    clock: Clock,
) -> list[str]:
    """Reset `running` stage_runs that have no `running` job back to `pending`."""
    live_stage_runs = repos.jobs.running_stage_run_ids()
    affected: list[str] = []
    for row in repos.pipeline.list_running_stage_runs():
        if row.id in live_stage_runs:
            continue
        repos.pipeline.set_stage_run(row.id, status="pending")
        repos.audit.append(
            actor="recovery",
            entity=f"stage_run:{row.id}",
            event="stage_run_recovery_pending",
            payload={"stage_run_id": row.id, "run_id": row.run_id, "stage": row.stage},
        )
        affected.append(row.id)
    return affected


def recover(
    db: Database,
    repos: StudioRepos,
    clock: Clock,
    stale_after_s: float,
) -> None:
    """Full recovery pass run at worker start (and usable from the CLI)."""
    recover_stale_jobs(db, repos, clock, stale_after_s)
    recover_stage_runs(db, repos, clock)
    enqueue_pending_stage_runs(repos)
