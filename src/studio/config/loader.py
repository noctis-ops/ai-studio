"""Aggregate loader: settings + registry + vocab + channels, failing fast.

Every error carries the file name and the field path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .channels import ChannelsConfig, load_channels_file
from .registry import ModelRegistry, load_registry_file
from .settings import Settings, load_settings_file_with_report
from .vocab import Vocab, load_vocab_file


@dataclass(frozen=True)
class AppConfig:
    home: Path
    settings: Settings
    registry: ModelRegistry
    vocab: Vocab
    channels: ChannelsConfig
    # H5.1: deprecated keys that were accepted-and-ignored (audit + doctor rows).
    config_notes: tuple[str, ...] = ()


def load_all(home: Path) -> AppConfig:
    home = Path(home)
    settings_path = home / "configs" / "settings.yaml"
    registry_path = home / "configs" / "model_registry.yaml"
    vocab_path = home / "configs" / "vocab.yaml"
    channels_path = home / "configs" / "channel_profiles.yaml"
    settings, notes = load_settings_file_with_report(settings_path)
    settings = settings.resolved_paths(home)
    registry = load_registry_file(registry_path)
    vocab = load_vocab_file(vocab_path)
    channels = load_channels_file(channels_path)
    return AppConfig(
        home=home,
        settings=settings,
        registry=registry,
        vocab=vocab,
        channels=channels,
        config_notes=tuple(notes),
    )
