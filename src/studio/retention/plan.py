"""Purge planning: what *could* go, per class, and why (Phase 5 §M).

Planning is pure with respect to the CAS: it reads rows, computes the protection
index and returns items with the rule that selected each one. `purge plan` and a
dry `purge run` print exactly this; nothing is ever deleted here.

Classes (`PurgeClass`) and their rules:

- `raw_clip`         a `video` asset with `retention = 'keep_n_days'` (raw provider
                     output) older than `retention.keep_raw_clips_days`
- `narration_audio`  an `audio` asset with `retention = 'keep_n_days'` (raw TTS)
                     older than `retention.keep_narration_audio_days`
- `rejected_attempt` asset of a `shot_attempts` row whose QC failed, older than
                     `retention.rejected_attempt_days`
- `intermediate`     `retention = 'intermediate'` and not protected

An asset is only eligible when it belongs to the episode being purged (through
any of the linking tables or a stage output of one of its runs) *and* is not
protected — a shared file that another unpublished episode needs stays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from studio.db.connection import Database
from studio.domain.models import AssetRow

from .protect import ProtectionIndex, build_protection_index, scan_json_shas

_CLASS_ORDER = ("raw_clip", "narration_audio", "rejected_attempt", "intermediate")


@dataclass(frozen=True)
class PurgeItem:
    sha256: str
    rel_path: str
    size_bytes: int
    purge_class: str
    rule: str
    kind: str
    created_at: str


@dataclass
class PurgePlan:
    episode_id: str
    items: list[PurgeItem] = field(default_factory=list)
    protected: int = 0
    protected_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    def by_class(self) -> dict[str, list[PurgeItem]]:
        out: dict[str, list[PurgeItem]] = {}
        for item in self.items:
            out.setdefault(item.purge_class, []).append(item)
        return {name: out[name] for name in _CLASS_ORDER if name in out} | {
            name: items for name, items in out.items() if name not in _CLASS_ORDER
        }

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "class": name,
                "count": len(items),
                "bytes": sum(item.size_bytes for item in items),
                "rules": sorted({item.rule for item in items}),
            }
            for name, items in self.by_class().items()
        ]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _now(clock: Any) -> datetime:
    from typing import cast

    return cast(datetime, clock.now())


def _episode_assets(db: Database, episode_id: str) -> set[str]:
    """Every sha this episode can be shown to own, directly or via a stage output."""
    conn = db.connect()
    shas: set[str] = set()
    rows = conn.execute(
        """
        SELECT nl.asset_sha256 AS sha FROM narration_lines nl
          JOIN deliverables d ON d.id = nl.deliverable_id
          JOIN tracks t ON t.id = d.track_id WHERE t.episode_id = ?
        UNION SELECT nl.conformed_sha256 FROM narration_lines nl
          JOIN deliverables d ON d.id = nl.deliverable_id
          JOIN tracks t ON t.id = d.track_id WHERE t.episode_id = ?
        UNION SELECT sa.asset_id FROM shot_attempts sa
          JOIN shots s ON s.id = sa.shot_id
          JOIN tracks t ON t.id = s.track_id WHERE t.episode_id = ?
        UNION SELECT sa.conformed_asset_id FROM shot_attempts sa
          JOIN shots s ON s.id = sa.shot_id
          JOIN tracks t ON t.id = s.track_id WHERE t.episode_id = ?
        UNION SELECT am.master_sha256 FROM audio_masters am
          JOIN deliverables d ON d.id = am.deliverable_id
          JOIN tracks t ON t.id = d.track_id WHERE t.episode_id = ?
        UNION SELECT am.narration_stem_sha256 FROM audio_masters am
          JOIN deliverables d ON d.id = am.deliverable_id
          JOIN tracks t ON t.id = d.track_id WHERE t.episode_id = ?
        UNION SELECT ex.render_asset_id FROM exports ex
          JOIN deliverables d ON d.id = ex.deliverable_id
          JOIN tracks t ON t.id = d.track_id WHERE t.episode_id = ?
        """,
        (episode_id,) * 7,
    ).fetchall()
    for row in rows:
        if row["sha"]:
            shas.add(str(row["sha"]))

    for row in conn.execute(
        """
        SELECT sr.output_json FROM stage_runs sr
          JOIN pipeline_runs r ON r.id = sr.run_id
         WHERE r.episode_id = ? AND sr.output_json IS NOT NULL AND sr.status = 'succeeded'
        """,
        (episode_id,),
    ):
        shas |= scan_json_shas(row["output_json"])
    return shas


def _rejected_attempt_shas(db: Database, episode_id: str) -> dict[str, str]:
    """sha → why it is a rejected attempt, for this episode."""
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT sa.asset_id AS sha, sa.qc_status AS qc, sa.created_at AS created_at
          FROM shot_attempts sa
          JOIN shots s ON s.id = sa.shot_id
          JOIN tracks t ON t.id = s.track_id
         WHERE t.episode_id = ? AND sa.asset_id IS NOT NULL
        """,
        (episode_id,),
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        if str(row["qc"]) in ("fail", "failed_policy"):
            out[str(row["sha"])] = f"shot_attempt qc_status={row['qc']}"
    return out


