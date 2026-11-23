"""S13 — export package (Phase 5 §K): free, no LLM, no network, atomic.

Per deliverable it writes one export directory:

```
<root>/<channel_key>/<YYYYMMDD>_<slug>/
    <slug>_<lang>_<aspect>.mp4
    metadata.json
    credits_licenses.txt
    thumbnail_candidates/frame_000123.png …
```

`requires_cleared_gates=True` puts the H0 barrier in front of it, so G1, G0, G2
and **G3 on S12** must all be approved against their current snapshots. A
re-render (a new sha) stales that G3 round and therefore blocks the export
until a human approves the new file (T-G3-01).

Nothing here regenerates text: the title/description/hashtags come from the
approved `metadata_safety` output, and the numbers come from the S11 render
report and the S12 QC report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.domain.enums import Language, Scope
from studio.domain.errors import PreflightError
from studio.domain.refs import SubjectRef
from studio.export.blocks import ExportInputs, collect_blocks
from studio.export.layout import (
    ExportTarget,
    date_str,
    leftover_temp_dirs,
    next_revision,
    publish_dir,
    remove_temp_dir,
    temp_dir_for,
)
from studio.export.sidecar import build_credits, build_metadata, write_metadata_json
from studio.export.slug import export_slug
from studio.export.thumbnails import extract_thumbnails
from studio.pipeline.stage import StageOutput
from studio.pipeline.stages_render import _deliverable_preset, deliverable_subjects

if TYPE_CHECKING:
    from studio.pipeline.context import RunContext

EXPORT_STAGE = "s13_export"
EXPORT_VERSION = "1"


def _sha(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_output(ctx: RunContext, stage: str, subject_id: str) -> dict[str, Any]:
    for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage=stage):
        if row.subject_id != subject_id or row.status != "succeeded":
            continue
        if not row.output_json:
            continue
        payload: dict[str, Any] = json.loads(row.output_json)
        return payload
    raise PreflightError(
        f"no successful {stage} output for deliverable {subject_id}; "
        f"S13 exports only an approved render"
    )


def _json_asset(ctx: RunContext, sha: str) -> dict[str, Any]:
    cas = ContentStore(Path(ctx.config.settings.paths.cas), ctx.db, ctx.clock)
    try:
        path = cas.resolve(sha)
    except Exception as exc:
        raise PreflightError(f"asset {sha} is missing from the CAS: {exc}") from exc
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


@dataclass
class ExportPlan:
    """Everything S13 needs, resolved before any directory is created."""

    deliverable_id: str
    channel_key: str
    language: str
    aspect: str
    slug: str
    profile: str
    root: Path
    preset: Any
    render_sha256: str
    render_path: Path
    frames: int
    fps: int
    metadata: dict[str, Any]
    qc_report_sha256: str
    qc_report: dict[str, Any]
    render_report: dict[str, Any]
    gate: Any
    checklist: dict[str, Any] | None
    models: list[dict[str, Any]]
    cues: list[dict[str, Any]]
    credits: list[str]
    attribution: list[str]
    cost: dict[str, Any]
    gates: list[dict[str, Any]]
    source_ref: str | None
    source_license: str | None
    episode_title: str
    problems: list[str] = field(default_factory=list)


def _models_for_episode(ctx: RunContext, episode_id: str) -> list[dict[str, Any]]:
    """Every settled model the episode called, with its commercial-use flag."""
    registry = ctx.config.registry.models
    rows = ctx.db.connect().execute(
        "SELECT model_key, provider, COUNT(*) AS calls FROM provider_calls "
        "WHERE episode_id = ? AND status IN ('succeeded', 'cached', 'succeeded_cached') "
        "GROUP BY model_key, provider ORDER BY model_key",
        (episode_id,),
    ).fetchall()
    models: list[dict[str, Any]] = []
    for row in rows:
        entry = registry.get(row["model_key"])
        models.append(
            {
                "role": entry.capability if entry is not None else "unknown",
                "model_key": row["model_key"],
                "provider": row["provider"],
                "commercial_use_verified": bool(
                    entry.commercial_use_verified if entry is not None else False
                ),
                "calls": int(row["calls"]),
            }
        )
    return models


def _cues_for_deliverable(ctx: RunContext, deliverable_id: str) -> tuple[
    list[dict[str, Any]], list[str], list[str]
]:
    """Cues with their licence state, the licence ids and attribution lines."""
    cues: list[dict[str, Any]] = []
    credits: set[str] = set()
    attribution: list[str] = []
    for cue in ctx.repos.audio.list_cues(deliverable_id):
        library = ctx.repos.audio.get_library_entry(cue.library_sha256)
        licensed = bool(library is not None and (library.license or "").strip())
        cues.append(
            {
                "kind": cue.kind,
                "library_sha256": cue.library_sha256,
                "title": library.title if library is not None else None,
                "license": library.license if library is not None else None,
                "licensed": licensed,
            }
        )
        if licensed and library is not None:
            assert library.license is not None
            credits.add(library.license)
            if library.attribution_required and library.attribution_text:
                attribution.append(library.attribution_text)
    return cues, sorted(credits), attribution


def _cost_by_stage(ctx: RunContext, episode_id: str) -> dict[str, Any]:
    rows = ctx.db.connect().execute(
        """
        SELECT COALESCE(sr.stage, 'episode') AS stage,
               COALESCE(SUM(pc.actual_cost_musd), 0) AS spent_musd
        FROM provider_calls pc
        LEFT JOIN stage_runs sr ON sr.id = pc.stage_run_id
        WHERE pc.episode_id = ?
        GROUP BY COALESCE(sr.stage, 'episode')
        ORDER BY stage
        """,
        (episode_id,),
    ).fetchall()
    by_stage = {row["stage"]: int(row["spent_musd"]) for row in rows}
    return {"total_musd": sum(by_stage.values()), "by_stage": by_stage}


def _gates_for_run(ctx: RunContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gate in ctx.repos.gates.list_for_run(ctx.run_id):
        if gate.status not in ("approved", "auto_approved"):
            continue
        out.append(
            {
                "gate": gate.gate,
                "stage": gate.stage,
                "round": gate.round,
                "status": gate.status,
                "decided_at": gate.decided_at,
                "snapshot_hash": gate.snapshot_hash,
            }
        )
    return sorted(out, key=lambda item: (item["stage"], item["round"]))


def _first_export_date(ctx: RunContext, deliverable_id: str) -> date:
    """The persisted first-export date, so re-runs never mint a new directory."""
    rows = ctx.repos.exports.list_for_deliverable(deliverable_id)
    if rows:
        try:
            return datetime.fromisoformat(rows[0].created_at.replace("Z", "+00:00")).date()
        except ValueError:  # pragma: no cover - ISO timestamps are CHECKed upstream
            pass
    # The run clock, not wall time: an export is reproducible under FakeClock.
    return ctx.clock.now().date()


def export_plan(ctx: RunContext, deliverable_id: str, *, profile: str) -> ExportPlan:
    """Free preflight: resolve every input S13 will need (no directory writes)."""
    settings = ctx.config.settings
    deliverable = ctx.repos.content.get_deliverable(deliverable_id)
    if deliverable is None:
        raise PreflightError(f"deliverable not found: {deliverable_id}")
    track = ctx.repos.content.get_track(deliverable.track_id)
    if track is None:
        raise PreflightError(f"track not found: {deliverable.track_id}")
    episode = ctx.repos.content.get_episode(track.episode_id)
    if episode is None:
        raise PreflightError(f"episode not found: {track.episode_id}")

    qc_output = _stage_output(ctx, "s12_render_qc", deliverable_id)
    render_sha = str(qc_output.get("render_sha256") or "")
    qc_sha = str(qc_output.get("qc_report_sha256") or "")
    if not render_sha or not qc_sha:
        raise PreflightError("the render QC output has no render/report sha")
    qc_report = _json_asset(ctx, qc_sha)
    render_report = _json_asset(ctx, str(qc_output.get("render_report_sha256") or ""))

    gate = ctx.repos.gates.latest(ctx.run_id, "s12_render_qc")
    checklist: dict[str, Any] | None = None
    if gate is not None and gate.checklist_json:
        payload = json.loads(gate.checklist_json)
        checklist = payload if isinstance(payload, dict) else None

    metadata_raw = deliverable.metadata_json or "{}"
    metadata = json.loads(metadata_raw)
    preset = _deliverable_preset(ctx, deliverable)
    channel_key = _channel_key(ctx, deliverable.channel_id)
    models = _models_for_episode(ctx, episode.id)
    cues, credits, attribution = _cues_for_deliverable(ctx, deliverable_id)

    root, _kind = (Path(settings.paths.home) / settings.export.root, "final")
    if profile != "final":
        root = Path(settings.paths.home) / settings.export.draft_root

    render_path = CAS_PATH(ctx, render_sha)
    return ExportPlan(
        deliverable_id=deliverable_id,
        channel_key=channel_key,
        language=deliverable.language,
        aspect=str(track.aspect),
        slug=export_slug(episode.title, episode.id),
        profile=profile,
        root=root,
        preset=preset,
        render_sha256=render_sha,
        render_path=render_path,
        frames=int(qc_output.get("frames") or 0),
        fps=int(qc_output.get("fps") or settings.render.fps),
        metadata=metadata,
        qc_report_sha256=qc_sha,
        qc_report=qc_report,
        render_report=render_report,
        gate=gate,
        checklist=checklist,
        models=models,
        cues=cues,
        credits=credits,
        attribution=attribution,
        cost=_cost_by_stage(ctx, episode.id),
        gates=_gates_for_run(ctx),
        source_ref=getattr(episode, "source_ref", None),
        source_license=getattr(episode, "source_license", None),
        episode_title=episode.title,
    )


def CAS_PATH(ctx: RunContext, sha: str) -> Path:
    cas = ContentStore(Path(ctx.config.settings.paths.cas), ctx.db, ctx.clock)
    try:
        return cas.resolve(sha)
    except Exception as exc:
        raise PreflightError(f"render asset {sha} is missing from the CAS: {exc}") from exc


def _channel_key(ctx: RunContext, channel_id: str) -> str:
    """The channel key from the config (ids are keys in this codebase)."""
    channels = ctx.config.channels.channels
    if channel_id in channels:
        return channel_id
    raise PreflightError(
        f"deliverable channel {channel_id!r} is not in channel_profiles.yaml "
        f"(known: {', '.join(sorted(channels))})"
    )


def _render_block(plan: ExportPlan, file_name: str) -> dict[str, Any]:
    measured = plan.qc_report.get("measured", {})
    streams = plan.qc_report.get("streams", {}).get("value", {}) or {}
    video = (streams.get("video") or [{}])[0]
    audio = (streams.get("audio") or [{}])[0]
    geometry = plan.render_report.get("geometry", [])
    adjusted = [item for item in geometry if float(item.get("crop_loss_pct", 0)) > 0]
    av_sync_check: dict[str, Any] = next(
        (c for c in plan.qc_report.get("checks", []) if c.get("name") == "av_sync"), {}
    )
    render_path = plan.render_path
    return {
        "file": file_name,
        "sha256": plan.render_sha256,
        "bytes": render_path.stat().st_size if render_path.exists() else 0,
        "duration_s": measured.get("video_duration_s"),
        "frames": plan.frames,
        "fps": plan.fps,
        "width": int(plan.preset.width),
        "height": int(plan.preset.height),
        "video_codec": video.get("codec_name"),
        "encoder": plan.render_report.get("encoder"),
        "audio_codec": audio.get("codec_name"),
        "sample_rate": audio.get("sample_rate"),
        "integrated_lufs": measured.get("lufs"),
        "true_peak_dbtp": measured.get("true_peak_dbtp"),
        "av_offset_ms": av_sync_check.get("value"),
        "geometry_adjust": {
            "shots": len(geometry),
            "adjusted_shots": len(adjusted),
            "max_crop_loss_pct": max(
                (float(item.get("crop_loss_pct", 0)) for item in geometry), default=0.0
            ),
        },
    }


def _audience_block(plan: ExportPlan) -> dict[str, Any]:
    checklist = plan.checklist or {}
    made_for_kids = checklist.get("audience_made_for_kids")
    return {
        "made_for_kids": made_for_kids if isinstance(made_for_kids, bool) else None,
        "checklist": checklist,
        "gate_id": plan.gate.id if plan.gate is not None else None,
        "round": plan.gate.round if plan.gate is not None else None,
    }


def _is_complete(directory: Path, *, render_sha256: str) -> bool:
    """Idempotence test: the directory already holds this exact export."""
    metadata_path = directory / "metadata.json"
    if not metadata_path.exists():
        return False
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    render = payload.get("render") or {}
    return render.get("sha256") == render_sha256


def _recorded_export_notes(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(overrides, export_flags) as the sidecar currently records them."""
    try:
        payload = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    overrides = payload.get("overrides")
    flags = payload.get("export_flags")
    return (
        overrides if isinstance(overrides, list) else [],
        flags if isinstance(flags, list) else [],
    )


