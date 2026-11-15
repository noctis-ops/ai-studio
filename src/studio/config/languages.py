"""The ONLY way stages obtain languages.

Stages receive their language list from the run context, which is built from
this function. Order is preserved end to end; duplicates are rejected.
"""

from __future__ import annotations

from collections.abc import Iterable

from studio.domain.enums import Language
from studio.domain.errors import ConfigError

from .settings import Settings


def _value(x: str | Language) -> str:
    return x.value if isinstance(x, Language) else str(x)


def resolve_languages(
    settings: Settings,
    episode_override: Iterable[str | Language] | None = None,
) -> list[Language]:
    raw: list[str] = (
        [_value(x) for x in episode_override]
        if episode_override is not None
        else [lang.value for lang in settings.languages]
    )
    if not raw:
        raise ConfigError("at least one language is required")
    seen: set[str] = set()
    out: list[Language] = []
    for value in raw:
        if value in seen:
            raise ConfigError(f"duplicate language in selection: {value!r}")
        seen.add(value)
        try:
            out.append(Language(value))
        except ValueError as exc:
            allowed = ", ".join(m.value for m in Language)
            raise ConfigError(f"unsupported language {value!r} (allowed: {allowed})") from exc
    return out
