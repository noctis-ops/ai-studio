"""Library scan and validation (PHASE3 §H3).

Walks ``<library_root>/{music,sfx,ambience}`` and reconciles the files with
``index.yaml`` sidecars into the ``audio_library`` table. Key rules:

- The sidecar is the source of truth for metadata; the filesystem is the
  source of truth for existence and bytes.
- Upsert is keyed by ``sha256``; a renamed file updates ``rel_path``.
- A file that disappears from disk keeps its row (``audio_cues`` reference
  it) but flips to ``active=0`` with ``scan_error='file missing'``.
- ``active=1`` only when: license present, tags/intensity valid against the
  controlled vocabulary (moods for music, ambience/sfx tags otherwise),
  file readable, duration > 0, loudness measured (or NULL but not silence).
- The scan never aborts on one bad entry; it collects a report.
- Idempotent: re-running the scan makes no changes when nothing changed.

``moods_json`` is the single controlled-vocabulary tag column for all kinds
(documented here and in 004_audio.sql): moods for music, ambience/sfx tags
for the other two kinds.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.config.vocab import Vocab
from studio.db.repos.audio import AudioRepo
from studio.domain.errors import MediaToolError
from studio.domain.models import AudioLibraryRow
from studio.video.ffmpeg import probe_duration_seconds

if TYPE_CHECKING:
    from studio.config.settings import Settings

KINDS = ("music", "sfx", "ambience")
INDEX_FILE = "index.yaml"


def resolve_library_root(settings: Settings) -> Path:
    """Absolute library root.

    ``audio.library_root`` may be absolute (used as-is) or relative to the
    studio home (``paths.home``), matching how the demo seeds and the
    ``library scan`` CLI lay files out.
    """
    root = Path(str(settings.audio.library_root)).expanduser()
    if root.is_absolute():
        return root
    return Path(str(settings.paths.home)).expanduser() / root

_SCAN_OK = "ok"


class LibraryEntry(BaseModel):
    """One sidecar entry (per file). Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    file: str
    title: str = Field(min_length=1)
    moods: list[str] | None = None
    tags: list[str] | None = None
    intensity: int | None = Field(default=None, ge=1, le=5)
    bpm: int | None = Field(default=None, ge=1, le=400)
    loopable: bool = False
    license: str | None = None
    license_url: str | None = None
    attribution_required: bool = False
    attribution_text: str | None = None
    source: str | None = None

    def tag_list(self) -> list[str]:
        return list(self.moods or self.tags or [])


class LibrarySidecar(BaseModel):
    """A ``index.yaml`` sidecar: optional ``defaults`` plus per-file ``tracks``."""

    model_config = ConfigDict(extra="forbid")

    defaults: dict[str, Any] = {}
    tracks: list[LibraryEntry] = []


@dataclass
class ScanReport:
    scanned: int = 0
    activated: int = 0
    updated: int = 0
    missing_on_disk: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, scan_error)

    @property
    def ok(self) -> bool:
        return not self.invalid and not self.missing_on_disk


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_sidecar(path: Path) -> tuple[LibrarySidecar | None, str | None]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, f"sidecar unreadable: {exc}"
    if data is None:
        return LibrarySidecar(), None
    if not isinstance(data, dict):
        return None, "sidecar must be a mapping at the top level"
    try:
        merged: list[dict[str, Any]] = []
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, dict):
            return None, "sidecar defaults: must be a mapping"
        raw_tracks = data.get("tracks") or []
        if not isinstance(raw_tracks, list):
            return None, "sidecar tracks: must be a list"
        for raw in raw_tracks:
            if not isinstance(raw, dict):
                return None, "sidecar tracks entries must be mappings"
            entry = {**defaults, **raw}
            merged.append(entry)
        tracks = [LibraryEntry.model_validate(e) for e in merged]
        return LibrarySidecar(defaults=defaults, tracks=tracks), None
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "<root>"
            return None, f"sidecar validation: {loc}: {first.get('msg')}"
        return None, f"sidecar validation: {exc}"


def _validate_entry(
    entry: LibraryEntry, kind: str, vocab: Vocab
) -> tuple[list[str], int | None, str | None]:
    """Returns (tags, intensity, scan_error)."""
    tags = entry.tag_list()
    intensity: int | None
    if kind == "music":
        known = set(vocab.moods)
        if entry.intensity is None:
            return tags, None, "music entry requires intensity (1-5)"
        tag_label = "moods"
        intensity = entry.intensity
    else:
        known = set(vocab.sfx_tags if kind == "sfx" else vocab.ambience_tags)
        tag_label = f"{kind} tags"
        intensity = entry.intensity  # optional for sfx/ambience
    if not tags:
        return tags, intensity, f"entry requires non-empty {tag_label}"
    unknown = [tag for tag in tags if tag not in known]
    if unknown:
        return tags, intensity, f"unknown {tag_label}: {', '.join(unknown)}"
    return tags, intensity, None


