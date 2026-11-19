"""Deterministic reference preparation (Phase 4 §F).

CAS assets are re-encoded to JPEG with the long side capped at
``media.ref_max_side_px`` via ffmpeg. The *prepared* sha (not the original)
goes into the provider cache key, so identical bytes always yield the same
sha and the same key (T-FAL-05).
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path

from studio.config.settings import Settings
from studio.core.cas import ContentStore
from studio.domain.refs import AssetRef
from studio.video.ffmpeg import run_ffmpeg


@dataclass(frozen=True)
class PreparedRef:
    sha256: str
    data_uri: str
    local_path: Path
    ref: AssetRef


async def prepare_ref(
    cas: ContentStore,
    ref: AssetRef,
    settings: Settings,
) -> PreparedRef:
    """Re-encode one CAS asset to a capped JPEG and store it in the CAS."""
    src = cas.resolve(ref.sha256)
    max_side = int(settings.media.ref_max_side_px)
    quality = int(settings.media.ref_jpeg_quality)
    with tempfile.NamedTemporaryFile(prefix="studio-ref-", suffix=".jpg", delete=False) as handle:
        tmp = Path(handle.name)
    try:
        await run_ffmpeg(
            [
                "-y",
                "-i",
                str(src),
                "-vf",
                f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
                "-q:v",
                str(max(2, 31 - quality // 3)),
                "-frames:v",
                "1",
                str(tmp),
            ],
            timeout_s=60.0,
        )
        stored = cas.put_file(tmp, kind="image", ext="jpg", retention="intermediate", move=True)
    finally:
        await asyncio.to_thread(tmp.unlink, missing_ok=True)
    prepared_path = cas.resolve(stored.sha256)
    encoded = base64.b64encode(await asyncio.to_thread(prepared_path.read_bytes)).decode("ascii")
    data_uri = "data:image/jpeg;base64," + encoded
    return PreparedRef(
        sha256=stored.sha256,
        data_uri=data_uri,
        local_path=prepared_path,
        ref=stored,
    )


async def prepare_refs(
    cas: ContentStore,
    refs: list[AssetRef],
    settings: Settings,
) -> list[PreparedRef]:
    return [await prepare_ref(cas, ref, settings) for ref in refs]
