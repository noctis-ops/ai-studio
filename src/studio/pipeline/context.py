"""RunContext: everything a stage handler needs, wired once per job.

Handlers obtain languages EXCLUSIVELY from `ctx.languages`; aspects likewise.
Nothing may hardcode a language or aspect.
"""

from __future__ import annotations

from dataclasses import dataclass

from studio.config.loader import AppConfig
from studio.config.registry import ResolvedProfile
from studio.core.clock import Clock
from studio.core.gateway import ProviderGateway
from studio.core.ids import Ids
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.domain.enums import Aspect, Language


@dataclass
class RunContext:
    db: Database
    repos: StudioRepos
    gateway: ProviderGateway
    clock: Clock
    ids: Ids
    config: AppConfig
    resolved_profile: ResolvedProfile
    run_id: str
    episode_id: str | None
    languages: list[Language]
    aspects: list[Aspect]
    stage_run_id: str | None = None
    # H0: free stages and Phase 1-3 demo handlers keep the permissive default.
    # The engine lowers this to False only for paid stages, then raises it
    # again after assert_gates_cleared() proves every ancestor gate current.
    gate_cleared: bool = True
