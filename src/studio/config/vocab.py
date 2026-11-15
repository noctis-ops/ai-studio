"""Controlled vocabularies (moods, shot types, camera moves, intensity).

Loaded and validated in Phase 1; stages start consuming them in Phase 2.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from studio.domain.errors import ConfigError

INTENSITY_DOMAIN = [1, 2, 3, 4, 5]


class Vocab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moods: list[str]
    shot_types: list[str]
    camera_moves: list[str]
    intensity: list[int]


def _check_list(name: str, values: list[str], source: str) -> None:
    if not values:
        raise ConfigError(f"{source}: {name} must be non-empty")
    numeric = [v for v in values if not v.strip()]
    if numeric:
        raise ConfigError(f"{source}: {name} contains blank entries")
    if len(set(values)) != len(values):
        raise ConfigError(f"{source}: {name} contains duplicates")


def load_vocab_file(path: Path) -> Vocab:
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
        vocab = Vocab.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "<root>"
            detail = str(first.get("msg", exc))
        else:
            loc, detail = "<root>", str(exc)
        raise ConfigError(f"{source}: {loc}: {detail}") from exc
    _check_list("moods", vocab.moods, source)
    _check_list("shot_types", vocab.shot_types, source)
    _check_list("camera_moves", vocab.camera_moves, source)
    if vocab.intensity != INTENSITY_DOMAIN:
        raise ConfigError(f"{source}: intensity must be exactly [1, 2, 3, 4, 5]")
    return vocab
