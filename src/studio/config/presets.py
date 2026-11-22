"""Export presets (Phase 5 §F), keyed by channel key.

``configs/export_presets.yaml`` must carry exactly the same channel keys as
``configs/channel_profiles.yaml`` (T-EXP-04). A preset also has to agree with
its channel's aspect, otherwise the export can never satisfy its own render QC.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.config.channels import ChannelsConfig
from studio.domain.enums import Aspect
from studio.domain.errors import ConfigError


class PresetVideo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maxrate_k: int = Field(ge=1)
    bufsize_k: int = Field(ge=1)


class PresetAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codec: Literal["aac"] = "aac"
    bitrate_k: int = Field(default=192, ge=32)
    sample_rate: int = Field(default=48000, ge=8000)
    channels: int = Field(default=2, ge=1, le=2)


class PresetLimits(BaseModel):
    """⚠ unverified platform limits: WARN-level checks only."""

    model_config = ConfigDict(extra="forbid")

    max_duration_s: int | None = Field(default=None, ge=1)
    title_max_chars: int | None = Field(default=None, ge=1)
    description_max_bytes: int | None = Field(default=None, ge=1)
    hashtags_max: int | None = Field(default=None, ge=1)


class ExportPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aspect: Aspect
    width: int = Field(ge=2)
    height: int = Field(ge=2)
    fps: int = Field(default=24, ge=1)
    video: PresetVideo
    audio: PresetAudio = PresetAudio()
    limits: PresetLimits = PresetLimits()

    @property
    def portrait(self) -> bool:
        return self.height > self.width


class ExportPresets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: dict[str, ExportPreset]

    def for_channel(self, channel_key: str) -> ExportPreset:
        try:
            return self.channels[channel_key]
        except KeyError as exc:
            raise ConfigError(
                f"no export preset for channel {channel_key!r}; "
                f"known channels: {', '.join(sorted(self.channels))}"
            ) from exc


def validate_presets_against_channels(
    presets: ExportPresets, channels: ChannelsConfig
) -> None:
    preset_keys = set(presets.channels)
    channel_keys = set(channels.channels)
    if preset_keys != channel_keys:
        missing = sorted(channel_keys - preset_keys)
        extra = sorted(preset_keys - channel_keys)
        detail = []
        if missing:
            detail.append(f"missing presets for {missing}")
        if extra:
            detail.append(f"presets without a channel {extra}")
        raise ConfigError("export presets do not match channel_profiles.yaml: " + "; ".join(detail))
    for key, preset in presets.channels.items():
        channel = channels.channels[key]
        if preset.aspect != channel.aspect:
            raise ConfigError(
                f"export preset {key!r} aspect {preset.aspect.value} does not match "
                f"channel aspect {channel.aspect.value}"
            )


def load_export_presets_file(path: Path, *, channels: ChannelsConfig) -> ExportPresets:
    source = str(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    try:
        presets = ExportPresets.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "<root>"
            detail = str(first.get("msg", exc))
        else:
            loc, detail = "<root>", str(exc)
        raise ConfigError(f"{source}: {loc}: {detail}") from exc
    if not presets.channels:
        raise ConfigError(f"{source}: channels must be non-empty")
    validate_presets_against_channels(presets, channels)
    return presets
