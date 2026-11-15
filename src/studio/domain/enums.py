"""Shared enums. Values match the CHECK constraints in db/migrations/001_init.sql.

This module is one of the few places (with config/ and language/) allowed to
contain the bare language codes, enforced by the T-LANG-01 lint test.
"""

from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    AR = "ar"
    EN = "en"


class Aspect(StrEnum):
    # str-Enum values ARE the config strings; the hyphen is why we cannot use
    # identifier-style member names that map 1:1.
    WIDE = "16:9"
    TALL = "9:16"


class Profile(StrEnum):
    SANDBOX = "sandbox"
    DRAFT = "draft"
    FINAL = "final"


class Scope(StrEnum):
    SERIES = "series"
    EPISODE = "episode"
    TRACK = "track"
    DELIVERABLE = "deliverable"
    SHOT = "shot"
    LINE = "line"


class GateKind(StrEnum):
    G0_CHARACTERS = "G0_CHARACTERS"
    G1_SCRIPT = "G1_SCRIPT"
    G2_KEYFRAMES = "G2_KEYFRAMES"
    G3_FINAL_CUT = "G3_FINAL_CUT"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    HALTED_BUDGET = "halted_budget"
    COMPLETED = "completed"
    FAILED = "failed"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    SKIPPED = "skipped"


class CallStatus(StrEnum):
    RESERVED = "reserved"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    POLICY = "policy"
    PERMANENT = "permanent"


class BudgetScope(StrEnum):
    EPISODE = "episode"
    MONTH = "month"
    GLOBAL = "global"


GLOBAL_SCOPE_REF = "global"


def budget_scopes_for_call(episode_id: str | None, month_tag: str) -> list[tuple[BudgetScope, str]]:
    """Scopes a paid call is charged against.

    Calls without an episode (e.g. series setup) are checked against the month
    and global scopes only, per the ledger spec.
    """
    scopes: list[tuple[BudgetScope, str]] = []
    if episode_id:
        scopes.append((BudgetScope.EPISODE, episode_id))
    scopes.append((BudgetScope.MONTH, month_tag))
    scopes.append((BudgetScope.GLOBAL, GLOBAL_SCOPE_REF))
    return scopes
