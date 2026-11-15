"""Provider factory.

`provider == "mock"` returns a mock configured from settings; every real
provider is a Phase 2/4 deliverable and raises ProviderNotImplementedError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from studio.config.registry import ModelEntry
from studio.config.settings import Settings
from studio.core.ids import Ids
from studio.domain.errors import ProviderNotImplementedError

from .mock.common import MockFaultPlan
from .mock.image import MockImageProvider
from .mock.llm import MockLLMProvider
from .mock.tts import MockTTSProvider
from .mock.video import MockVideoProvider


def build_provider(
    entry: ModelEntry,
    *,
    settings: Settings,
    fault_plan: MockFaultPlan | None = None,
    ids: Ids | None = None,
) -> Any:
    if entry.provider != "mock":
        raise ProviderNotImplementedError(
            f"{entry.provider} adapter is implemented in Phase 2 (LLM/TTS) or Phase 4 (fal)"
        )
    home = Path(settings.paths.home)
    mock_dir = home / "data" / "mock"
    mock = settings.mock
    capability = entry.capability
    if capability == "llm":
        return MockLLMProvider(fault_plan, ids=ids)
    if capability == "tts":
        return MockTTSProvider(
            chars_per_second=mock.tts_chars_per_second,
            workdir=mock_dir / "tts",
            fault_plan=fault_plan,
            ids=ids,
        )
    if capability == "image_reference_generate":
        return MockImageProvider(
            ledger_path=mock_dir / "image_jobs.json",
            width_16x9=mock.video_width_16x9,
            height_16x9=mock.video_height_16x9,
            fault_plan=fault_plan,
            ids=ids,
            poll_ticks=mock.poll_ticks,
        )
    if capability == "image_to_video":
        return MockVideoProvider(
            ledger_path=mock_dir / "video_jobs.json",
            width_16x9=mock.video_width_16x9,
            height_16x9=mock.video_height_16x9,
            fps=mock.fps,
            fault_plan=fault_plan,
            ids=ids,
            poll_ticks=mock.poll_ticks,
        )
    raise ProviderNotImplementedError(f"no mock provider registered for capability {capability!r}")
