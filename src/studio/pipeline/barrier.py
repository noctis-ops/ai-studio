"""H0 paid-stage gate barrier (R1).

The scheduler is intentionally left alone: queued jobs can still be claimed
while a gate is pending. This module is the execution-time proof that a paid
stage may spend. For every ancestor stage carrying a gate it requires:

1. the latest gate round is ``approved`` / ``auto_approved``, and
2. that round's ``snapshot_hash`` still equals the current hash of the
   ancestor stage's succeeded outputs (so approving, then changing an
   ancestor output, re-blocks the paid descendant).

A gated ancestor with zero succeeded stage runs needs no approval (e.g. S8a
generates no sheets and therefore creates no G0 round).

Phase 5 (§S15) adds a second, independent barrier: once an episode is
``published`` or ``archived`` it is closed, and no paid stage may run for it
(``EpisodeClosed``). Free stages stay runnable so a re-export remains possible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from studio.core.canonical import canonical_json
from studio.domain.errors import GateNotCleared
from studio.domain.models import PipelineRunRow, StageRunRow

if TYPE_CHECKING:
    from studio.db.repos import StudioRepos
    from studio.pipeline.dag import Dag

_APPROVED = frozenset({"approved", "auto_approved"})


def stage_outputs_snapshot_hash(rows: list[StageRunRow]) -> str:
    """Identical to Engine._gate_snapshot_hash, shared so they cannot drift."""
    import hashlib

    items = sorted((r.id, r.output_json or "") for r in rows)
    return hashlib.sha256(canonical_json(items)).hexdigest()


def ancestor_gate_problems(
    *, repos: StudioRepos, dag: Dag, run_id: str, stage_id: str
) -> list[str]:
    """All uncleared ancestor gates, in deterministic order (never raises)."""
    problems: list[str] = []
    for ancestor in sorted(dag.ancestors(stage_id)):
        spec = dag.spec(ancestor)
        if spec.gate_after is None:
            continue
        succeeded = [
            row
            for row in repos.pipeline.list_stage_runs(run_id, stage=ancestor)
            if row.status == "succeeded"
        ]
        if not succeeded:
            # Zero-subject gated stage: the engine creates no gate round.
            continue
        current = stage_outputs_snapshot_hash(succeeded)
        latest = repos.gates.latest(run_id, ancestor)
        if latest is None:
            problems.append(
                f"{ancestor}/{spec.gate_after.value}: no gate round exists yet"
            )
            continue
        if latest.status not in _APPROVED:
            problems.append(
                f"{ancestor}/{spec.gate_after.value}: latest round is {latest.status!r}"
            )
            continue
        if latest.snapshot_hash != current:
            approved_hash = latest.snapshot_hash or "<missing>"
            problems.append(
                f"{ancestor}/{spec.gate_after.value}: approved snapshot is stale "
                f"({approved_hash[:12]} != {current[:12]})"
            )
    return problems


_CLOSED = frozenset({"published", "archived"})


def episode_closed_problem(repos: StudioRepos, run: PipelineRunRow) -> str | None:
    """Return a human-readable reason when the run's episode is closed."""
    if not run.episode_id:
        return None
    episode = repos.content.get_episode(run.episode_id)
    if episode is None:
        return None
    if episode.status in _CLOSED:
        return f"episode {episode.id} is {episode.status}"
    return None


def assert_gates_cleared(
    *, repos: StudioRepos, dag: Dag, run_id: str, stage_id: str
) -> None:
    """Raise GateNotCleared unless every ancestor gate round is current."""
    problems = ancestor_gate_problems(repos=repos, dag=dag, run_id=run_id, stage_id=stage_id)
    if problems:
        raise GateNotCleared(
            f"paid stage {stage_id!r} blocked by ancestor gates: " + "; ".join(problems)
        )


def free_stage_gate_problems(
    *, repos: StudioRepos, dag: Dag, run_id: str, stage_id: str
) -> list[str]:
    """Gate proof for a *free* stage that still requires cleared ancestors."""
    return ancestor_gate_problems(repos=repos, dag=dag, run_id=run_id, stage_id=stage_id)
