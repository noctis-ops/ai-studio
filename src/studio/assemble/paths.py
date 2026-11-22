"""The single escaping helper for ffmpeg list files and filter arguments.

Two different grammars, two different rules, both tested on Linux against
Windows-style inputs (drive letters, backslashes) as pure string tests:

* **concat demuxer list files** quote each path in single quotes, so only the
  single quote itself needs escaping (``'\''``). A literal newline is refused;
  there is no escape for it.
* **filter graph option values** are parsed twice (graph, then option), so
  ``\\ : ' [ ] , ;`` all need a backslash. A Windows drive colon is therefore
  escaped as ``C\\:/...``, which is exactly what ffmpeg expects there.
"""

from __future__ import annotations

from pathlib import Path

from studio.domain.errors import MediaToolError

_FILTER_SPECIAL = ("\\", ":", "'", "[", "]", ",", ";")


def _normalize_separators(path: str) -> str:
    """Windows backslashes become forward slashes: ffmpeg accepts both."""
    return path.replace("\\", "/")


def escape_concat_path(path: str | Path) -> str:
    text = _normalize_separators(str(path))
    if "\n" in text or "\r" in text:
        raise MediaToolError(f"path contains a newline and cannot be listed: {text!r}")
    return text.replace("'", "'\\''")


def concat_line(path: str | Path) -> str:
    return f"file '{escape_concat_path(path)}'"


def write_concat_list(list_path: Path, parts: list[Path]) -> Path:
    list_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(concat_line(p) for p in parts) + "\n"
    list_path.write_text(body, encoding="utf-8")
    return list_path


def escape_filter_path(path: str | Path) -> str:
    """Escape a path for use as an ffmpeg filter option value."""
    text = _normalize_separators(str(path))
    for char in _FILTER_SPECIAL:
        text = text.replace(char, "\\" + char)
    return text


def escape_filter_arg(value: str) -> str:
    """Escape a non-path filter option value (same double-parsing rule)."""
    for char in _FILTER_SPECIAL:
        value = value.replace(char, "\\" + char)
    return value
