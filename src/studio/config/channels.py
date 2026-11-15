"""Channel profiles: one channel per (language, aspect) pair."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from studio.domain.enums import Aspect, Language
from studio.domain.errors import ConfigError


class ChannelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language
    aspect: Aspect
    platform: str


class ChannelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: dict[str, ChannelProfile]

    def for_pair(self, language: Language, aspect: Aspect) -> str:
        for name, profile in self.channels.items():
            if profile.language == language and profile.aspect == aspect:
                return name
        raise ConfigError(
            f"no channel configured for language={language.value} aspect={aspect.value}"
        )


def load_channels_file(path: Path) -> ChannelsConfig:
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
        channels = ChannelsConfig.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "<root>"
            detail = str(first.get("msg", exc))
        else:
            loc, detail = "<root>", str(exc)
        raise ConfigError(f"{source}: {loc}: {detail}") from exc
    if not channels.channels:
        raise ConfigError(f"{source}: channels must be non-empty")
    return channels


def _coerce_language(value: Language | str) -> Language:
    if isinstance(value, Language):
        return value
    try:
        return Language(str(value))
    except ValueError as exc:
        raise ConfigError(f"unsupported language {value!r}") from exc


def _coerce_aspect(value: Aspect | str) -> Aspect:
    if isinstance(value, Aspect):
        return value
    try:
        return Aspect(str(value))
    except ValueError as exc:
        raise ConfigError(f"unsupported aspect {value!r}") from exc


def channel_for(
    language: Language | str,
    aspect: Aspect | str,
    channels: ChannelsConfig | None = None,
) -> str:
    """Map a (language, aspect) pair to its configured channel id.

    `channels` defaults to the profiles under `$STUDIO_HOME/configs`; tests and
    the CLI pass the loaded config explicitly.
    """
    if channels is None:
        home = Path(os.environ.get("STUDIO_HOME", "."))
        channels = load_channels_file(home / "configs" / "channel_profiles.yaml")
    return channels.for_pair(_coerce_language(language), _coerce_aspect(aspect))