def build_plan(
    repos: Any,
    db: Database,
    *,
    episode_id: str,
    settings: Any,
    clock: Any,
    index: ProtectionIndex | None = None,
) -> PurgePlan:
    """Decide what a `purge run` may delete for one episode (nothing deleted)."""
    from studio.domain.errors import StudioError

    episode = repos.content.get_episode(episode_id)
    if episode is None:
        raise StudioError(f"episode not found: {episode_id}")

    retention = settings.retention
    now = _now(clock)
    raw_cutoff = now - timedelta(days=int(retention.keep_raw_clips_days))
    tts_cutoff = now - timedelta(days=int(retention.keep_narration_audio_days))
    rejected_cutoff = now - timedelta(days=int(retention.rejected_attempt_days))

    protection = (
        index
        if index is not None
        else build_protection_index(db, purging_episode_id=episode_id)
    )
    owned = _episode_assets(db, episode_id)
    rejected = _rejected_attempt_shas(db, episode_id)

    plan = PurgePlan(episode_id=episode_id)
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM assets WHERE purged_at IS NULL ORDER BY created_at, sha256"
    ).fetchall()
    for raw in rows:
        asset = AssetRow.model_validate(dict(raw))
        if asset.sha256 not in owned:
            continue
        reason = protection.get(asset.sha256)
        if reason is not None:
            plan.protected += 1
            plan.protected_reasons[reason] = plan.protected_reasons.get(reason, 0) + 1
            continue
        created = _parse_time(asset.created_at)

        purge_class: str | None = None
        rule = ""
        if asset.sha256 in rejected and created is not None and created <= rejected_cutoff:
            purge_class = "rejected_attempt"
            rule = (
                f"{rejected[asset.sha256]} older than "
                f"retention.rejected_attempt_days={retention.rejected_attempt_days}"
            )
        elif (
            asset.kind == "video"
            and asset.retention == "keep_n_days"
            and created is not None
            and created <= raw_cutoff
        ):
            purge_class = "raw_clip"
            rule = (
                "raw video older than "
                f"retention.keep_raw_clips_days={retention.keep_raw_clips_days}"
            )
        elif (
            asset.kind == "audio"
            and asset.retention == "keep_n_days"
            and created is not None
            and created <= tts_cutoff
        ):
            purge_class = "narration_audio"
            rule = (
                "raw TTS older than "
                f"retention.keep_narration_audio_days={retention.keep_narration_audio_days}"
            )
        elif asset.retention == "intermediate":
            purge_class = "intermediate"
            rule = "retention=intermediate and unreferenced by any live work"

        if purge_class is None:
            continue
        plan.items.append(
            PurgeItem(
                sha256=asset.sha256,
                rel_path=asset.rel_path,
                size_bytes=asset.size_bytes,
                purge_class=purge_class,
                rule=rule,
                kind=asset.kind,
                created_at=asset.created_at,
            )
        )
    return plan


def gc_plan(db: Database, *, settings: Any, clock: Any) -> dict[str, list[str]]:
    """Orphans a `purge gc` may remove: unreferenced CAS files, scratch, `.tmp-*`.

    Never deletes anything; returns the candidate paths grouped by reason.
    """
    from pathlib import Path

    cas_root = Path(settings.paths.cas)
    tmp_root = Path(settings.paths.home) / "data" / "tmp"
    export_root = Path(settings.paths.home) / settings.export.root
    draft_root = Path(settings.paths.home) / settings.export.draft_root

    conn = db.connect()
    known = {
        str(row["sha256"]) for row in conn.execute("SELECT sha256 FROM assets").fetchall()
    }
    known_paths = {
        str(row["rel_path"]) for row in conn.execute("SELECT rel_path FROM assets").fetchall()
    }

    out: dict[str, list[str]] = {"cas_orphan": [], "scratch": [], "export_tmp": []}
    if cas_root.is_dir():
        for path in sorted(cas_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(cas_root).as_posix()
            if rel in known_paths:
                continue
            if path.stem in known or path.name.split(".")[0] in known:
                continue
            out["cas_orphan"].append(str(path))
    if tmp_root.is_dir():
        for path in sorted(tmp_root.rglob("*")):
            if path.is_file():
                out["scratch"].append(str(path))
    for root in {export_root.parent, draft_root.parent}:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(".tmp-*")):
            if path.is_dir():
                out["export_tmp"].append(str(path))
    return out
