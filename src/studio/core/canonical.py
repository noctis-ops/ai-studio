"""Canonical JSON encoding used for cache keys, snapshots and hashes.

Guarantees: sorted keys, compact separators, UTF-8 with no ASCII escaping,
NFC-normalized strings. Arabic diacritics are never stripped, so different
tashkeel produces a different byte string (and therefore a different key).
Non-finite floats are rejected; sets/tuples normalize to lists (sets sorted by
their own canonical encoding); `Path` objects are rejected outright.
"""

from __future__ import annotations

import json
import math
import unicodedata
from pathlib import Path
from typing import Any

_REJECT_TYPES = (Path,)
_LEAF_TYPES = (str, int, float, bool, type(None))


def _normalize(obj: Any) -> Any:
    if isinstance(obj, _REJECT_TYPES):
        raise TypeError(
            "Path objects are not allowed in canonical JSON; pass a content sha256 instead"
        )
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError("NaN/Inf are not allowed in canonical JSON")
        return obj
    if isinstance(obj, dict):
        items: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"dict keys must be str, got {type(key).__name__}")
            nkey = unicodedata.normalize("NFC", key)
            items[nkey] = _normalize(value)
        return {k: items[k] for k in sorted(items)}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        encoded = [canonical_json(_normalize(v)) for v in obj]
        encoded.sort()
        return [json.loads(e.decode("utf-8")) for e in encoded]
    raise TypeError(f"unsupported type in canonical JSON: {type(obj).__name__}")


def canonical_json(obj: Any) -> bytes:
    normalized = _normalize(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(obj: Any) -> str:
    return canonical_json(obj).decode("utf-8")


def sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
