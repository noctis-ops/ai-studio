"""Content-addressed cache keys for provider calls.

`Absent optionals are encoded as null, never omitted` — every field is always
present so two calls differ exactly when one of their inputs differ.
"""

from __future__ import annotations

import hashlib
from typing import Any

from studio.core.canonical import canonical_json

_FIELDS = (
    "capability",
    "model_key",
    "model_ref",
    "params",
    "input_assets",
    "prompt_version_sha",
    "seed",
    "nonce",
)


def make_cache_key(
    capability: str,
    model_key: str,
    model_ref: str | None,
    params: dict[str, Any],
    input_asset_sha256s: list[str] | tuple[str, ...],
    prompt_version_sha: str | None,
    seed: int | None,
    nonce: str,
) -> str:
    payload = {
        "capability": capability,
        "model_key": model_key,
        "model_ref": model_ref,
        "params": params,
        "input_assets": sorted(input_asset_sha256s),
        "prompt_version_sha": prompt_version_sha,
        "seed": seed,
        "nonce": nonce,
    }
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return digest
