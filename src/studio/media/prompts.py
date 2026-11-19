"""Deterministic media prompt composition (Phase 4 §G).

No LLM is involved: the same (bible, shot, templates) always yields the same
bytes. The sha256 of the four template files is the "prompt version sha"
carried in every media cache key (``MediaRequest.prompt_version_sha``).

Layout: ``locked_prefix + scene_text + locked_suffix``. Only ``scene_text``
is sanitizable; locked parts are byte-identical across sanitize levels
(T-PRM-01). Visual prompts are English only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.domain.errors import ConfigError

_TEMPLATE_NAMES = ("keyframe", "motion", "negative", "sanitizer")


@dataclass(frozen=True)
class PromptSpec:
    locked_prefix: str
    scene_text: str
    locked_suffix: str
    negative: str
    prompt_version_sha: str

    @property
    def full_prompt(self) -> str:
        parts = [p for p in (self.locked_prefix, self.scene_text, self.locked_suffix) if p]
        return "\n".join(parts)

    def with_scene(self, scene_text: str) -> PromptSpec:
        """Same locked parts, new scene text (sanitizer output)."""
        return PromptSpec(
            locked_prefix=self.locked_prefix,
            scene_text=scene_text,
            locked_suffix=self.locked_suffix,
            negative=self.negative,
            prompt_version_sha=self.prompt_version_sha,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "locked_prefix": self.locked_prefix,
            "scene_text": self.scene_text,
            "locked_suffix": self.locked_suffix,
            "negative": self.negative,
            "prompt_version_sha": self.prompt_version_sha,
            "full_prompt": self.full_prompt,
        }


def _templates_dir(home: Path | None) -> Path:
    if home is not None:
        return Path(home) / "configs" / "prompts" / "media"
    import os

    return Path(os.environ.get("STUDIO_HOME", ".")).resolve() / "configs" / "prompts" / "media"


def load_templates(home: Path | None = None) -> dict[str, str]:
    directory = _templates_dir(home)
    templates: dict[str, str] = {}
    for name in _TEMPLATE_NAMES:
        path = directory / f"{name}.txt"
        try:
            templates[name] = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(f"media prompt template {name!r} not found at {path}") from exc
        except OSError as exc:
            raise ConfigError(f"media prompt template {name!r} unreadable: {exc}") from exc
    return templates


def prompt_version_sha(home: Path | None = None) -> str:
    templates = load_templates(home)
    blob = "\x00".join(templates[name] for name in _TEMPLATE_NAMES)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _style_text(style_tokens: list[str]) -> str:
    return ", ".join(token.strip() for token in style_tokens if token.strip())


def compose_keyframe_prompt(
    *,
    style_tokens: list[str],
    characters: list[tuple[str, str]],
    shot_visual: str,
    ref_clause_template: str | None,
    ref_clauses: list[tuple[str, int]] | None = None,
    home: Path | None = None,
) -> PromptSpec:
    """Keyframe prompt: style lock + per-character specs + ref clauses, then
    the shot's visual description as the (only) scene text.

    ``ref_clauses`` lists (character_name, reference_image_number) pairs in
    the order the refs are actually sent to the model (after truncation);
    when omitted, every character gets one clause in shot order.
    """
    templates = load_templates(home)
    version_sha = hashlib.sha256(
        "\x00".join(templates[name] for name in _TEMPLATE_NAMES).encode("utf-8")
    ).hexdigest()
    prefix_parts: list[str] = []
    style = _style_text(style_tokens)
    if style:
        prefix_parts.append(f"Series style: {style}.")
    for name, visual in characters:
        visual = visual.strip()
        prefix_parts.append(f"{name}: {visual}" if visual else f"{name}:")
    if ref_clause_template:
        entries = ref_clauses if ref_clauses is not None else [
            (name, index) for index, (name, _visual) in enumerate(characters, start=1)
        ]
        for name, index in entries:
            prefix_parts.append(ref_clause_template.format(name=name, n=index))
    scene = shot_visual.strip()
    return PromptSpec(
        locked_prefix="\n".join(prefix_parts),
        scene_text=scene,
        locked_suffix=templates["keyframe"].strip(),
        negative=templates["negative"].strip(),
        prompt_version_sha=version_sha,
    )


def compose_motion_prompt(
    *,
    shot_visual: str,
    camera: str = "",
    characters: list[str] | None = None,
    home: Path | None = None,
) -> str:
    """Motion sentence composed deterministically from S2 shot fields.

    S2 has no motion/action/mood columns (Discovery Report §C), so the motion
    prompt is derived from the visual description plus the camera move.
    """
    templates = load_templates(home)
    base = shot_visual.strip().rstrip(".")
    camera = camera.strip()
    motion = f"{base}. Gentle natural motion" if base else "Gentle natural motion"
    if camera:
        motion += f" with a {camera} camera move"
    motion += ". " + templates["motion"].strip()
    return motion


def compose_motion_prompt_spec(
    *,
    motion: str,
    home: Path | None = None,
) -> PromptSpec:
    """Wrap a motion sentence in a PromptSpec (sanitizer target + cache key)."""
    templates = load_templates(home)
    return PromptSpec(
        locked_prefix="",
        scene_text=motion.strip(),
        locked_suffix=templates["motion"].strip(),
        negative=templates["negative"].strip(),
        prompt_version_sha=prompt_version_sha(home),
    )


def compose_sheet_prompt(
    *,
    character_name: str,
    visual_spec: str,
    role: str,
    style_tokens: list[str],
    ref_clause_template: str | None = None,
    ref_clauses: list[tuple[str, int]] | None = None,
    home: Path | None = None,
) -> PromptSpec:
    """Character-sheet prompt (S8a): single-view full-body neutral pose.

    When ``ref_clause_template`` is given (non-primary roles conditioned on
    an approved ref_full sheet), one clause line is appended per
    ``ref_clauses`` entry (default: one clause for this character at n=1).
    """
    templates = load_templates(home)
    version_sha = hashlib.sha256(
        "\x00".join(templates[name] for name in _TEMPLATE_NAMES).encode("utf-8")
    ).hexdigest()
    style = _style_text(style_tokens)
    prefix = f"Character reference sheet ({role}) for {character_name}: {visual_spec.strip()}"
    if style:
        prefix += f"\nSeries style: {style}."
    if ref_clause_template:
        entries = ref_clauses if ref_clauses is not None else [(character_name, 1)]
        for name, index in entries:
            prefix += "\n" + ref_clause_template.format(name=name, n=index)
    scene = (
        f"Single full-body view of {character_name}, neutral standing pose, "
        "plain solid backdrop, even studio lighting, no other characters, "
        "no text, no props."
    )
    return PromptSpec(
        locked_prefix=prefix,
        scene_text=scene,
        locked_suffix=templates["keyframe"].strip(),
        negative=templates["negative"].strip(),
        prompt_version_sha=version_sha,
    )


def seed_for(episode_id: str, shot_id: str, nonce: str) -> int:
    """§F seed: int(sha256(f"{episode_id}:{shot_id}:{nonce}")[:8], 16) % 2**31."""
    digest = hashlib.sha256(f"{episode_id}:{shot_id}:{nonce}".encode()).hexdigest()
    return int(digest[:8], 16) % (2**31)