def _patch_export_notes(
    directory: Path, *, overrides: list[dict[str, Any]], flags: list[dict[str, Any]]
) -> None:
    """Rewrite only the override/flag notes of a complete export, atomically.

    An operator override or a late checklist fix must land in the sidecar of the
    directory that already exists — the render did not change, so a new `_rN`
    revision would be a lie.
    """
    sidecar_path = directory / "metadata.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["overrides"] = overrides
    payload["export_flags"] = flags
    write_metadata_json(sidecar_path, payload)
    files = payload.get("files")
    if isinstance(files, dict) and "metadata.json" in files:
        files["metadata.json"] = _file_sha256(sidecar_path)
        write_metadata_json(sidecar_path, payload)


def _target_for(ctx: RunContext, plan: ExportPlan) -> tuple[ExportTarget, bool]:
    """(target, reuse) — reuse when this render already has a directory."""
    existing = ctx.repos.exports.get(plan.deliverable_id, plan.render_sha256)
    if existing is not None and Path(existing.dir_path).exists():
        path = Path(existing.dir_path)
        target = ExportTarget(
            root=path.parent.parent,
            channel_key=path.parent.name,
            date_str=path.name.split("_", 1)[0],
            slug=plan.slug,
            revision=_revision_of(path.name, plan.slug),
        )
        return target, True
    first = _first_export_date(ctx, plan.deliverable_id)
    day = date_str(first)
    revision = next_revision(
        plan.root, channel_key=plan.channel_key, date_str_value=day, slug=plan.slug
    )
    return (
        ExportTarget(
            root=plan.root,
            channel_key=plan.channel_key,
            date_str=day,
            slug=plan.slug,
            revision=revision,
        ),
        False,
    )


