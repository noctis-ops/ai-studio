"""D3: ffmpeg capability gate shared by CI and `studio doctor`.

The same `studio.video.ffmpeg.capability_check()` helper backs both this
test and the doctor output so the two can never drift.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ffmpeg


@pytest.mark.asyncio
async def test_ffmpeg_version_at_least_5() -> None:
    from studio.video.ffmpeg import ffmpeg_version

    version = await ffmpeg_version()
    assert version >= (5, 0, 0), f"ffmpeg >= 5.0 required, found {version}"


@pytest.mark.asyncio
async def test_ffmpeg_has_required_audio_filters() -> None:
    from studio.video.ffmpeg import REQUIRED_FILTERS, capability_check

    version, missing = await capability_check()
    assert version >= (5, 0, 0)
    assert missing == [], (
        f"ffmpeg is missing required filters: {missing} "
        f"(required: {sorted(set(REQUIRED_FILTERS))})"
    )
