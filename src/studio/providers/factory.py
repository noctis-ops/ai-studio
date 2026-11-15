"""Provider factory.

`provider == "mock"` returns a mock configured from settings (unchanged
Phase 1 behavior). `anthropic` / `openai` bind lazily to the Phase 2 LLM
adapters: the adapter module imports cleanly without the SDK, and a missing
SDK surfaces as ConfigError("LLM adapter '<name>' requires ai-studio[providers]")
at client construction time — never at import time, never with a secret.
Anything else is a ConfigError naming the provider and the phase that will
implement it.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from studio.config.registry import ModelEntry
from studio.config.settings import Settings
from studio.core.ids import Ids
from studio.domain.errors import ConfigError

from .mock.common import MockFaultPlan
from .mock.image import MockImageProvider
from .mock.llm import MockLLMProvider
from .mock.tts import MockTTSProvider
from .mock.video import MockVideoProvider

# provider name -> (module, class). Imported lazily in _build_real.
_REAL_LLM_ADAPTERS: dict[str, tuple[str, str]] = {
    "anthropic": ("studio.providers.anthropic_llm", "AnthropicLLMProvider"),
    "openai": ("studio.providers.openai_llm", "OpenAILLMProvider"),
}

_FUTURE_PROVIDERS: dict[str, str] = {
    "elevenlabs": "Phase 4 (TTS)",
    "fal": "Phase 4 (image/video)",
}


def build_provider(
    entry: ModelEntry,
    *,
    settings: Settings,
    fault_plan: MockFaultPlan | None = None,
    ids: Ids | None = None,
) -> Any:
    if entry.provider == "mock":
        return _build_mock(entry, settings=settings, fault_plan=fault_plan, ids=ids)
    binding = _REAL_LLM_ADAPTERS.get(entry.provider)
    if binding is not None:
        if entry.capability != "llm":
            raise ConfigError(
                f"provider {entry.provider!r} adapter only supports capability "
                f"'llm', got {entry.capability!r}"
            )
        module_name, class_name = binding
        module = importlib.import_module(module_name)
        adapter_cls = getattr(module, class_name)
        return adapter_cls(entry)
    future = _FUTURE_PROVIDERS.get(entry.provider)
    if future is not None:
        raise ConfigError(
            f"provider {entry.provider!r} is not implemented in this phase ({future}); "
            f"no adapter is registered for capability {entry.capability!r}"
        )
    known = ", ".join(sorted(["mock", *_REAL_LLM_ADAPTERS, *_FUTURE_PROVIDERS]))
    raise ConfigError(f"unknown provider {entry.provider!r} (known: {known})")


def _build_mock(
    entry: ModelEntry,
    *,
    settings: Settings,
    fault_plan: MockFaultPlan | None,
    ids: Ids | None,
) -> Any:
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
    raise ConfigError(f"no mock provider registered for capability {capability!r}")