def _revision_of(dir_name: str, slug: str) -> int:
    suffix = f"_{slug}"
    marker = f"{suffix}_r"
    if marker in dir_name:
        try:
            return int(dir_name.rsplit(marker, 1)[1])
        except (IndexError, ValueError):  # pragma: no cover - defensive
            return 1
    return 1


class ExportHandler:
    """S13: approved render → atomic export directory + sidecar."""

    _STAGE = EXPORT_STAGE

    def __init__(
        self,
        *,
        override_commercial_use: bool = False,
        override_reason: str | None = None,
    ) -> None:
        self.override_commercial_use = override_commercial_use
        self.override_reason = override_reason

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return deliverable_subjects(ctx)

    def _profile(self, ctx: RunContext) -> str:
        run = ctx.repos.pipeline.get_run(ctx.run_id)
        return run.profile if run is not None else "final"

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        profile = self._profile(ctx)
        try:
            plan = export_plan(ctx, subject.id, profile=profile)
        except Exception as exc:  # run() reports the real reason
            return _sha({"stage": self._STAGE, "error": str(exc), "profile": profile})
        else:
            return _sha(
                {
                    "stage": self._STAGE,
                    "export_version": EXPORT_VERSION,
                    "deliverable_id": subject.id,
                    "profile": profile,
                    "render_sha256": plan.render_sha256,
                    "qc_report_sha256": plan.qc_report_sha256,
                    "gate_id": plan.gate.id if plan.gate is not None else None,
                    "gate_round": plan.gate.round if plan.gate is not None else None,
                    "gate_snapshot": plan.gate.snapshot_hash if plan.gate is not None else None,
                    "metadata": plan.metadata,
                    "preset": {
                        "aspect": plan.preset.aspect.value,
                        "width": plan.preset.width,
                        "height": plan.preset.height,
                        "fps": plan.preset.fps,
                    },
                    "override": [
                        self.override_commercial_use,
                        self.override_reason or "",
                    ],
                }
            )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        settings = ctx.config.settings
        profile = self._profile(ctx)
        plan = export_plan(ctx, subject.id, profile=profile)

        blocks = collect_blocks(
            ExportInputs(
                profile=profile,
                models=plan.models,
                cues=plan.cues,
                source_ref=plan.source_ref,
                source_license=plan.source_license,
                checklist=plan.checklist,
                qc_status=str(plan.qc_report.get("status") or "pass"),
            ),
            override_commercial_use=self.override_commercial_use,
            override_reason=self.override_reason,
        )
        if profile == "final" and blocks.blockers:
            raise PreflightError(
                "export blocked; nothing was written:\n- "
                + "\n- ".join(f"{b.code}: {b.detail}" for b in blocks.blockers)
            )
        for override in blocks.overrides:
            ctx.repos.audit.append(
                actor="operator",
                entity=f"deliverable:{subject.id}",
                event="export_override",
                payload={"rule": override["rule"], "reason": override["reason"],
                         "models": override["models"], "run_id": ctx.run_id},
            )
        for flag in blocks.flags():
            ctx.repos.audit.append(
                actor="export",
                entity=f"deliverable:{subject.id}",
                event="export_flag",
                payload={**flag, "profile": profile, "run_id": ctx.run_id},
            )

        target, reuse = _target_for(ctx, plan)
        if reuse and _is_complete(target.path, render_sha256=plan.render_sha256):
            recorded = _recorded_export_notes(target.path)
            if recorded != (blocks.overrides, blocks.flags()):
                await asyncio.to_thread(
                    _patch_export_notes,
                    target.path,
                    overrides=blocks.overrides,
                    flags=blocks.flags(),
                )
            output = self._output(plan, target, blocks, reused=True)
            return StageOutput(output=output)

        # Leftovers from a crashed run are removed before a new scratch dir.
        for leftover in leftover_temp_dirs(plan.root):
            remove_temp_dir(leftover)

        file_name = f"{plan.slug}_{plan.language}_{plan.aspect}.mp4"
        tmp = temp_dir_for(target)
        try:
            await asyncio.to_thread(tmp.mkdir, parents=True, exist_ok=False)
            copied = tmp / file_name
            await asyncio.to_thread(shutil.copyfile, plan.render_path, copied)
            actual = await asyncio.to_thread(_file_sha256, copied)
            if actual != plan.render_sha256:
                raise PreflightError(
                    f"copied render sha256 {actual} does not match the approved "
                    f"render {plan.render_sha256}"
                )
            thumbs_dir = tmp / "thumbnail_candidates"
            thumbs = await extract_thumbnails(
                plan.render_path,
                thumbs_dir,
                total_frames=plan.frames,
                count=int(settings.export.thumbnails),
            )
            files = {file_name: plan.render_sha256}
            for thumb in thumbs:
                files[thumb.relative_to(tmp).as_posix()] = await asyncio.to_thread(
                    _file_sha256, thumb
                )

            render_block = _render_block(plan, file_name)
            title = str(plan.metadata.get("title") or plan.episode_title)
            description = str(plan.metadata.get("description") or "")
            tags = plan.metadata.get("tags")
            hashtags = [str(tag) for tag in tags] if isinstance(tags, list) else []
            metadata_payload = build_metadata(
                channel_key=plan.channel_key,
                language=plan.language,
                aspect=plan.aspect,
                preset={
                    "width": plan.preset.width,
                    "height": plan.preset.height,
                    "fps": plan.preset.fps,
                    "video": plan.preset.video.model_dump(mode="json"),
                    "audio": plan.preset.audio.model_dump(mode="json"),
                },
                profile=profile,
                title=title,
                description=description,
                hashtags=hashtags,
                audience=_audience_block(plan),
                ai_disclosure=bool(
                    (plan.checklist or {}).get("ai_disclosure", True) is not False
                ),
                source={
                    "source_ref": plan.source_ref,
                    "source_license": plan.source_license,
                },
                render=render_block,
                qc={
                    "status": plan.qc_report.get("status"),
                    "warnings": [
                        check.get("name")
                        for check in plan.qc_report.get("checks", [])
                        if check.get("status") == "warn"
                    ],
                },
                credits=plan.credits,
                models=plan.models,
                cost=plan.cost,
                gates=plan.gates,
                overrides=blocks.overrides,
                export_flags=blocks.flags(),
                files=files,
            )
            sidecar_path = tmp / "metadata.json"
            write_metadata_json(sidecar_path, metadata_payload)
            files["metadata.json"] = await asyncio.to_thread(_file_sha256, sidecar_path)

            credits_path = tmp / "credits_licenses.txt"
            credits_path.write_text(
                build_credits(
                    title=title,
                    credits=plan.credits,
                    models=plan.models,
                    attribution=plan.attribution,
                ),
                encoding="utf-8",
            )
            files["credits_licenses.txt"] = await asyncio.to_thread(
                _file_sha256, credits_path
            )

            if target.path.exists():
                # A previous attempt at this exact path: replace it only after
                # the new directory is complete and fsynced.
                await asyncio.to_thread(shutil.rmtree, target.path)
            await asyncio.to_thread(publish_dir, tmp, target.path)
        except Exception:
            remove_temp_dir(tmp)
            raise

        ctx.repos.exports.create(
            deliverable_id=subject.id,
            render_asset_id=plan.render_sha256,
            dir_path=str(target.path),
            profile=profile,
            status="written",
        )
        ctx.repos.audit.append(
            actor="export",
            entity=f"deliverable:{subject.id}",
            event="export_written",
            payload={
                "run_id": ctx.run_id,
                "dir": str(target.path),
                "render_sha256": plan.render_sha256,
                "qc_report_sha256": plan.qc_report_sha256,
                "profile": profile,
                "files": files,
                "flags": blocks.flags(),
                "overrides": blocks.overrides,
            },
        )
        output = self._output(plan, target, blocks, reused=False)
        output["files"] = files
        warning = target.path_warning()
        if warning:
            output.setdefault("warnings", []).append(warning)
        return StageOutput(output=output)

    def _output(
        self,
        plan: ExportPlan,
        target: ExportTarget,
        blocks: Any,
        *,
        reused: bool,
    ) -> dict[str, Any]:
        return {
            "deliverable_id": plan.deliverable_id,
            "dir_path": str(target.path),
            "dir_name": target.dir_name,
            "revision": target.revision,
            "profile": plan.profile,
            "channel_key": plan.channel_key,
            "language": plan.language,
            "aspect": plan.aspect,
            "slug": plan.slug,
            "render_sha256": plan.render_sha256,
            "qc_report_sha256": plan.qc_report_sha256,
            "qc_status": plan.qc_report.get("status"),
            "frames": plan.frames,
            "export_flags": blocks.flags(),
            "overrides": blocks.overrides,
            "blocked": bool(plan.profile == "final" and blocks.blockers),
            "reused": reused,
            "files": {},
            "warnings": [],
        }


def export_specs() -> list[Any]:  # pragma: no cover - convenience for tests
    from studio.pipeline.specs import production_specs

    return [spec for spec in production_specs() if spec.name == EXPORT_STAGE]


def _language_of(deliverable: Any) -> Language:  # pragma: no cover - small helper
    return Language(deliverable.language)


__all__ = [
    "EXPORT_STAGE",
    "EXPORT_VERSION",
    "ExportHandler",
    "ExportPlan",
    "Scope",
    "export_plan",
]
