"""D3/H3 CI capability check: ffmpeg >= 5.0 + audio and visual filter sets.

Shares `studio.video.ffmpeg.capability_check` with `studio doctor` and
tests/test_ffmpeg_capabilities.py so CI, doctor and tests can never drift.
"""

from __future__ import annotations

import asyncio
import sys

from studio.video.ffmpeg import (
    REQUIRED_ENCODERS,
    REQUIRED_FILTERS,
    REQUIRED_MUXERS,
    assembly_capability_check,
    capability_check,
)


async def main() -> int:
    version, missing = await capability_check()
    missing_filters, missing_encoders, missing_muxers = await assembly_capability_check()
    missing = [*missing, *missing_filters]
    if version < (5, 0, 0):
        found = ".".join(str(part) for part in version)
        print(f"FAIL: ffmpeg >= 5.0 required, found {found}")
        return 1
    if missing:
        print(f"FAIL: ffmpeg is missing required filters: {', '.join(missing)}")
        return 1
    if missing_encoders:
        print(f"FAIL: ffmpeg is missing required encoders: {', '.join(missing_encoders)}")
        return 1
    if missing_muxers:
        print(f"FAIL: ffmpeg is missing required muxers: {', '.join(missing_muxers)}")
        return 1
    found = ".".join(str(part) for part in version)
    print(
        f"OK: ffmpeg {found}; {len(set(REQUIRED_FILTERS))} required filters, "
        f"{len(REQUIRED_ENCODERS)} encoders and {len(REQUIRED_MUXERS)} muxers present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
