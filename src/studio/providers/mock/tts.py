"""Mock ElevenLabs-style TTS: a deterministic sine tone as a WAV file.

Duration is `chars / tts_chars_per_second`, floored at 0.5 s, so clip planning
can be exercised without a real speech API.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from studio.core.ids import Ids
from studio.providers.base import MediaPayload, TTSRequest, TTSResult
from studio.video.ffmpeg import run_ffmpeg

from .common import MockBase, MockFaultPlan

MIN_DURATION_S = 0.5
_FREQ_BASE_HZ = 220
_FREQ_SPAN_HZ = 440


class MockTTSProvider(MockBase):
    def __init__(
        self,
        *,
        chars_per_second: int,
        workdir: Path | None = None,
        fault_plan: MockFaultPlan | None = None,
        ids: Ids | None = None,
    ) -> None:
        super().__init__(fault_plan, ids=ids)
        if chars_per_second <= 0:
            raise ValueError("chars_per_second must be > 0")
        self.chars_per_second = chars_per_second
        self._workdir = (
            Path(workdir)
            if workdir is not None
            else Path(tempfile.mkdtemp(prefix="studio-mock-tts-"))
        )

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        await self._tick()
        chars = len(req.text)
        duration_s = max(MIN_DURATION_S, chars / self.chars_per_second)
        digest = hashlib.sha256(req.text.encode("utf-8")).hexdigest()
        freq_hz = _FREQ_BASE_HZ + int(digest[:8], 16) % _FREQ_SPAN_HZ
        out = self._workdir / f"tts-{digest[:16]}.wav"
        if self.fault_plan.zero_byte:
            out.write_bytes(b"")
        else:
            self._workdir.mkdir(parents=True, exist_ok=True)
            await run_ffmpeg(
                [
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={freq_hz}:duration={duration_s:.3f}",
                    "-ar",
                    "24000",
                    "-ac",
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
        self._record(
            "synthesize",
            chars=chars,
            duration_s=duration_s,
            language=req.language.value,
        )
        return TTSResult(
            payload=MediaPayload(local_path=out, ext="wav", mime="audio/wav"),
            billed_chars=chars,
        )
