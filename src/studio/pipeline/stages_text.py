"""Text pipeline stages S1-S4 (PHASE2 §F6).

Ordering and wiring (DAG registered by `text_specs()`):

    S1 bible_upsert      EPISODE      llm_creative  writes series_bible + CAS asset
    S2 narration_script  EPISODE      creative → light  tashkeel strip-equality gate
    S3 shot_plan         TRACK        llm_creative  writes shots rows (G1_SCRIPT gate)
    S4 metadata_safety   DELIVERABLE  llm_light     writes metadata_json + safety_status
                                                     (G1_SCRIPT gate parks the run)

Design decisions (recorded in PHASE2_REPORT.md):

- The bible version consulted by S2..S4 is *pinned* to the version S1
  recorded for this run (read via S1's stage output), not `latest()`.
  Pack content and cache keys therefore survive unrelated bible updates
  elsewhere in the series, while a legitimate bible change (re-run of S1
  on a changed concept) correctly invalidates S2..S4.
- S1 packs without a bible section (it defines it); its content-addressed
  upsert makes re-runs free (identical content ⇒ no new version).
- S2 validates lines (strip-equality via F3, one deterministic retry with
  the exact retry sentence appended to the system pack, then a terminal
  StudioError + `tashkeel_mismatch` audit). Validated lines are carried in
  the stage output; `narration_lines` rows are materialized by S3 — the
  table requires a NOT NULL shot_id, which does not exist until S3 creates
  shots. The lines are keyed to shots by (line.no == shot_no, language).
- S3 validates that every referenced character exists in the pinned bible
  BEFORE writing anything; a violation fails the stage with the name in
  the message and writes zero shots.
- S4 fails closed: an unrecognized `safety` value is stored as `flagged`
  with a synthetic issue, and `flagged` runs still park at G3_FINAL_CUT
  (the gate is decision input here, not a block).
- No provider SDK imports, no URLs: everything flows through the gateway.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from studio.domain.models import EpisodeRow, TrackRow
    from studio.pipeline.context import RunContext

from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.core.gateway import CallContext
from studio.domain.enums import GateKind, Scope
from studio.domain.errors import (
    PermanentProviderError,
    PolicyRefusalError,
    StudioError,
)
from studio.domain.refs import SubjectRef
from studio.domain.series_bible import BibleContent, CharacterBible, SeriesBible
from studio.language.tashkeel import check_tashkeel
from studio.pipeline.bible_pack import (
    ContextPack,
    EpisodeSummary,
    episode_summary_from_row,
    pack_context,
)
from studio.pipeline.estimate_media import media_estimate, media_estimate_sha
from studio.pipeline.stage import StageOutput, StageSpec
from studio.providers.base import LLMRequest

_BIBLE_SCHEMA = "bible_upsert"
_SCRIPT_SCHEMA = "narration_script"
_VERIFY_SCHEMA = "tashkeel_verify"
_SHOTS_SCHEMA = "shot_plan"
_METADATA_SCHEMA = "metadata_safety"

_S1_STAGE_INPUT = (
    "اُنشئ أو حدّث إنجيل هذه السلسلة بناءً على حلقةٍ جديدة. أعد كائن JSON واحدًا "
    "بالحقول: title، premise، world، characters (كل عنصر: name، description، "
    "visual_tokens[]، arc)، style_tokens[]، tone[]، continuity_notes. "
    "لا تكتب شيئًا خارج JSON."
)

_S2_STAGE_INPUT = (
    "اكتب الأسطر السردية للحلقة التالية (من ٣ إلى ٥ أسطر). أعد كائن JSON واحدًا "
    "بالشكل {\"lines\": [{\"no\": 1, \"text_tashkeel\": \"...\", \"language\": \"ar\"}]}. "
    "كل سطر عربي كامل التشكيل، والحروف الأصلية لا تُمسّ. لا تكتب شيئًا خارج JSON."
)

_S3_STAGE_INPUT = (
    "خطّط للقطات هذه القصّة الجانبية. اكتب من ٣ إلى ١٢ لقطة. أعد كائن JSON واحدًا "
    "بالشكل {\"shots\": [{\"no\": 1, \"duration_s\": 5, \"visual_prompt\": \"...\", "
    "\"characters\": [\"<اسم من الإنجيل>\"], \"camera\": \"...\"}]}. characters يجب "
    "أن تكون أسماءَ شخصيات الإنجيل حصرًا، وduration_s بين ٣ و١٢ ثانية. "
    "لا تكتب شيئًا خارج JSON."
)

_S4_STAGE_INPUT = (
    "اكتب بيانات النشر والسلامة لهذا التسليم. أعد كائن JSON واحدًا بالحقول: "
    "title، description، tags[]، safety (clear أو flagged)، issues[]. "
    "إن خالف المحتوى معايير السلامة فأشر إليه في issues واجعل safety = flagged. "
    "لا تكتب شيئًا خارج JSON."
)

_RETRY_SENTENCE = (
    "Your previous answer changed the underlying letters; emit the identical "
    "consonantal text with only tashkeel added."
)


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _episode(ctx: RunContext) -> EpisodeRow | None:
    if not ctx.episode_id:
        return None
    return ctx.repos.content.get_episode(ctx.episode_id)


def _episode_subject(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    return [
        SubjectRef(
            scope=Scope.EPISODE,
            id=episode.id,
            lineage=((Scope.SERIES, episode.series_id),),
        )
    ]


def _track_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    tracks = ctx.repos.content.list_tracks(episode.id)
    return [
        SubjectRef(
            scope=Scope.TRACK,
            id=track.id,
            lineage=(
                (Scope.SERIES, episode.series_id),
                (Scope.EPISODE, episode.id),
            ),
        )
        for track in sorted(tracks, key=lambda item: item.aspect)
    ]


def _deliverable_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    deliverables = ctx.repos.content.list_deliverables(episode.id)
    return [
        SubjectRef(
            scope=Scope.DELIVERABLE,
            id=item.id,
            lineage=(
                (Scope.SERIES, episode.series_id),
                (Scope.EPISODE, episode.id),
                (Scope.TRACK, item.track_id),
            ),
        )
        for item in sorted(deliverables, key=lambda d: (d.track_id, d.language))
    ]


def _stage_output(ctx: RunContext, stage: str, subject_id: str) -> dict[str, Any] | None:
    for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage=stage):
        if row.subject_id == subject_id and row.status == "succeeded" and row.output_json:
            try:
                data: dict[str, Any] = json.loads(row.output_json)
                return data
            except json.JSONDecodeError:
                continue
    return None


def _director(ctx: RunContext) -> tuple[str, str]:
    from studio.config.prompts import load_prompt

    home = Path(ctx.config.settings.paths.home)
    return load_prompt("director_prompt", home=home)


def _pinned_bible(ctx: RunContext, episode_id: str) -> SeriesBible | None:
    """Bible version recorded by S1 for this run, falling back to latest()."""
    episode = _episode(ctx)
    if episode is None:
        return None
    series_id = episode.series_id
    s1_output = _stage_output(ctx, "bible_upsert", episode_id)
    version = s1_output.get("version") if isinstance(s1_output, dict) else None
    if isinstance(version, int) and version >= 1:
        pinned = ctx.repos.bible.get(series_id, version)
        if pinned is not None:
            return pinned
    return ctx.repos.bible.latest(series_id)


def _pack(
    ctx: RunContext,
    episode_summary: EpisodeSummary,
    *,
    stage_input: str,
    bible: SeriesBible | None,
) -> ContextPack:
    budget = ctx.config.settings.text_pipeline.context_token_budget
    return pack_context(
        bible=bible,
        episode=episode_summary,
        prior_deliverables=[],
        stage_input=stage_input,
        token_budget=budget,
    )


def _system(ctx: RunContext, director_text: str, pack: ContextPack) -> str:
    return f"{director_text}\n\n{pack.text}"


def _check_refusal(parsed: dict[str, Any]) -> None:
    if parsed.get("error") == "refused":
        reason = str(parsed.get("reason", "unspecified"))[:300]
        raise PolicyRefusalError(f"model refused: {reason}")


def _str_field(item: dict[str, Any], key: str, *, default: str = "") -> str:
    value = item.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


# ==================================================================
# S1 — bible_upsert
# ==================================================================


class BibleUpsertHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _episode_subject(ctx)

    def _context_pack(self, ctx: RunContext) -> tuple[EpisodeSummary, ContextPack, str, str]:
        episode = _episode(ctx)
        assert episode is not None
        summary = episode_summary_from_row(episode)
        pack = _pack(ctx, summary, stage_input=_S1_STAGE_INPUT, bible=None)
        director_text, director_sha = _director(ctx)
        return summary, pack, director_text, director_sha

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        _summary, pack, _director_text, director_sha = self._context_pack(ctx)
        return _sha(
            {
                "stage": "bible_upsert",
                "episode_id": subject.id,
                "concept": episode.concept,
                "languages_json": episode.languages_json,
                "aspects_json": episode.aspects_json,
                "llm": ctx.resolved_profile.model_key_for("llm_creative"),
                "schema": _BIBLE_SCHEMA,
                "pack": pack.pack_sha256,
                "director": director_sha,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        summary, pack, director_text, _director_sha = self._context_pack(ctx)
        user_prompt = (
            f"الحلقة: {episode.title}\nالفكرة: {episode.concept}\n"
            f"اللغات: {'، '.join(summary.languages)} · الأبعاد: {'، '.join(summary.aspects)}\n"
            "أعد الإنجيل المحدَّث كائن JSON واحدًا فقط."
        )
        model_key = ctx.resolved_profile.model_key_for("llm_creative")
        result = await ctx.gateway.llm(
            model_key,
            LLMRequest(
                system=_system(ctx, director_text, pack),
                prompt=user_prompt,
                schema_name=_BIBLE_SCHEMA,
                max_tokens=4096,
                temperature=0.7,
                prompt_version_sha=pack.pack_sha256,
                expect_json=True,
            ),
            CallContext(
                episode_id=episode.id,
                stage_run_id=ctx.stage_run_id,
                nonce="0",
                purpose="bible_upsert",
            ),
        )
        parsed = result.inline_json or {}
        if not isinstance(parsed, dict):
            raise PermanentProviderError("bible_upsert: model returned a non-object JSON payload")
        _check_refusal(parsed)
        content = _parse_bible_content(parsed)
        bible, created = ctx.repos.bible.create_version(episode.series_id, content, created_by="s1")
        ref = ContentStore(
            Path(ctx.config.settings.paths.cas), ctx.db, ctx.clock
        ).put_bytes(
            bible.content_json.encode("utf-8"),
            kind="bible",
            ext="json",
            retention="intermediate",
        )
        assert ref.sha256 == bible.sha256
        ctx.repos.audit.append(
            actor="system",
            entity=f"series:{episode.series_id}",
            event="series_bible_updated",
            payload={
                "series_id": episode.series_id,
                "version": bible.version,
                "sha256": bible.sha256,
                "created": created,
            },
        )
        return StageOutput(
            output={
                "series_id": episode.series_id,
                "version": bible.version,
                "sha256": bible.sha256,
                "created": created,
            }
        )


def _parse_bible_content(parsed: dict[str, Any]) -> BibleContent:
    title = _str_field(parsed, "title")
    premise = _str_field(parsed, "premise")
    if not title or not premise:
        raise PermanentProviderError(
            f"bible_upsert: model payload is missing required title/premise "
            f"(got keys: {sorted(parsed)})"
        )
    characters: list[CharacterBible] = []
    raw_characters = parsed.get("characters")
    if isinstance(raw_characters, list):
        for item in raw_characters:
            if not isinstance(item, dict):
                continue
            name = _str_field(item, "name")
            if not name:
                continue
            characters.append(
                CharacterBible(
                    name=name,
                    description=_str_field(item, "description"),
                    visual_tokens=_str_list(item.get("visual_tokens")),
                    arc=_str_field(item, "arc"),
                )
            )
    if not characters:
        raise PermanentProviderError("bible_upsert: model payload has no valid characters")
    return BibleContent(
        title=title,
        premise=premise,
        world=_str_field(parsed, "world"),
        characters=characters,
        style_tokens=_str_list(parsed.get("style_tokens")),
        tone=_str_list(parsed.get("tone")),
        continuity_notes=_str_field(parsed, "continuity_notes"),
    )


# ==================================================================
# S2 — narration_script (creative, then light tashkeel verification)
# ==================================================================


class NarrationScriptHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _episode_subject(ctx)

    def _context_pack(self, ctx: RunContext) -> tuple[EpisodeSummary, ContextPack, str, str]:
        episode = _episode(ctx)
        assert episode is not None
        summary = episode_summary_from_row(episode)
        bible = _pinned_bible(ctx, episode.id)
        pack = _pack(ctx, summary, stage_input=_S2_STAGE_INPUT, bible=bible)
        director_text, director_sha = _director(ctx)
        return summary, pack, director_text, director_sha

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        _summary, pack, _director_text, director_sha = self._context_pack(ctx)
        bible = _pinned_bible(ctx, episode.id)
        return _sha(
            {
                "stage": "narration_script",
                "episode_id": subject.id,
                "languages_json": episode.languages_json,
                "llm_creative": ctx.resolved_profile.model_key_for("llm_creative"),
                "llm_light": ctx.resolved_profile.model_key_for("llm_light"),
                "schema": _SCRIPT_SCHEMA,
                "verify_schema": _VERIFY_SCHEMA,
                "pack": pack.pack_sha256,
                "bible": bible.sha256 if bible else None,
                "director": director_sha,
                "tashkeel_retry": ctx.config.settings.text_pipeline.tashkeel_retry,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        summary, pack, director_text, _director_sha = self._context_pack(ctx)
        base_system = _system(ctx, director_text, pack)
        user_prompt = (
            f"الحلقة: {episode.title}\nالفكرة: {episode.concept}\n"
            f"اللغات: {json.dumps(summary.languages, ensure_ascii=False)}\n"
            "اكتب الأسطر السردية الآن."
        )
        creative_key = ctx.resolved_profile.model_key_for("llm_creative")
        light_key = ctx.resolved_profile.model_key_for("llm_light")

        # --- S2a: creative writes the script ---
        result_a = await ctx.gateway.llm(
            creative_key,
            LLMRequest(
                system=base_system,
                prompt=user_prompt,
                schema_name=_SCRIPT_SCHEMA,
                max_tokens=4096,
                temperature=0.7,
                prompt_version_sha=pack.pack_sha256,
                expect_json=True,
            ),
            CallContext(
                episode_id=episode.id,
                stage_run_id=ctx.stage_run_id,
                nonce="0",
                purpose="narration_script",
            ),
        )
        lines = _parse_lines(result_a.inline_json, summary.languages)

        # --- S2b: light verifies tashkeel (strip-equality, hard gate) ---
        retry_budget = ctx.config.settings.text_pipeline.tashkeel_retry
        verify_payload = {
            "lines": [
                {"no": line["no"], "text": line["text_tashkeel"], "language": line["language"]}
                for line in lines
            ]
        }
        verify_prompt = (
            "أعد إصدار كل سطر من الأسطر التالية حرفيًّا (نفس الحروف ونفس الترتيب) "
            "مع إضافة أو تصحيح التشكيل فقط؛ لا تُغيّر أي حرف. "
            'أعد JSON واحدًا بالشكل {"lines": [{"no": 1, "text": "...", "language": "ar"}]} '
            "بلا أي نص آخر.\n"
            + json.dumps(verify_payload, ensure_ascii=False, separators=(",", ":"))
        )
        verify_system = base_system
        attempts = 0
        mismatches: list[tuple[int, int | None]] = []
        while True:
            system = verify_system if attempts == 0 else verify_system + "\n\n" + _RETRY_SENTENCE
            result_b = await ctx.gateway.llm(
                light_key,
                LLMRequest(
                    system=system,
                    prompt=verify_prompt,
                    schema_name=_VERIFY_SCHEMA,
                    max_tokens=1024,
                    temperature=0.2,
                    prompt_version_sha=pack.pack_sha256,
                    expect_json=True,
                ),
                CallContext(
                    episode_id=episode.id,
                    stage_run_id=ctx.stage_run_id,
                    nonce=str(attempts),
                    purpose="tashkeel_verify",
                ),
            )
            attempts += 1
            mismatches = _verify_lines(lines, result_b.inline_json)
            if not mismatches:
                break
            if attempts > retry_budget:
                ctx.repos.audit.append(
                    actor="system",
                    entity=f"stage_run:{ctx.stage_run_id}",
                    event="tashkeel_mismatch",
                    payload={
                        "episode_id": episode.id,
                        "lines": [
                            {"line_no": line_no, "first_mismatch": index}
                            for line_no, index in mismatches
                        ],
                        "attempts": attempts,
                    },
                )
                failed = ", ".join(str(line_no) for line_no, _ in mismatches)
                raise StudioError(
                    f"narration_script: tashkeel verification failed for line(s) {failed} "
                    f"after {attempts} attempt(s); underlying letters were altered"
                )
        return StageOutput(
            output={
                "episode_id": episode.id,
                "languages": summary.languages,
                "lines": lines,
                "verified": True,
                "verify_attempts": attempts,
            }
        )


def _parse_lines(
    parsed: Any, episode_languages: list[str]
) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict):
        raise PermanentProviderError("narration_script: model returned a non-object JSON payload")
    _check_refusal(parsed)
    raw_lines = parsed.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise PermanentProviderError("narration_script: payload has no lines")
    allowed = set(episode_languages)
    seen: set[tuple[int, str]] = set()
    lines: list[dict[str, Any]] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        try:
            no = int(item.get("no", 0))
        except (TypeError, ValueError):
            continue
        text = _str_field(item, "text_tashkeel")
        language = _str_field(item, "language")
        if no < 1 or not text or not language:
            continue
        if language not in allowed:
            raise PermanentProviderError(
                f"narration_script: line {no} carries language {language!r}, "
                f"outside the episode's languages {sorted(allowed)}"
            )
        key = (no, language)
        if key in seen:
            raise PermanentProviderError(
                f"narration_script: duplicate line (no={no}, language={language!r})"
            )
        seen.add(key)
        lines.append({"no": no, "text_tashkeel": text, "language": language})
    if not lines:
        raise PermanentProviderError("narration_script: no valid lines in model payload")
    lines.sort(key=lambda line: (line["language"], line["no"]))
    return lines


def _verify_lines(
    source_lines: list[dict[str, Any]], emitted: Any
) -> list[tuple[int, int | None]]:
    """Positional strip-equality check of the light model's re-emission."""
    out_lines: list[dict[str, Any]] = []
    if isinstance(emitted, dict):
        _check_refusal(emitted)
        raw = emitted.get("lines")
        if isinstance(raw, list):
            out_lines = [item for item in raw if isinstance(item, dict)]
    mismatches: list[tuple[int, int | None]] = []
    for index, source in enumerate(source_lines):
        if index >= len(out_lines):
            mismatches.append((int(source["no"]), None))
            continue
        emitted_text = _str_field(out_lines[index], "text")
        check = check_tashkeel(str(source["text_tashkeel"]), emitted_text)
        if not check.ok:
            mismatches.append((int(source["no"]), check.first_mismatch))
    extra = len(out_lines) - len(source_lines)
    if extra > 0:
        mismatches.append((-1, None))  # verifier added lines; treat as failure
    return mismatches


