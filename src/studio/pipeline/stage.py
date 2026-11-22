"""Stage contracts: handler protocol, spec, output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from studio.domain.enums import GateKind, Scope
from studio.domain.refs import SubjectRef

if TYPE_CHECKING:
    from studio.pipeline.context import RunContext


class StageHandler(Protocol):
    def subjects(self, ctx: RunContext) -> list[SubjectRef]: ...

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str: ...

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput: ...


@dataclass(frozen=True)
class StageSpec:
    name: str
    scope: Scope
    depends_on: tuple[str, ...]
    handler: StageHandler
    gate_after: GateKind | None = None
    # Phase 4 H0: paid stages must prove that every ancestor gate round is
    # both approved and still bound to the current stage outputs before the
    # handler is entered. Free stages are unaffected.
    spends_money: bool = False
    # Phase 5 (H5.7): a *free* stage may still need ancestor gates cleared
    # before it runs (S9 animatic is free but must not render an unapproved
    # cut). Defaults keep every Phase <= 4 spec unchanged.
    requires_cleared_gates: bool = False
    # The stage whose output a rejection of this stage's gate must stale.
    # ``None`` keeps the historical behaviour (stale this stage + downstream).
    gate_reject_stage: str | None = None


@dataclass(frozen=True)
class StageOutput:
    output: dict[str, Any]
