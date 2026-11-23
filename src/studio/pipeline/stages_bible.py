"""S14 — bible proposals as an engine node (Phase 5 §L).

`spends_money=True`, episode scope, one LLM call through the gateway with the
usual `llm.creative` model. Its only gated ancestor is S3 (`shot_plan`, G1), so
the H0 barrier refuses it until the script gate is approved and re-refuses it if
the approved script changes (T-BIB-04).

The stage's success means "a proposal row exists". A `pending` proposal never
blocks the run: applying it is an operator decision (`studio bible proposals
accept`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from studio.bible.proposal import STAGE, gather_inputs, propose
from studio.core.canonical import canonical_json
from studio.domain.errors import StudioError
from studio.domain.refs import SubjectRef
from studio.pipeline.context import RunContext
from studio.pipeline.stage import StageOutput
from studio.pipeline.subjects import episode_subject


def _sha(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


class BibleProposalHandler:
    """Episode-scope, paid, single-call. Never applies its own proposal."""

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        episode = ctx.repos.content.get_episode(ctx.episode_id) if ctx.episode_id else None
        if episode is None:
            return []
        return [episode_subject(episode.series_id, episode.id)]

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = ctx.repos.content.get_episode(subject.id)
        if episode is None:
            raise StudioError(f"episode not found: {subject.id}")
        inputs = gather_inputs(ctx.repos, episode)
        prompt_sha = ""
        prompt_error = ""
        try:
            from studio.config.prompts import load_prompt

            _text, prompt_sha = load_prompt("bible_update", home=_home(ctx))
        except StudioError as exc:  # reported by run(), not by the hash
            prompt_error = str(exc)
        return _sha(
            {
                "stage": STAGE,
                "episode_id": episode.id,
                "series_id": episode.series_id,
                "base_bible_version": inputs.base_version,
                "bible_sha256": inputs.bible.sha256 if inputs.bible else None,
                "grounding": inputs.summary_sha(),
                "llm": ctx.resolved_profile.model_key_for("llm_creative"),
                "prompt_sha256": prompt_sha,
                "prompt_error": prompt_error,
                "nonce": "0",
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = ctx.repos.content.get_episode(subject.id)
        if episode is None:
            raise StudioError(f"episode not found: {subject.id}")
        output = await propose(ctx, regenerate=False)
        output["episode_id"] = episode.id
        output["series_id"] = episode.series_id
        return StageOutput(output=output)


def _home(ctx: RunContext) -> Path | None:
    home = getattr(ctx.config.settings.paths, "home", None)
    return Path(home) if home else None