# ==================================================================
# S3 — shot_plan (creative; continuity gate; shots rows)
# ==================================================================


class ShotPlanHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _track_subjects(ctx)

    def _context_pack(
        self, ctx: RunContext, track: TrackRow
    ) -> tuple[EpisodeSummary, ContextPack, str, str, SeriesBible | None]:
        episode = _episode(ctx)
        assert episode is not None
        summary = episode_summary_from_row(episode)
        bible = _pinned_bible(ctx, episode.id)
        stage_input = self._stage_input(ctx, episode.id, track)
        pack = _pack(ctx, summary, stage_input=stage_input, bible=bible)
        director_text, director_sha = _director(ctx)
        return summary, pack, director_text, director_sha, bible

    def _stage_input(self, ctx: RunContext, episode_id: str, track: TrackRow) -> str:
        s2_output = _stage_output(ctx, "narration_script", episode_id)
        lines = []
        if isinstance(s2_output, dict):
            raw = s2_output.get("lines")
            if isinstance(raw, list):
                lines = raw
        script = json.dumps(lines, ensure_ascii=False, separators=(",", ":"))
        return f"{_S3_STAGE_INPUT}\naspect: {track.aspect}\nالأسطر السردية:\n{script}"

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        shot_track = ctx.repos.content.get_track(subject.id)
        assert shot_track is not None
        episode = _episode(ctx)
        assert episode is not None
        _summary, pack, _director_text, director_sha, bible = self._context_pack(ctx, shot_track)
        return _sha(
            {
                "stage": "shot_plan",
                "track_id": subject.id,
                "aspect": shot_track.aspect,
                "llm": ctx.resolved_profile.model_key_for("llm_creative"),
                "schema": _SHOTS_SCHEMA,
                "pack": pack.pack_sha256,
                "bible": bible.sha256 if bible else None,
                "director": director_sha,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        track = ctx.repos.content.get_track(subject.id)
        if track is None:
            raise PermanentProviderError(f"shot_plan: track not found: {subject.id}")
        _summary, pack, director_text, _director_sha, bible = self._context_pack(ctx, track)
        if bible is None:
            raise StudioError("shot_plan: no series bible; bible_upsert must run first")
        s2_output = _stage_output(ctx, "narration_script", episode.id)
        if not isinstance(s2_output, dict) or not s2_output.get("lines"):
            raise PermanentProviderError(
                "shot_plan: narration_script output not found; narration_script must run first"
            )

        base_system = _system(ctx, director_text, pack)
        names = "، ".join(character.name for character in bible.characters)
        user_prompt = (
            f"الحلقة: {episode.title}\nالفكرة: {episode.concept}\n"
            f"البُعد: {track.aspect}\nشخصيات الإنجيل: {names}\n"
            "خطّط اللقطات الآن."
        )
        model_key = ctx.resolved_profile.model_key_for("llm_creative")
        result = await ctx.gateway.llm(
            model_key,
            LLMRequest(
                system=base_system,
                prompt=user_prompt,
                schema_name=_SHOTS_SCHEMA,
                max_tokens=4096,
                temperature=0.7,
                prompt_version_sha=pack.pack_sha256,
                expect_json=True,
            ),
            CallContext(
                episode_id=episode.id,
                stage_run_id=ctx.stage_run_id,
                nonce="0",
                purpose="shot_plan",
            ),
        )
        shots = _parse_shots(result.inline_json)

        # Continuity gate: validate EVERY referenced character before ANY write.
        known = {character.name for character in bible.characters}
        for shot in shots:
            for name in shot["characters"]:
                if name not in known:
                    raise PermanentProviderError(
                        f"shot_plan: shot {shot['no']} references character {name!r}, "
                        f"which is not in series bible v{bible.version} "
                        f"(known: {', '.join(sorted(known))})"
                    )

        existing = ctx.repos.content.shots_for_track(track.id)
        if existing:
            shot_rows = sorted(existing, key=lambda item: item.shot_no)
            created = 0
        else:
            created = 0
            for shot in shots:
                ctx.repos.content.create_shot(
                    track_id=track.id,
                    shot_no=int(shot["no"]),
                    prompt=str(shot["visual_prompt"]),
                )
                created += 1
            shot_rows = sorted(
                ctx.repos.content.shots_for_track(track.id), key=lambda item: item.shot_no
            )

        shot_ids_by_no = {row.shot_no: row.id for row in shot_rows}

        # Materialize narration_lines (shots now exist; language-aware).
        deliverables = [
            item
            for item in ctx.repos.content.list_deliverables(episode.id)
            if item.track_id == track.id
        ]
        lines = s2_output["lines"]
        lines_persisted = 0
        for deliverable in deliverables:
            for line in lines:
                if str(line.get("language")) != deliverable.language:
                    continue
                shot_id = shot_ids_by_no.get(int(line["no"]))
                if shot_id is None:
                    continue
                ctx.repos.content.upsert_narration_line(
                    deliverable_id=deliverable.id,
                    shot_id=shot_id,
                    line_no=int(line["no"]),
                    text=str(line["text_tashkeel"]),
                    asset_sha256=None,
                    duration_ms=None,
                )
                lines_persisted += 1

        return StageOutput(
            output={
                "track_id": track.id,
                "aspect": track.aspect,
                "shots_created": created,
                "shot_ids": [row.id for row in shot_rows],
                "shots": shots,
                "narration_lines_persisted": lines_persisted,
            }
        )


def _parse_shots(parsed: Any) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict):
        raise PermanentProviderError("shot_plan: model returned a non-object JSON payload")
    _check_refusal(parsed)
    raw_shots = parsed.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        raise PermanentProviderError("shot_plan: payload has no shots")
    shots: list[dict[str, Any]] = []
    seen_nos: set[int] = set()
    for item in raw_shots:
        if not isinstance(item, dict):
            continue
        try:
            no = int(item.get("no", 0))
            duration = float(item.get("duration_s", 0) or 0)
        except (TypeError, ValueError):
            continue
        visual_prompt = _str_field(item, "visual_prompt")
        if no < 1 or not visual_prompt or duration <= 0:
            continue
        if no in seen_nos:
            raise PermanentProviderError(f"shot_plan: duplicate shot number {no}")
        seen_nos.add(no)
        shots.append(
            {
                "no": no,
                "duration_s": duration,
                "visual_prompt": visual_prompt,
                "characters": _str_list(item.get("characters")),
                "camera": _str_field(item, "camera"),
            }
        )
    if not shots:
        raise PermanentProviderError("shot_plan: no valid shots in model payload")
    shots.sort(key=lambda shot: shot["no"])
    return shots


# ==================================================================
# S4 — metadata_safety (light; per deliverable; fail-closed safety)
# ==================================================================


class MetadataSafetyHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _deliverable_subjects(ctx)

    def _context_pack(
        self, ctx: RunContext, deliverable_language: str
    ) -> tuple[EpisodeSummary, ContextPack, str, str]:
        episode = _episode(ctx)
        assert episode is not None
        summary = episode_summary_from_row(episode)
        bible = _pinned_bible(ctx, episode.id)
        stage_input = f"{_S4_STAGE_INPUT}\nlanguage: {deliverable_language}"
        pack = _pack(ctx, summary, stage_input=stage_input, bible=bible)
        director_text, director_sha = _director(ctx)
        return summary, pack, director_text, director_sha

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        _summary, pack, _director_text, director_sha = self._context_pack(
            ctx, deliverable.language
        )
        return _sha(
            {
                "stage": "metadata_safety",
                "deliverable_id": subject.id,
                "language": deliverable.language,
                "channel_id": deliverable.channel_id,
                "llm": ctx.resolved_profile.model_key_for("llm_light"),
                "schema": _METADATA_SCHEMA,
                "pack": pack.pack_sha256,
                "director": director_sha,
                # H2: registry prices/settings are part of the decision input.
                "media_estimate": media_estimate_sha(ctx),
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        if deliverable is None:
            raise PermanentProviderError(f"metadata_safety: deliverable not found: {subject.id}")
        _summary, pack, director_text, _director_sha = self._context_pack(
            ctx, deliverable.language
        )
        base_system = _system(ctx, director_text, pack)
        user_prompt = (
            f"الحلقة: {episode.title}\nاللغة: {deliverable.language}\n"
            f"القناة: {deliverable.channel_id}\nاكتب بيانات النشر والسلامة الآن."
        )
        model_key = ctx.resolved_profile.model_key_for("llm_light")
        result = await ctx.gateway.llm(
            model_key,
            LLMRequest(
                system=base_system,
                prompt=user_prompt,
                schema_name=_METADATA_SCHEMA,
                max_tokens=1024,
                temperature=0.2,
                prompt_version_sha=pack.pack_sha256,
                expect_json=True,
            ),
            CallContext(
                episode_id=episode.id,
                stage_run_id=ctx.stage_run_id,
                nonce="0",
                purpose="metadata_safety",
            ),
        )
        metadata = _parse_metadata(result.inline_json)
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        ctx.repos.content.set_deliverable_metadata(
            deliverable_id=deliverable.id,
            metadata_json=metadata_json,
            safety_status=metadata["safety"],
        )
        estimate = media_estimate(ctx, deliverable.id)
        output: dict[str, Any] = {
            "deliverable_id": deliverable.id,
            "language": deliverable.language,
            "safety": metadata["safety"],
            "title": metadata["title"],
            "tags": metadata["tags"],
        }
        output.update(estimate.additive_output())
        return StageOutput(output=output)


def _parse_metadata(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        raise PermanentProviderError(
            "metadata_safety: model returned a non-object JSON payload"
        )
    _check_refusal(parsed)
    title = _str_field(parsed, "title")
    if not title:
        raise PermanentProviderError("metadata_safety: payload is missing a title")
    description = _str_field(parsed, "description")
    tags = _str_list(parsed.get("tags"))
    issues = _str_list(parsed.get("issues"))
    raw_safety = _str_field(parsed, "safety").lower()
    if raw_safety == "clear":
        safety = "clear"
    elif raw_safety == "flagged":
        safety = "flagged"
    else:
        # Fail closed: anything unrecognized is treated as flagged.
        safety = "flagged"
        issues = [*issues, f"unrecognized safety value {raw_safety!r} (treated as flagged)"]
    return {
        "title": title,
        "description": description,
        "tags": tags,
        "safety": safety,
        "issues": issues,
    }


# ==================================================================
# spec table
# ==================================================================


def text_specs() -> list[StageSpec]:
    """S1 → S2 → S3 (G1_SCRIPT) → S4 (G3_FINAL_CUT).

    S4's gate parks the run at G3_FINAL_CUT — the same gate a final cut
    would park at; metadata/safety are decision input for that approval.
    """
    return [
        StageSpec(
            name="bible_upsert",
            scope=Scope.EPISODE,
            depends_on=(),
            handler=BibleUpsertHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="narration_script",
            scope=Scope.EPISODE,
            depends_on=("bible_upsert",),
            handler=NarrationScriptHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="shot_plan",
            scope=Scope.TRACK,
            depends_on=("narration_script",),
            handler=ShotPlanHandler(),
            gate_after=GateKind.G1_SCRIPT,
        ),
        StageSpec(
            name="metadata_safety",
            scope=Scope.DELIVERABLE,
            depends_on=("shot_plan",),
            handler=MetadataSafetyHandler(),
            # H5.7: the metadata text review is a script approval, not a cut
            # approval. G3_FINAL_CUT now hangs off S12 on the rendered file.
            gate_after=GateKind.G1_SCRIPT,
        ),
    ]
