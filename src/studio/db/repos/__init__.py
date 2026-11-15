"""Protocol-backed repositories with SQLite implementations."""

from __future__ import annotations

from dataclasses import dataclass

from studio.core.clock import Clock
from studio.core.ids import Ids

from ..connection import Database
from .assets import AssetsRepo, SqliteAssetsRepo
from .audit import AuditRepo, SqliteAuditRepo
from .bible import BibleRepo, SqliteBibleRepo
from .budgets import BudgetsRepo, SqliteBudgetsRepo
from .content import ContentRepo, SqliteContentRepo
from .gates import GatesRepo, SqliteGatesRepo
from .jobs import JobsRepo, SqliteJobsRepo
from .pipeline import PipelineRepo, SqlitePipelineRepo, subject_from_stage_run
from .providers import NewCacheEntry, NewCall, ProvidersRepo, SqliteProvidersRepo

__all__ = [
    "AssetsRepo",
    "AuditRepo",
    "BibleRepo",
    "BudgetsRepo",
    "ContentRepo",
    "GatesRepo",
    "JobsRepo",
    "NewCacheEntry",
    "NewCall",
    "PipelineRepo",
    "ProvidersRepo",
    "SqliteAssetsRepo",
    "SqliteAuditRepo",
    "SqliteBibleRepo",
    "SqliteBudgetsRepo",
    "SqliteContentRepo",
    "SqliteGatesRepo",
    "SqliteJobsRepo",
    "SqlitePipelineRepo",
    "SqliteProvidersRepo",
    "StudioRepos",
    "build_repos",
    "subject_from_stage_run",
]


@dataclass
class StudioRepos:
    assets: AssetsRepo
    bible: BibleRepo
    providers: ProvidersRepo
    budgets: BudgetsRepo
    jobs: JobsRepo
    pipeline: PipelineRepo
    gates: GatesRepo
    content: ContentRepo
    audit: AuditRepo


def build_repos(db: Database, clock: Clock, ids: Ids | None = None) -> StudioRepos:
    return StudioRepos(
        assets=SqliteAssetsRepo(db, clock, ids),
        bible=SqliteBibleRepo(db, clock, ids),
        providers=SqliteProvidersRepo(db, clock, ids),
        budgets=SqliteBudgetsRepo(db, clock, ids),
        jobs=SqliteJobsRepo(db, clock, ids),
        pipeline=SqlitePipelineRepo(db, clock, ids),
        gates=SqliteGatesRepo(db, clock, ids),
        content=SqliteContentRepo(db, clock, ids),
        audit=SqliteAuditRepo(db, clock, ids),
    )
