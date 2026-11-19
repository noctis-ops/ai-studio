"""Dependency graph validation + deterministic topological order."""

from __future__ import annotations

from collections.abc import Iterable

from studio.domain.errors import ConfigError
from studio.pipeline.stage import StageSpec


class Dag:
    def __init__(self, stages: Iterable[StageSpec]) -> None:
        specs = list(stages)
        if not specs:
            raise ConfigError("at least one stage is required")
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ConfigError(f"duplicate stage names: {', '.join(duplicates)}")
        known = set(names)
        for spec in specs:
            for dep in spec.depends_on:
                if dep not in known:
                    raise ConfigError(f"stage {spec.name!r} depends on unknown stage {dep!r}")
        self._specs = self._toposort(specs)
        self._by_name = {spec.name: spec for spec in self._specs}
        self._order = [spec.name for spec in self._specs]
        self._downstream = self._transitive_downstream(specs)
        self._upstream = self._transitive_upstream(specs)

    # ---- API ------------------------------------------------------------

    @property
    def order(self) -> list[StageSpec]:
        return list(self._specs)

    @property
    def names(self) -> list[str]:
        return list(self._order)

    def spec(self, name: str) -> StageSpec:
        return self._by_name[name]

    def deps(self, name: str) -> tuple[str, ...]:
        return self._by_name[name].depends_on

    def downstream(self, name: str) -> frozenset[str]:
        return self._downstream.get(name, frozenset())

    def ancestors(self, name: str) -> frozenset[str]:
        """Transitive `depends_on` closure (H0 gate barrier)."""
        return self._upstream.get(name, frozenset())

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _toposort(specs: list[StageSpec]) -> list[StageSpec]:
        """Deterministic topological order.

        Always emits the lowest-indexed ready stage (rather than appending
        whole waves) so the result respects declaration order among
        independent branches; the engine relies on this to enumerate
        cheaper, already-declared stages before later gated ones.
        """
        index = {spec.name: i for i, spec in enumerate(specs)}
        indegree = {spec.name: len(spec.depends_on) for spec in specs}
        remaining = set(index)
        by_name = {spec.name: spec for spec in specs}
        ordered: list[StageSpec] = []
        while remaining:
            ready = sorted((n for n in remaining if indegree[n] == 0), key=lambda n: index[n])
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise ConfigError(f"stage dependency cycle involving: {cycle}")
            name = ready[0]
            ordered.append(by_name[name])
            remaining.remove(name)
            for other in specs:
                if name in other.depends_on:
                    indegree[other.name] -= 1
        return ordered

    @staticmethod
    def _transitive_upstream(specs: list[StageSpec]) -> dict[str, frozenset[str]]:
        result: dict[str, frozenset[str]] = {}
        by_name = {spec.name: spec for spec in specs}

        def expand(name: str) -> set[str]:
            seen: set[str] = set()
            stack = list(by_name[name].depends_on)
            while stack:
                current = stack.pop()
                if current in seen or current not in by_name:
                    continue
                seen.add(current)
                stack.extend(by_name[current].depends_on)
            return seen

        for spec in specs:
            result[spec.name] = frozenset(expand(spec.name))
        return result

    @staticmethod
    def _transitive_downstream(specs: list[StageSpec]) -> dict[str, frozenset[str]]:
        direct: dict[str, set[str]] = {spec.name: set() for spec in specs}
        for spec in specs:
            for dep in spec.depends_on:
                direct[dep].add(spec.name)
        result: dict[str, frozenset[str]] = {}

        def expand(name: str) -> set[str]:
            seen: set[str] = set()
            stack = list(direct[name])
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(direct[current])
            return seen

        for spec in specs:
            result[spec.name] = frozenset(expand(spec.name))
        return result
