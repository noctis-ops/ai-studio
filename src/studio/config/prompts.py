"""Prompt file loading with provenance.

Prompt files live in `<STUDIO_HOME>/configs/prompts/<name>.txt` (a copy of the
repository `configs/prompts/` is materialized into every studio home by
`studio init`/test fixtures). Loading is deterministic: the same file bytes
always yield the same (text, sha256) pair, and the sha is what pipeline
input hashes carry.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from studio.domain.errors import ConfigError

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def load_prompt(name: str, *, home: Path | None = None) -> tuple[str, str]:
    """Return (text, sha256) for `configs/prompts/<name>.txt`.

    `home` defaults to STUDIO_HOME (falling back to cwd, mirroring the CLI).
    Raises ConfigError on a malformed name, a missing file, or a file that
    is not valid UTF-8. Never returns a partial/empty prompt silently.
    """
    if not _NAME_RE.fullmatch(name):
        raise ConfigError(
            f"invalid prompt name {name!r} (allowed: lowercase letters, digits, underscore)"
        )
    if home is None:
        import os

        home = Path(os.environ.get("STUDIO_HOME", ".")).resolve()
    path = Path(home) / "configs" / "prompts" / f"{name}.txt"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"prompt {name!r} not found at {path}") from exc
    except OSError as exc:
        raise ConfigError(f"prompt {name!r} unreadable at {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"prompt {name!r} at {path} is not valid UTF-8: {exc}") from exc
    if not text.strip():
        raise ConfigError(f"prompt {name!r} at {path} is empty")
    return text, hashlib.sha256(raw).hexdigest()
