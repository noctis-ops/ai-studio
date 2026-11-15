"""Injectable ULID identity source.

No bare `uuid`/`random` is allowed anywhere else in src/; tests inject a
deterministic generator instead.
"""

from __future__ import annotations

import itertools
from typing import Protocol

import ulid


class Ids(Protocol):
    def new(self) -> str: ...


class UlidIds:
    def new(self) -> str:
        return str(ulid.ULID())


class SequentialIds:
    """Deterministic ids for tests: prefix-000001, prefix-000002, ..."""

    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._counter = itertools.count(1)

    def new(self) -> str:
        return f"{self._prefix}-{next(self._counter):06d}"


_default_ids = UlidIds()


def new_id() -> str:
    return _default_ids.new()
