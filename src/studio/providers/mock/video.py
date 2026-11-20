"""Mock i2v model: deterministic testsrc2 MP4 at the track's aspect."""

from __future__ import annotations

from pathlib import Path

from studio.core.ids import Ids
from studio.video.ffmpeg import run_ffmpeg

from .common import MockFaultPlan, MockJobBase, aspect_dimensions


class MockVideoProvider(MockJobBase):
    name = "mock.video"

    def __init__(
        self,
        *,
        ledger_path: Path,
        width_16x9: int,
        height_16x9: int,
        fps: int,
        fault_plan: MockFaultPlan | None = None,
        ids: Ids | None = None,
        poll_ticks: int | None = None,
        fps_override: int | None = None,
        duration_offset_s: float = 0.0,
    ) -> None:
        super().__init__(
            ledger_path=ledger_path, fault_plan=fault_plan, ids=ids, poll_ticks=poll_ticks
        )
        self.width_16x9 = width_16x9
        self.height_16x9 = height_16x9
        self.fps = fps
        # Test-only variants (§F): exercise the S10 conform path with a
        # wrong-fps or slightly-off-duration raw clip. Defaults are exact.
        self.fps_override = fps_override
        self.duration_offset_s = float(duration_offset_s)

    async def fetch(self, handle):  # type: ignore[no-untyped-def]
        from studio.providers.base import MediaPayload

        state = self._load_state()
        job = state.get("jobs", {}).get(handle.remote_request_id, {})
        out = self._render_dir() / f"{handle.remote_request_id}.mp4"
        if not out.exists():
            if self.fault_plan.zero_byte:
                out.write_bytes(b"")
            else:
                aspect = job.get("aspect")
                width, height = aspect_dimensions(
                    aspect, width_16x9=self.width_16x9, height_16x9=self.height_16x9
                )
                duration_s = float(job.get("duration_s") or 5.0) + self.duration_offset_s
                duration_s = max(0.1, duration_s)
                fps = self.fps_override or self.fps
                await run_ffmpeg(
                    [
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration_s:.3f}",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-bitexact",
                        "-fflags",
                        "+bitexact",
                        "-map_metadata",
                        "-1",
                        str(out),
                    ],
                    timeout_s=180.0,
                )
        self._record("fetch", remote_request_id=handle.remote_request_id)
        return [MediaPayload(local_path=out, ext="mp4", mime="video/mp4")]
