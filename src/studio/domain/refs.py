"""Immutable reference types shared across layers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import Scope


@dataclass(frozen=True)
class AssetRef:
    sha256: str
    rel_path: str  # POSIX-style path relative to the CAS root
    kind: str
    mime: str
    size_bytes: int
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None


# An ancestor chain, root first, e.g. ((episode, "ep_1"), (track, "tr_1")).
Lineage = tuple[tuple[Scope, str], ...]


def lineage_contains(lineage: Lineage, scope: Scope, subject_id: str) -> bool:
    return any(s == scope and i == subject_id for s, i in lineage)


@dataclass(frozen=True, order=True)
class SubjectRef:
    """Identifies one subject of a stage run.

    `lineage` lists ancestors root-first. Ordered so lists of subjects can be
    sorted deterministically for persistence and hashing.
    """

    scope: Scope
    id: str
    lineage: Lineage = field(default=(), compare=True)

    def ancestor_or_equal(self, other: SubjectRef) -> bool:
        """True if `self` is `other` or an ancestor of `other`."""
        if self.scope == other.scope and self.id == other.id:
            return True
        return lineage_contains(other.lineage, self.scope, self.id)


def subjects_related(a: SubjectRef, b: SubjectRef) -> bool:
    """Spec relation: one subject is an ancestor-or-equal of the other."""
    return a.ancestor_or_equal(b) or b.ancestor_or_equal(a)


def lineage_to_json(lineage: Lineage) -> list[list[str]]:
    return [[scope.value, subject_id] for scope, subject_id in lineage]


def lineage_from_json(data: object) -> Lineage:
    if not isinstance(data, list):
        raise ValueError("lineage_json must be a list")
    out: list[tuple[Scope, str]] = []
    for item in data:
        if not (isinstance(item, list) and len(item) == 2):
            raise ValueError("lineage entries must be [scope, id] pairs")
        out.append((Scope(str(item[0])), str(item[1])))
    return tuple(out)
