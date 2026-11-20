"""Dependency-completion check shared by the engine and crash recovery.

`_deps_complete` semantics (recursive):

- a dependency is complete iff it is enumerated, owns no `pending`/`running`/
  `stale`/`failed` rows, and — recursively — all of *its* dependencies are
  complete.
- a dependency with zero rows is trivially complete for its own rows, but its
  own ancestors must still be complete (an empty zero-subject gated stage like
  `s8a_character_sheets` with no bible must not let its descendants run ahead
  of live upstream stages).

The engine uses this per advance_run pass; crash recovery uses it to avoid
re-enqueuing rows whose ancestors are still live (Phase 4: otherwise pending
s8/s10/demo_finalize rows are swept into a concurrent execution that violate
the DAG and trips the H0 gate barrier).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from studio.db.repos import StudioRepos
    from studio.pipeline.dag import Dag

_STAGE_LIVE = ("pending", "running", "stale")


def deps_complete(
    repos: StudioRepos,
    dag: Dag,
    run_id: str,
    spec_name: str,
    memo: dict[str, bool] | None = None,
) -> bool:
    """True iff `spec_name` is enumerated, owns no live/failed rows, and all of
    its own dependencies (recursively) are complete. A dependency with zero
    rows is trivially satisfied at that stage, but its ancestors must still be
    complete."""
    if memo is None:
        memo = {}
    if spec_name in memo:
        return memo[spec_name]
    spec = dag.spec(spec_name)
    if not repos.pipeline.is_enumerated(run_id, spec_name):
        memo[spec_name] = False
        return False
    rows = repos.pipeline.list_stage_runs(run_id, stage=spec_name)
    for row in rows:
        if row.status in _STAGE_LIVE:
            memo[spec_name] = False
            return False
        if row.status == "failed":
            memo[spec_name] = False
            return False
    # Zero rows: nothing to wait for at this stage, but ancestors still must be
    # complete. Recurse into direct dependencies (DAG is acyclic).
    ok = all(deps_complete(repos, dag, run_id, dep, memo) for dep in spec.depends_on)
    memo[spec_name] = ok
    return ok


def direct_deps_complete(repos: StudioRepos, dag: Dag, run_id: str, spec_name: str) -> bool:
    """True iff every dependency of `spec_name` is complete (recursively).

    The stage itself is not required to be enumerated — this is the check used
    to decide whether `spec_name` may start.
    """
    from studio.pipeline.dag import Dag as _Dag

    assert isinstance(dag, _Dag)
    spec = dag.spec(spec_name)
    memo: dict[str, bool] = {}
    return all(deps_complete(repos, dag, run_id, dep, memo) for dep in spec.depends_on)


__all__ = ["deps_complete", "direct_deps_complete"]
