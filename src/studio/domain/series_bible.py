"""Series Bible domain model.

Content-addressed: `content_json` is the canonical JSON of the hashed core
(everything except series_id/version/provenance) and `sha256` is its hex
digest. Two drafts with identical wording produce identical bytes, hence an
identical sha — the S1 upsert uses this for idempotent versioning.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

_SHA256_LEN = 64


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CharacterBible(_Strict):
    name: str = Field(min_length=1)
    description: str = ""
    visual_tokens: list[str] = []
    arc: str = ""


class BibleContent(_Strict):
    """The hashed core of a bible version."""

    title: str = Field(min_length=1)
    premise: str = Field(min_length=1)
    world: str = ""
    characters: list[CharacterBible] = []
    style_tokens: list[str] = []
    tone: list[str] = []
    continuity_notes: str = ""


class SeriesBible(_Strict):
    series_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    title: str
    premise: str
    world: str = ""
    characters: list[CharacterBible] = []
    style_tokens: list[str] = []
    tone: list[str] = []
    continuity_notes: str = ""
    content_json: str
    sha256: str = Field(min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    created_by: str = "s1"
    created_at: str = ""

    @classmethod
    def from_content(
        cls, series_id: str, version: int, content: BibleContent, *, content_json: str | None = None
    ) -> SeriesBible:
        from studio.core.canonical import canonical_text

        canonical = (
            content_json
            if content_json is not None
            else canonical_text(content.model_dump(mode="json"))
        )
        sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(
            series_id=series_id,
            version=version,
            title=content.title,
            premise=content.premise,
            world=content.world,
            characters=content.characters,
            style_tokens=content.style_tokens,
            tone=content.tone,
            continuity_notes=content.continuity_notes,
            content_json=canonical,
            sha256=sha,
        )

    @classmethod
    def from_row(
        cls,
        *,
        series_id: str,
        version: int,
        content_json: str,
        content_sha256: str,
        created_by: str = "s1",
        created_at: str = "",
    ) -> SeriesBible:
        content = BibleContent.model_validate(json.loads(content_json))
        bible = cls.from_content(series_id, version, content, content_json=content_json)
        return bible.model_copy(
            update={
                "sha256": content_sha256,
                "created_by": created_by,
                "created_at": created_at,
            }
        )
