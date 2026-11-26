"""Retention: publish marker, purge planning/execution, GC (Phase 5 §M / S15).

Two invariants drive this package:

1. **Publish before purge.** `mark_published` is the only way an episode becomes
   `published`, and `run_purge` refuses a non-published episode.
2. **Never pay twice.** A cache hit whose asset was purged fails closed
   (`AssetPurged`) in the gateway; only an explicit `--allow-repay-purged`
   action may spend again, and it is audited.

Nothing here is an engine node: publishing and purging are operator actions.
"""

from __future__ import annotations

from .plan import PurgeItem, PurgePlan, build_plan, gc_plan
from .protect import ProtectionIndex, build_protection_index, is_protected, scan_json_shas
from .publish import mark_published, parse_published_at
from .run import PurgeResult, run_gc, run_purge

__all__ = [
    "ProtectionIndex",
    "PurgeItem",
    "PurgePlan",
    "PurgeResult",
    "build_plan",
    "build_protection_index",
    "gc_plan",
    "is_protected",
    "mark_published",
    "parse_published_at",
    "run_gc",
    "run_purge",
    "scan_json_shas",
]
