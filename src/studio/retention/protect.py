"""What may never be deleted (Phase 5 §M).

The CAS is shared: one file can be referenced by several episodes and by rows
the operator never sees. Purge therefore never trusts "this asset looks like an
intermediate" on its own; it computes a protection set first, from the whole
database, and only assets outside that set can be considered.

Protected, in the spec's wording and in this module's terms:

1. `assets.retention = 'permanent'` — final renders, sidecars, exports, bible.
2. The asset of any **approved** `character_sheets` row, in any series.
3. Keyframe assets of a gated stage whose latest gate round is **approved**
   (G2 on `s8_keyframes`): the human signed off on exactly those frames.
4. Assets referenced by a **non-published** episode, through any table that
   links to it, *or by a stage output of a run of such an episode*. This is the
   rule that keeps a shared file alive for the next episode.
5. Anything cited by a run that is still `running` / `awaiting_approval` /
   `halted` — work in flight is not garbage.

`scan_json_shas` is how stage outputs are mined: every stage output is JSON, and
the 64-hex strings inside it are asset references (that is how the pipeline
records outputs today), so a single regex covers future stages too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from studio.db.connection import Database
from studio.domain.models import AssetRow

SHA_RE = re.compile(r"\b[0-9a-f]{64}\b")

# §M: everything an approved character sheet or a signed-off keyframe touches.
_PERMANENT = "permanent"

_OPEN_EPISODE_STATUSES = frozenset({"published", "archived"})

# §M protects "gate-approved keyframes" — the frames a human signed off at G2.
# Other gated stages (the animatic at G2, the render at G3) are protected by
# their own rules (the render is `retention=permanent`), and the animatic is
# explicitly listed as purgeable ("intermediates: ... animatics").
_GATE_PROTECTED_STAGE_SUFFIXES = ("_keyframes",)


@dataclass
class ProtectionIndex:
    """The protected sha set, with the reason each sha is protected."""

    reasons: dict[str, str] = field(default_factory=dict)

    def add(self, sha: str, reason: str) -> None:
        self.reasons.setdefault(sha, reason)

    def extend(self, shas: set[str] | list[str] | tuple[str, ...], reason: str) -> None:
        for sha in shas:
            self.add(sha, reason)

    def __contains__(self, sha: str) -> bool:
        return sha in self.reasons

    def get(self, sha: str) -> str | None:
        return self.reasons.get(sha)

    def __len__(self) -> int:
        return len(self.reasons)


def scan_json_shas(payload: Any) -> set[str]:
    """Every 64-hex token in a JSON-ish value (strings or nested structures)."""
    if payload is None:
        return set()
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return set()
    return set(SHA_RE.findall(payload))


def _columns(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    rows = db.connect().execute(sql, params).fetchall()
    return list(rows)


def _run_episode_statuses(db: Database) -> dict[str, str]:
    rows = _columns(db, "SELECT e.id AS id, e.status AS status FROM episodes e")
    return {str(row["id"]): str(row["status"]) for row in rows}


def _open_runs(db: Database) -> set[str]:
    """Run ids that are not finished: nothing they touched is garbage yet."""
    rows = _columns(db, "SELECT id, status FROM pipeline_runs")
    return {
        str(row["id"])
        for row in rows
        if str(row["status"]) not in ("completed", "failed", "halted")
    }


def _approved_gate_stages(db: Database) -> dict[str, str]:
    """stage → latest gate status, for every (run, stage) with a gate round."""
    rows = _columns(
        db,
        "SELECT run_id, stage, status, round FROM approval_gates ORDER BY run_id, stage, round",
    )
    latest: dict[tuple[str, str], str] = {}
    for row in rows:
        latest[(str(row["run_id"]), str(row["stage"]))] = str(row["status"])
    return {f"{run}:{stage}": status for (run, stage), status in latest.items()}


def build_protection_index(
    db: Database, *, purging_episode_id: str | None = None
) -> ProtectionIndex:
    """The protected set, ignoring the episode currently being purged.

    Rule 4 ("referenced by a non-published episode") must not protect the very
    episode whose files are being deleted — otherwise a purge could never plan
    anything for an episode that is still unpublished. Passing
    `purging_episode_id` excludes exactly that episode's own references, and
    keeps every *other* unpublished episode's references protected.
    """
    import sqlite3

    index = ProtectionIndex()
    conn = db.connect()

    # 1. permanent retention.
    for row in conn.execute("SELECT sha256 FROM assets WHERE retention = ?", (_PERMANENT,)):
        index.add(str(row["sha256"]), "retention:permanent")

    # 1b. the Series Bible itself: its CAS copy is referenced by every
    #     context pack, and a purge must never be able to remove it.
    for row in conn.execute("SELECT sha256 FROM assets WHERE kind = 'bible'"):
        index.add(str(row["sha256"]), "series_bible")

    # 2. approved character sheets (any series).
    for row in conn.execute(
        "SELECT asset_id FROM character_sheets WHERE status = 'approved'"
    ):
        index.add(str(row["asset_id"]), "character_sheet:approved")

    # 3. gated-stage outputs whose latest round is approved (G2 keyframes, G3 or
    #    G1 rounds on text stages), plus every stage output of an open run.
    gate_status = _approved_gate_stages(db)
    open_runs = _open_runs(db)
    episode_status = _run_episode_statuses(db)

    try:
        runs = conn.execute("SELECT id, episode_id, status FROM pipeline_runs").fetchall()
    except sqlite3.Error:  # pragma: no cover - schema always has pipeline_runs
        runs = []
    run_episode = {str(row["id"]): row["episode_id"] for row in runs}

    for row in conn.execute(
        "SELECT run_id, stage, status, output_json FROM stage_runs WHERE output_json IS NOT NULL"
    ):
        run_id = str(row["run_id"])
        stage = str(row["stage"])
        status = str(row["status"])
        if status != "succeeded":
            continue
        reason: str | None = None
        if run_id in open_runs:
            reason = "run:in_flight"
        elif (
            gate_status.get(f"{run_id}:{stage}") in ("approved", "auto_approved")
            and stage.endswith(_GATE_PROTECTED_STAGE_SUFFIXES)
        ):
            reason = f"gate_approved:{stage}"
        else:
            episode_id = run_episode.get(run_id)
            if (
                episode_id
                and str(episode_id) != purging_episode_id
                and episode_status.get(str(episode_id)) not in _OPEN_EPISODE_STATUSES
            ):
                reason = "episode:unpublished"
        if reason is not None:
            index.extend(scan_json_shas(row["output_json"]), reason)

    # 4. rows that link an asset to an episode (or a series) that is not closed.
    episode_shas = conn.execute(
        """
        SELECT nl.asset_sha256 AS sha, e.status AS status, e.id AS episode
          FROM narration_lines nl
          JOIN deliverables d ON d.id = nl.deliverable_id
          JOIN tracks t ON t.id = d.track_id
          JOIN episodes e ON e.id = t.episode_id
         WHERE nl.asset_sha256 IS NOT NULL
        UNION ALL
        SELECT nl.conformed_sha256, e.status, e.id
          FROM narration_lines nl
          JOIN deliverables d ON d.id = nl.deliverable_id
          JOIN tracks t ON t.id = d.track_id
          JOIN episodes e ON e.id = t.episode_id
         WHERE nl.conformed_sha256 IS NOT NULL
        UNION ALL
        SELECT sa.asset_id, e.status, e.id
          FROM shot_attempts sa
          JOIN shots s ON s.id = sa.shot_id
          JOIN tracks t ON t.id = s.track_id
          JOIN episodes e ON e.id = t.episode_id
         WHERE sa.asset_id IS NOT NULL
        UNION ALL
        SELECT sa.conformed_asset_id, e.status, e.id
          FROM shot_attempts sa
          JOIN shots s ON s.id = sa.shot_id
          JOIN tracks t ON t.id = s.track_id
          JOIN episodes e ON e.id = t.episode_id
         WHERE sa.conformed_asset_id IS NOT NULL
        UNION ALL
        SELECT am.master_sha256, e.status, e.id
          FROM audio_masters am
          JOIN deliverables d ON d.id = am.deliverable_id
          JOIN tracks t ON t.id = d.track_id
          JOIN episodes e ON e.id = t.episode_id
        UNION ALL
        SELECT am.narration_stem_sha256, e.status, e.id
          FROM audio_masters am
          JOIN deliverables d ON d.id = am.deliverable_id
          JOIN tracks t ON t.id = d.track_id
          JOIN episodes e ON e.id = t.episode_id
         WHERE am.narration_stem_sha256 IS NOT NULL
        UNION ALL
        SELECT ex.render_asset_id, e.status, e.id
          FROM exports ex
          JOIN deliverables d ON d.id = ex.deliverable_id
          JOIN tracks t ON t.id = d.track_id
          JOIN episodes e ON e.id = t.episode_id
        """
    ).fetchall()
    for row in episode_shas:
        if row["sha"] is None:
            continue
        if str(row["episode"]) == purging_episode_id:
            continue
        if str(row["status"]) not in _OPEN_EPISODE_STATUSES:
            index.add(str(row["sha"]), "episode:unpublished")

    return index


def is_protected(index: ProtectionIndex, asset: AssetRow) -> bool:
    if asset.retention == _PERMANENT:
        return True
    return asset.sha256 in index
