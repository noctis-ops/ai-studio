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


@dataclass(frozen=True)
class StageOutput:
    output: dict[str, Any]
