"""Protocol-backed repositories with SQLite implementations."""

from __future__ import annotations

from dataclasses import dataclass

from studio.core.clock import Clock
from studio.core.ids import Ids

from ..connection import Database
from .assets import AssetsRepo, SqliteAssetsRepo
from .audio import AudioRepo, NewAudioCue, SqliteAudioRepo
from .audit import AuditRepo, SqliteAuditRepo
from .bible import BibleRepo, SqliteBibleRepo
from .budgets import BudgetsRepo, SqliteBudgetsRepo
from .content import ContentRepo, SqliteContentRepo
from .exports import ExportsRepo, SqliteExportsRepo
from .gates import GatesRepo, SqliteGatesRepo
from .jobs import JobsRepo, SqliteJobsRepo
from .pipeline import PipelineRepo, SqlitePipelineRepo, subject_from_stage_run
from .proposals import ProposalsRepo, SqliteProposalsRepo
from .providers import NewCacheEntry, NewCall, ProvidersRepo, SqliteProvidersRepo
from .sheets import SheetsRepo, SqliteSheetsRepo

__all__ = [
    "AssetsRepo",
    "AudioRepo",
    "AuditRepo",
    "BibleRepo",
    "BudgetsRepo",
    "ContentRepo",
    "ExportsRepo",
    "GatesRepo",
    "JobsRepo",
    "NewAudioCue",
    "NewCacheEntry",
    "NewCall",
    "PipelineRepo",
    "ProposalsRepo",
    "ProvidersRepo",
    "SheetsRepo",
    "SqliteAssetsRepo",
    "SqliteAudioRepo",
    "SqliteAuditRepo",
    "SqliteBibleRepo",
    "SqliteBudgetsRepo",
    "SqliteContentRepo",
    "SqliteExportsRepo",
    "SqliteGatesRepo",
    "SqliteJobsRepo",
    "SqlitePipelineRepo",
    "SqliteProposalsRepo",
    "SqliteProvidersRepo",
    "SqliteSheetsRepo",
    "StudioRepos",
    "build_repos",
    "subject_from_stage_run",
]


@dataclass
class StudioRepos:
    assets: AssetsRepo
    audio: AudioRepo
    bible: BibleRepo
    providers: ProvidersRepo
    budgets: BudgetsRepo
    jobs: JobsRepo
    pipeline: PipelineRepo
    gates: GatesRepo
    content: ContentRepo
    audit: AuditRepo
    sheets: SheetsRepo
    exports: ExportsRepo
    proposals: ProposalsRepo


def build_repos(db: Database, clock: Clock, ids: Ids | None = None) -> StudioRepos:
    return StudioRepos(
        assets=SqliteAssetsRepo(db, clock, ids),
        audio=SqliteAudioRepo(db, clock, ids),
        bible=SqliteBibleRepo(db, clock, ids),
        providers=SqliteProvidersRepo(db, clock, ids),
        budgets=SqliteBudgetsRepo(db, clock, ids),
        jobs=SqliteJobsRepo(db, clock, ids),
        pipeline=SqlitePipelineRepo(db, clock, ids),
        gates=SqliteGatesRepo(db, clock, ids),
        content=SqliteContentRepo(db, clock, ids),
        audit=SqliteAuditRepo(db, clock, ids),
        sheets=SqliteSheetsRepo(db, clock, ids),
        exports=SqliteExportsRepo(db, clock, ids),
        proposals=SqliteProposalsRepo(db, clock, ids),
    )