async def scan_library(
    *,
    library_root: Path,
    repos: AudioRepo,
    vocab: Vocab,
    scan_extensions: tuple[str, ...] = (".wav", ".mp3", ".flac", ".ogg"),
    measure_loudness: bool = True,
) -> ScanReport:
    """Reconcile the on-disk library with the audio_library table."""

    report = ScanReport()
    seen_shas: set[str] = set()

    for kind in KINDS:
        kind_dir = library_root / kind
        if not kind_dir.is_dir():
            continue
        sidecar_path = kind_dir / INDEX_FILE
        sidecar: LibrarySidecar | None = None
        sidecar_error: str | None = None
        if sidecar_path.exists():
            sidecar, sidecar_error = _load_sidecar(sidecar_path)
        else:
            sidecar_error = "missing index.yaml"

        entries: list[LibraryEntry] = list(sidecar.tracks) if sidecar else []
        files_on_disk = {
            p.name: p
            for p in sorted(kind_dir.iterdir())
            if p.is_file() and p.suffix.lower() in scan_extensions
        }
        listed = {entry.file for entry in entries}
        if sidecar_error is not None:
            # whole directory is untrusted: register disk files as inactive
            for name, path in files_on_disk.items():
                await _register_invalid(
                    repos=repos, kind=kind, path=path, name=name,
                    scan_error=sidecar_error, report=report,
                )
                seen_shas.add(_sha256_file(path))
            continue

        for entry in entries:
            rel_path = f"{kind}/{entry.file}"
            disk = files_on_disk.get(entry.file)
            if disk is None or not disk.is_file():
                # keep the row, flip it inactive
                existing = await _find_by_rel_path(repos, rel_path)
                if existing is not None:
                    repos.set_library_active(existing.sha256, active=False,
                                             scan_error="file missing")
                    report.missing_on_disk.append(rel_path)
                else:
                    report.invalid.append((rel_path, "file missing and never scanned"))
                continue
            sha = _sha256_file(disk)
            seen_shas.add(sha)
            report.scanned += 1
            await _scan_one(
                repos=repos, vocab=vocab, kind=kind, entry=entry, path=disk,
                sha256=sha, rel_path=rel_path, report=report,
                measure_loudness=measure_loudness,
            )

        # files on disk but not listed in the sidecar: register inactive
        for name, path in files_on_disk.items():
            if name in listed:
                continue
            sha = _sha256_file(path)
            seen_shas.add(sha)
            await _register_invalid(
                repos=repos, kind=kind, path=path, name=name,
                scan_error="not listed in index.yaml", report=report,
            )

    # rows whose file vanished entirely and which were not seen this scan
    for row in repos.list_library(include_inactive=True):
        if row.sha256 not in seen_shas and row.active:
            repos.set_library_active(row.sha256, active=False, scan_error="file missing")
            report.missing_on_disk.append(row.rel_path)

    return report


async def _register_invalid(
    *, repos: AudioRepo, kind: str, path: Path, name: str, scan_error: str,
    report: ScanReport,
) -> None:
    sha = _sha256_file(path)
    rel_path = f"{kind}/{name}"
    try:
        duration_ms = round((await probe_duration_seconds(str(path))) * 1000)
    except MediaToolError:
        duration_ms = 0
    repos.upsert_library_entry(
        sha256=sha, rel_path=rel_path, kind=kind, title=name,
        moods_json="[]", intensity=None, bpm=None, loopable=0,
        duration_ms=duration_ms, loudness_lufs=None,
        license=None, license_url=None, source=None,
        attribution_required=0, attribution_text=None,
        active=0, scan_error=scan_error,
    )
    report.invalid.append((rel_path, scan_error))


async def _find_by_rel_path(
    repos: AudioRepo, rel_path: str
) -> AudioLibraryRow | None:
    for row in repos.list_library(include_inactive=True):
        if row.rel_path == rel_path:
            return row
    return None


async def _scan_one(
    *, repos: AudioRepo, vocab: Vocab, kind: str, entry: LibraryEntry,
    path: Path, sha256: str, rel_path: str, report: ScanReport,
    measure_loudness: bool,
) -> None:
    from studio.audio.analysis import integrated_loudness

    tags, intensity, scan_error = _validate_entry(entry, kind, vocab)
    if scan_error is None and not (entry.license and entry.license.strip()):
        scan_error = "license missing"
    loudness: float | None = None
    try:
        duration_ms = round((await probe_duration_seconds(str(path))) * 1000)
    except MediaToolError:
        duration_ms = 0
        scan_error = scan_error or "duration probe failed"
    if duration_ms <= 0:
        scan_error = scan_error or "zero duration"
    elif scan_error is None and measure_loudness:
        try:
            summary = await integrated_loudness(str(path))
            loudness = summary.integrated_lufs
        except MediaToolError:
            scan_error = "loudness measurement failed"

    active = 0 if scan_error else 1
    existing = repos.get_library_entry(sha256)
    repos.upsert_library_entry(
        sha256=sha256, rel_path=rel_path, kind=kind, title=entry.title,
        moods_json=json.dumps(tags, ensure_ascii=False),
        intensity=intensity, bpm=entry.bpm, loopable=int(entry.loopable),
        duration_ms=duration_ms, loudness_lufs=loudness,
        license=entry.license, license_url=entry.license_url, source=entry.source,
        attribution_required=int(entry.attribution_required),
        attribution_text=entry.attribution_text,
        active=active, scan_error=scan_error,
    )
    if active:
        if existing is None:
            report.activated += 1
        else:
            report.updated += 1
    else:
        report.invalid.append((rel_path, scan_error or "invalid"))
