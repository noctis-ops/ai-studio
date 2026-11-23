"""S14 — bible proposals: gather, propose, accept, reject (Phase 5 §L).

One paid call per episode, through the same structured-output path S1-S3 use.
The rules that matter here:

- **Nothing is auto-applied.** A run produces a `bible_proposals` row in
  `pending`; only `accept` writes a new bible version.
- **One paid regeneration, then stop.** A payload that fails
  `validate_proposal` is retried once with a different nonce; a second failure
  raises `ProposalInvalid` instead of looping.
- **Resume never double-pays.** The cache key is
  (prompt sha, inputs, nonce) and the nonce only changes on an explicit
  `--regenerate`, so a crash-resume hits the gateway cache.
- **Accept refuses a stale base.** If the bible version moved since the
  proposal was made, the proposal is marked `stale` and nothing is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from studio.config.prompts import load_prompt
from studio.core.gateway import CallContext
from studio.domain.errors import (
    ConfigError,
    PermanentProviderError,
    ProposalInvalid,
    ProposalStale,
    StudioError,
)
from studio.domain.models import BibleProposalRow, EpisodeRow
from studio.domain.series_bible import BibleContent, SeriesBible
from studio.providers.base import LLMRequest

from .apply import apply_proposal
from .schema import BibleUpdate, proposal_to_json, validate_proposal

STAGE = "s14_bible_proposals"
PROMPT_NAME = "bible_update"
SCHEMA_NAME = "bible_update"

# Facts per proposal list are individually capped in `schema.py`; this is the
# cross-list cap from `bible.proposals.max_facts`.
_DEFAULT_MAX_FACTS = 40


@dataclass(frozen=True)
class ProposalInputs:
    """Everything the prompt is built from, gathered once."""

    episode: EpisodeRow
    bible: SeriesBible | None
    scenes: tuple[dict[str, Any], ...]
    characters: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    @property
    def base_version(self) -> int:
        return self.bible.version if self.bible is not None else 0

    def payload(self) -> dict[str, Any]:
        """The JSON block appended to the prompt (and hashed into the key)."""
        return {
            "episode": {
                "title": self.episode.title,
                "concept": self.episode.concept,
                "languages": json.loads(self.episode.languages_json or "[]"),
                "aspects": json.loads(self.episode.aspects_json or "[]"),
            },
            "bible_version": self.base_version,
            "known_characters": list(self.characters),
            "scenes": list(self.scenes),
            "scene_ids": list(self.evidence_ids),
        }

    def summary_sha(self) -> str:
        """Stable digest of the grounding material (used in input hashes)."""
        import hashlib

        from studio.core.canonical import canonical_json

        return hashlib.sha256(canonical_json(self.payload())).hexdigest()


def gather_inputs(repos: Any, episode: EpisodeRow) -> ProposalInputs:
    """(episode, current bible, scene/shots summary, known characters).

    Scenes are Phase 3 rows per track; an episode whose scenes have not been
    materialised yet falls back to its shots, which are what a scene ranges
    over. Either way the model only ever sees ids it is allowed to cite.
    """
    bible = repos.bible.latest(episode.series_id)
    characters = tuple(character.name for character in (bible.characters if bible else []))

    scenes: list[dict[str, Any]] = []
    evidence_ids: list[str] = []
    shots = sorted(repos.content.list_shots(episode.id), key=lambda shot: shot.shot_no)
    shots_by_no = {shot.shot_no: shot for shot in shots}
    for track in repos.content.list_tracks(episode.id):
        for scene in repos.content.list_scenes(track.id):
            if scene.id in evidence_ids:
                continue
            evidence_ids.append(scene.id)
            shot_range = [
                shots_by_no[number].prompt
                for number in range(scene.shot_from, scene.shot_to + 1)
                if number in shots_by_no
            ]
            scenes.append(
                {
                    "scene_id": scene.id,
                    "scene_no": scene.scene_no,
                    "aspect": track.aspect,
                    "mood": scene.music_mood,
                    "intensity": scene.intensity,
                    "shots": shot_range,
                }
            )
    if not evidence_ids:
        for shot in shots:
            evidence_ids.append(shot.id)
            scenes.append(
                {
                    "scene_id": shot.id,
                    "scene_no": shot.shot_no,
                    "aspect": None,
                    "mood": None,
                    "intensity": None,
                    "shots": [shot.prompt],
                }
            )
    return ProposalInputs(
        episode=episode,
        bible=bible,
        scenes=tuple(scenes),
        characters=characters,
        evidence_ids=tuple(evidence_ids),
    )


def _max_facts(ctx: Any) -> int:
    settings = getattr(ctx.config, "settings", None)
    bible_settings = getattr(settings, "bible", None)
    proposals = getattr(bible_settings, "proposals", None)
    return int(getattr(proposals, "max_facts", _DEFAULT_MAX_FACTS))


def _prompt_text(ctx: Any) -> tuple[str, str]:
    """(system text, sha) from `configs/prompts/bible_update.txt`.

    An explicit `paths.home` keeps the CLI and tests on the same file the run
    used; a missing prompt is an error, never a silent empty system message.
    """
    home = None
    settings = getattr(ctx.config, "settings", None)
    paths = getattr(settings, "paths", None)
    if paths is not None and getattr(paths, "home", None):
        from pathlib import Path

        home = Path(paths.home)
    try:
        return load_prompt(PROMPT_NAME, home=home)
    except ConfigError as exc:
        raise StudioError(f"S14 needs configs/prompts/{PROMPT_NAME}.txt: {exc}") from exc


def _user_prompt(inputs: ProposalInputs) -> str:
    payload = json.dumps(inputs.payload(), ensure_ascii=False, sort_keys=True)
    return (
        "حدِّث إنجيل السلسلة لهذه الحلقة. الأدلّة أدناه فقط، ولكل حقيقة "
        "يجب أن تُسنَد إلى scene_id واحد على الأقل من قائمة scene_ids.\n"
        "أعد كائن JSON واحدًا فقط بالمفاتيح الستة المطلوبة.\n\n"
        f"{payload}"
    )


def _known_character_names(inputs: ProposalInputs) -> tuple[str, ...]:
    return inputs.characters


def build_request(
    ctx: Any, inputs: ProposalInputs
) -> tuple[LLMRequest, str, int]:
    """(request, model_key, max_facts) — shared by `propose` and the CLI's
    cost display, so the estimate is derived from the exact bytes that get sent."""
    system_text, prompt_sha = _prompt_text(ctx)
    request = LLMRequest(
        system=system_text,
        prompt=_user_prompt(inputs),
        schema_name=SCHEMA_NAME,
        max_tokens=4096,
        temperature=0.4,
        prompt_version_sha=prompt_sha,
        expect_json=True,
    )
    return request, ctx.resolved_profile.model_key_for("llm_creative"), _max_facts(ctx)


def estimate_cost(ctx: Any) -> tuple[int, str]:
    """Worst-case mUSD for one proposal call, plus a human line."""
    from studio.core.pricing import estimate_cost_musd

    episode = ctx.repos.content.get_episode(ctx.episode_id) if ctx.episode_id else None
    if episode is None:
        raise StudioError(f"episode not found: {ctx.episode_id}")
    inputs = gather_inputs(ctx.repos, episode)
    request, model_key, _unused_limit = build_request(ctx, inputs)
    entry = ctx.config.registry.models.get(model_key)
    if entry is None:
        raise StudioError(f"model_key {model_key!r} is not in the registry")
    del _unused_limit
    musd = int(estimate_cost_musd(entry, request))
    return musd, f"one {model_key} call ({SCHEMA_NAME}, max_tokens=4096)"


async def _call(
    ctx: Any,
    *,
    inputs: ProposalInputs,
    nonce: str,
) -> tuple[BibleUpdate, str | None]:
    """One gateway call + validation. Returns (update, provider_call_id)."""
    request, model_key, max_facts = build_request(ctx, inputs)
    result = await ctx.gateway.llm(
        model_key,
        request,
        CallContext(
            episode_id=inputs.episode.id,
            stage_run_id=ctx.stage_run_id,
            nonce=nonce,
            purpose=SCHEMA_NAME,
            gate_cleared=bool(getattr(ctx, "gate_cleared", True)),
        ),
    )
    parsed = result.inline_json
    if parsed is None:
        raise PermanentProviderError("bible_update: the model returned no JSON payload")
    update = validate_proposal(
        parsed,
        known_ids=inputs.evidence_ids,
        character_names=_known_character_names(inputs),
        max_facts=max_facts,
    )
    return update, result.provider_call_id


async def propose(
    ctx: Any,
    *,
    regenerate: bool = False,
    nonce_override: str | None = None,
) -> dict[str, Any]:
    """Run one proposal (one paid call, plus at most one retry) and store it."""
    episode = ctx.repos.content.get_episode(ctx.episode_id) if ctx.episode_id else None
    if episode is None:
        raise StudioError(f"episode not found: {ctx.episode_id}")
    inputs = gather_inputs(ctx.repos, episode)
    if not inputs.evidence_ids:
        raise StudioError(
            f"episode {episode.id} has no scenes or shots to cite; nothing to propose from"
        )

    # Nonce policy: "0" for the normal path (stable across resume), a fresh
    # nonce only for an explicit regeneration. Existing proposals count so two
    # regenerations never collide.
    if nonce_override is not None:
        nonce = nonce_override
    elif regenerate:
        nonce = f"regen-{ctx.repos.proposals.count_for_episode(episode.id) + 1}"
    else:
        nonce = "0"

    try:
        update, call_id = await _call(ctx, inputs=inputs, nonce=nonce)
    except ProposalInvalid as first:
        # Exactly one paid regeneration, then the operator decides.
        try:
            update, call_id = await _call(ctx, inputs=inputs, nonce=f"{nonce}-retry1")
        except ProposalInvalid as second:
            ctx.repos.audit.append(
                actor="system",
                entity=f"episode:{episode.id}",
                event="bible_proposal_invalid",
                payload={
                    "run_id": ctx.run_id,
                    "first": str(first)[:1000],
                    "second": str(second)[:1000],
                },
            )
            raise ProposalInvalid(
                f"bible_update rejected twice; regenerate deliberately from the CLI. {second}"
            ) from second

    proposal = ctx.repos.proposals.create(
        episode_id=episode.id,
        series_id=episode.series_id,
        base_bible_version=inputs.base_version,
        proposal_json=json.dumps(proposal_to_json(update), ensure_ascii=False, sort_keys=True),
        provider_call_id=call_id,
    )
    ctx.repos.audit.append(
        actor="system",
        entity=f"episode:{episode.id}",
        event="bible_proposal_created",
        payload={
            "proposal_id": proposal.id,
            "base_bible_version": proposal.base_bible_version,
            "facts": update.fact_count(),
            "nonce": nonce,
            "run_id": ctx.run_id,
        },
    )
    return _proposal_output(proposal, update)


def _proposal_output(proposal: BibleProposalRow, update: BibleUpdate) -> dict[str, Any]:
    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "base_bible_version": proposal.base_bible_version,
        "facts": update.fact_count(),
        "new_characters": [item.name for item in update.new_characters],
        "evidence_ids": sorted(set(update.all_evidence_ids())),
    }


def _content_from(bible: SeriesBible | None) -> BibleContent:
    if bible is None:
        raise StudioError("the series has no bible version to propose against")
    return BibleContent.model_validate(json.loads(bible.content_json))


def accept_proposal(ctx: Any, proposal_id: str, *, actor: str = "operator") -> dict[str, Any]:
    """Apply a pending proposal as a new bible version, or refuse if stale."""
    proposal = ctx.repos.proposals.get(proposal_id)
    if proposal is None:
        raise StudioError(f"unknown proposal {proposal_id}")
    if proposal.status != "pending":
        raise StudioError(f"proposal {proposal_id} is {proposal.status}, not pending")
    current = ctx.repos.bible.latest(proposal.series_id)
    current_version = current.version if current is not None else 0
    if current_version != proposal.base_bible_version:
        ctx.repos.proposals.decide(proposal_id, status="stale")
        ctx.repos.audit.append(
            actor=actor,
            entity=f"bible_proposal:{proposal_id}",
            event="bible_proposal_stale",
            payload={
                "base_bible_version": proposal.base_bible_version,
                "current_version": current_version,
            },
        )
        raise ProposalStale(
            f"proposal {proposal_id} was based on bible v{proposal.base_bible_version} "
            f"but the bible is at v{current_version}; regenerate it"
        )
    # Re-validate the stored shape (a hand-edited row is not trusted), but skip
    # the evidence cross-check: scene ids were valid when the row was written
    # and re-running it would make an old proposal un-acceptable after a re-render.
    update = BibleUpdate.model_validate(json.loads(proposal.proposal_json))
    next_content = apply_proposal(_content_from(current), update)
    bible, created = ctx.repos.bible.create_version(
        proposal.series_id, next_content, created_by="s14"
    )
    ctx.repos.proposals.decide(proposal_id, status="accepted")
    ctx.repos.audit.append(
        actor=actor,
        entity=f"bible_proposal:{proposal_id}",
        event="bible_proposal_accepted",
        payload={
            "series_id": proposal.series_id,
            "from_version": proposal.base_bible_version,
            "to_version": bible.version,
            "bible_sha256": bible.sha256,
        },
    )
    return {"proposal_id": proposal_id, "version": bible.version, "created": created}


def reject_proposal(
    ctx: Any, proposal_id: str, *, reason: str = "", actor: str = "operator"
) -> dict[str, Any]:
    """Mark a pending proposal rejected; the bible is untouched."""
    proposal = ctx.repos.proposals.get(proposal_id)
    if proposal is None:
        raise StudioError(f"unknown proposal {proposal_id}")
    if proposal.status != "pending":
        raise StudioError(f"proposal {proposal_id} is {proposal.status}, not pending")
    ctx.repos.proposals.decide(proposal_id, status="rejected")
    ctx.repos.audit.append(
        actor=actor,
        entity=f"bible_proposal:{proposal_id}",
        event="bible_proposal_rejected",
        payload={"reason": reason[:500]},
    )
    return {"proposal_id": proposal_id, "status": "rejected"}
