"""Mock image model: a single-frame PNG encoding the seed in its color."""

from __future__ import annotations

from pathlib import Path

from studio.core.ids import Ids
from studio.video.ffmpeg import run_ffmpeg

from .common import MockFaultPlan, MockJobBase, aspect_dimensions


class MockImageProvider(MockJobBase):
    name = "mock.image"

    def __init__(
        self,
        *,
        ledger_path: Path,
        width_16x9: int,
        height_16x9: int,
        fault_plan: MockFaultPlan | None = None,
        ids: Ids | None = None,
        poll_ticks: int | None = None,
    ) -> None:
        super().__init__(
            ledger_path=ledger_path, fault_plan=fault_plan, ids=ids, poll_ticks=poll_ticks
        )
        self.width_16x9 = width_16x9
        self.height_16x9 = height_16x9

    async def fetch(self, handle):  # type: ignore[no-untyped-def]
        from studio.providers.base import MediaPayload

        state = self._load_state()
        job = state.get("jobs", {}).get(handle.remote_request_id, {})
        out = self._render_dir() / f"{handle.remote_request_id}.png"
        if not out.exists():
            if self.fault_plan.zero_byte:
                out.write_bytes(b"")
            else:
                aspect = job.get("aspect")
                width, height = aspect_dimensions(
                    aspect, width_16x9=self.width_16x9, height_16x9=self.height_16x9
                )
                seed = job.get("seed") or 0
                color = f"0x{seed & 0xFFFFFF:06x}"
                await run_ffmpeg(
                    [
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s={width}x{height}",
                        "-frames:v",
                        "1",
                        "-bitexact",
                        "-fflags",
                        "+bitexact",
                        "-map_metadata",
                        "-1",
                        str(out),
                    ],
                    timeout_s=60.0,
                )
        self._record("fetch", remote_request_id=handle.remote_request_id)
        return [MediaPayload(local_path=out, ext="png", mime="image/png")]
