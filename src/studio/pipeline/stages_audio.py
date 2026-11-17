"""Audio pipeline stages S5a / S5 / S6 (PHASE3 §H6, §H7).

DAG (registered by the caller — see `demo_specs()`):

    <Gate-1 stage>  →  s5a_tts_preflight  →  s5_tts  →  s6_timing  →  s7_audio_plan  →  …

Design decisions (recorded in PHASE3_REPORT.md):

- **S5a is EPISODE scope and free.** It spends nothing, writes nothing, and
  fails closed with one typed error listing *every* problem found
  (missing voice per (speaker, language) pair, unattested voices for
  `final`, placeholder/unresolvable TTS model — including per-voice
  `model_key` overrides — words-per-second coverage, over-budget
  worst-case reservation). The over-budget case raises `BudgetExceeded`,
  which the engine treats as a budget halt (stage back to pending, run →
  halted_budget), never a crash.
- **S5 is LINE scope** so the operator sees per-line progress and resumed
  runs re-pay only unfinished lines (cache keys carry `tts_nonce`).
  `text_to_send` is computed exactly once via
  `language.normalize_for_tts(...)` (honors `tts.keep_diacritics`) and drives
  both the request and the worst-case accounting. The QC regeneration loop
  re-tries at most `tts.qc.auto_retries` times with fresh nonces
  (`<base>.q<n>`, persisted), then fails with `TtsQualityError` naming the
  line and every measurement.
- **S6 is TRACK scope** (free, local), aggregating every shot of the track so
  the operator sees *all* overflows in one error. It writes
  `clip_duration_ms` (a supported value), `speed_factor`, `hold_frames` and
  `timing_status='locked'` on each shot. Narration text is never edited.
  **Regenerating a clip must never change the locked duration** (Phase 4
  invariant — the clip assembler reads `clip_duration_ms`, not the request).
- The Gate-1 video estimate ("stored by S4" per the spec) does not exist in
  this codebase — Phase 2's S4 stores no projection. S6 supports an optional
  baseline via the S4 stage-output keys `projected_video_cost_musd` /
  `gate1_video_estimate_musd`; when absent the drift comparison is skipped
  and only the S6 output JSON carries the projection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import wave
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:

    from studio.domain.models import (
        CharacterVoiceRow,
        DeliverableRow,
        EpisodeRow,
        NarrationLineRow,
    )
    from studio.pipeline.context import RunContext

from studio.audio.analysis import rms_dbfs, s16_le_samples
from studio.audio.conform import conform_line
from studio.audio.library import resolve_library_root
from studio.audio.mastering import master_two_pass
from studio.audio.mixer import DuckSpec, cues_from_rows, render_premaster
from studio.audio.plan import plan_audio, scene_pieces
from studio.audio.stem import build_narration_stem
from studio.audio.timeline import build_timeline
from studio.audio.voice import (
    parse_voice_settings,
    request_settings,
    voice_snapshot,
    worst_case_tts_musd,
)
from studio.config.registry import PerSecond
from studio.core.canonical import canonical_json
from studio.core.cas import ContentStore
from studio.core.clock import format_ts
from studio.core.gateway import CallContext
from studio.db.repos.audio import NewAudioCue
from studio.domain.enums import (
    GLOBAL_SCOPE_REF,
    BudgetScope,
    Language,
    Profile,
    Scope,
)
from studio.domain.errors import (
    AssetNotFoundError,
    AudioQcError,
    BudgetExceeded,
    ConfigError,
    PermanentProviderError,
    PreflightError,
    StudioError,
    TimingOverflowError,
    TtsQualityError,
)
from studio.domain.money import musd_to_usd, usd_to_musd
from studio.domain.refs import SubjectRef
from studio.language import count_words, normalize_for_tts
from studio.pipeline.stage import StageOutput
from studio.providers.base import TTSRequest
from studio.qc.audio import AudioQcInputs, QcSpec, qc_rows, run_audio_qc

logger = logging.getLogger(__name__)

_NONCE_RETRY_MARKER = ".q"


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


def _line_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    deliverables = {d.id: d for d in ctx.repos.content.list_deliverables(episode.id)}
    subjects: list[SubjectRef] = []
    for line in ctx.repos.content.narration_lines_for_episode(episode.id):
        deliverable = deliverables.get(line.deliverable_id)
        if deliverable is None:
            continue
        subjects.append(
            SubjectRef(
                scope=Scope.LINE,
                id=line.id,
                lineage=(
                    (Scope.SERIES, episode.series_id),
                    (Scope.EPISODE, episode.id),
                    (Scope.TRACK, deliverable.track_id),
                    (Scope.DELIVERABLE, deliverable.id),
                ),
            )
        )
    subjects.sort(key=lambda item: item.id)
    return subjects


def _stage_output(ctx: RunContext, stage: str, subject_id: str) -> dict[str, Any] | None:
    for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage=stage):
        if row.subject_id == subject_id and row.status == "succeeded" and row.output_json:
            try:
                data: dict[str, Any] = json.loads(row.output_json)
                return data
            except json.JSONDecodeError:
                continue
    return None


def _text_to_send(ctx: RunContext, language: Language, approved_text: str) -> str:
    return normalize_for_tts(language, approved_text, ctx.config.settings)


def _base_nonce(nonce: str) -> str:
    """Strip a QC-retry suffix (`<base>.q<n>`) to get the base nonce."""
    head, sep, tail = nonce.rpartition(_NONCE_RETRY_MARKER)
    if sep and head and tail.isdigit():
        return head
    return nonce


def _speaker_voice(
    ctx: RunContext, episode: EpisodeRow, language: Language
) -> CharacterVoiceRow | None:
    narrator = ctx.repos.content.get_narrator(episode.series_id)
    if narrator is None:
        return None
    voice = ctx.repos.audio.get_character_voice(
        series_id=episode.series_id,
        character_slug=narrator.role,
        language=language.value,
    )
    return voice


def _effective_tts_entry(
    ctx: RunContext, settings_json: str
) -> tuple[str, Any, Any]:
    """Voice's effective (model_key, ModelEntry) via VoiceSettings.model_key."""
    parsed = parse_voice_settings(settings_json)
    model_key = parsed.model_key
    entry = ctx.config.registry.models.get(model_key)
    return model_key, entry, parsed


def _wanted_languages(ctx: RunContext, episode: EpisodeRow) -> list[Language]:
    languages: set[Language] = set()
    for deliverable in ctx.repos.content.list_deliverables(episode.id):
        lang = Language(deliverable.language)
        if lang in ctx.languages:
            languages.add(lang)
    return sorted(languages, key=lambda item: item.value)


def _line_rows(
    ctx: RunContext, episode: EpisodeRow
) -> list[tuple[NarrationLineRow, DeliverableRow]]:
    deliverables = {d.id: d for d in ctx.repos.content.list_deliverables(episode.id)}
    pairs: list[tuple[NarrationLineRow, DeliverableRow]] = []
    for line in ctx.repos.content.narration_lines_for_episode(episode.id):
        deliverable = deliverables.get(line.deliverable_id)
        if deliverable is not None and Language(deliverable.language) in ctx.languages:
            pairs.append((line, deliverable))
    pairs.sort(key=lambda item: item[0].id)
    return pairs


# ==================================================================
# S5a — TTS preflight (EPISODE, free, fail closed before any spend)
# ==================================================================


class TtsPreflightHandler:
    """Voice/registry/budget checks; raises before the first paid call."""

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _episode_subject(ctx)

    def _texts_for(
        self, ctx: RunContext, episode: EpisodeRow
    ) -> list[tuple[NarrationLineRow, str]]:
        return [
            (line, _text_to_send(ctx, Language(d.language), line.approved_text))
            for line, d in _line_rows(ctx, episode)
        ]

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        texts = [
            (line.id, text) for line, text in self._texts_for(ctx, episode)
        ]
        settings = ctx.config.settings
        try:
            model_key: str | None = ctx.resolved_profile.model_key_for("tts")
        except Exception:
            model_key = None
        voices = {
            lang.value: (
                voice_row.voice_id if (voice_row := _speaker_voice(ctx, episode, lang)) else None
            )
            for lang in ctx.languages
        }
        return _sha(
            {
                "stage": "s5a_tts_preflight",
                "episode_id": subject.id,
                "languages": sorted(lang.value for lang in ctx.languages),
                "profile": ctx.resolved_profile.name,
                "texts": texts,
                "voices": voices,
                "tts_model": model_key,
                "tts_config": {
                    "keep_diacritics": settings.tts.keep_diacritics,
                    "use_context": settings.tts.use_context,
                    "context_max_chars": settings.tts.context_max_chars,
                },
                "words_per_second": settings.timing.words_per_second,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        problems: list[str] = []

        # ---- coverage: words_per_second for every language in use
        for lang in ctx.languages:
            if lang.value not in settings.timing.words_per_second:
                problems.append(
                    f"timing.words_per_second has no entry for language {lang.value!r}"
                )

        # ---- voices: list every missing (speaker, language) pair at once
        narrator = ctx.repos.content.get_narrator(episode.series_id)
        speaker_slug = narrator.role if narrator is not None else "narrator"
        wanted = _wanted_languages(ctx, episode)
        voice_rows: dict[str, CharacterVoiceRow] = {}
        for lang in wanted:
            voice = _speaker_voice(ctx, episode, lang)
            if voice is None:
                if narrator is None:
                    problems.append(
                        f"missing voice for ({speaker_slug}, {lang.value}): no narrator character"
                    )
                else:
                    problems.append(f"missing voice for ({speaker_slug}, {lang.value})")
                continue
            try:
                parse_voice_settings(voice.settings_json)
            except StudioError as exc:
                problems.append(
                    f"voice ({speaker_slug}, {lang.value}) has invalid settings_json: {exc}"
                )
                continue
            voice_rows[lang.value] = voice

        # ---- registry: every voice's effective TTS model must resolve
        entries: dict[str, Any] = {}
        for lang_value, voice in sorted(voice_rows.items()):
            model_key, entry, _parsed = _effective_tts_entry(ctx, voice.settings_json)
            if entry is None:
                problems.append(
                    f"voice ({speaker_slug}, {lang_value}) selects unknown "
                    f"tts model_key {model_key!r}"
                )
            elif entry.placeholder:
                problems.append(
                    f"voice ({speaker_slug}, {lang_value}) resolves to tts model "
                    f"{model_key!r} with an unresolved placeholder model_ref "
                    f"{entry.model_ref!r}"
                )
            else:
                entries[lang_value] = entry

        if problems:
            raise PreflightError(
                "TTS preflight failed before any spend:\n- " + "\n- ".join(problems)
            )

        # ---- final profile: commercial attestation for every used voice
        if Profile(ctx.resolved_profile.name) is Profile.FINAL:
            unattested = [
                lang_value
                for lang_value, voice in sorted(voice_rows.items())
                if not parse_voice_settings(voice.settings_json).license_attested_at
            ]
            if unattested:
                raise PreflightError(
                    f"final profile requires a commercial-use attestation for every "
                    f"voice; missing for: {', '.join(unattested)} "
                    f"(re-run `studio voice set ... --attest-commercial`)"
                )

        # ---- worst-case reservation vs remaining episode/month/global budgets
        texts = self._texts_for(ctx, episode)
        # per-line reservation uses the effective entry of that line's language
        est = 0
        for line, text in texts:
            lang = Language(
                ctx.repos.content.get_deliverable(line.deliverable_id).language  # type: ignore[union-attr]
            )
            est += worst_case_tts_musd(entries[lang.value], [text])
        month_ref = ctx.clock.month_tag()
        global_cap = settings.budgets.global_cap_usd

        def effective_cap(scope: BudgetScope, ref: str, default_cap: int | None) -> int | None:
            """Cap for a scope: the operator-managed budgets row wins over the
            settings default (a kill-switch reset may raise it)."""
            row = ctx.repos.budgets.get(scope.value, ref)
            if row is not None:
                return row.cap_musd
            return default_cap

        caps: list[tuple[BudgetScope, str, int | None]] = [
            (
                BudgetScope.EPISODE,
                episode.id,
                effective_cap(
                    BudgetScope.EPISODE,
                    episode.id,
                    usd_to_musd(str(settings.budgets.default_episode_cap_usd)),
                ),
            ),
            (
                BudgetScope.MONTH,
                month_ref,
                effective_cap(
                    BudgetScope.MONTH,
                    month_ref,
                    usd_to_musd(str(settings.budgets.monthly_cap_usd)),
                ),
            ),
            (
                BudgetScope.GLOBAL,
                GLOBAL_SCOPE_REF,
                effective_cap(
                    BudgetScope.GLOBAL,
                    GLOBAL_SCOPE_REF,
                    usd_to_musd(str(global_cap)) if global_cap is not None else None,
                ),
            ),
        ]
        for scope, ref, cap in caps:
            if cap is None:
                continue
            held = ctx.repos.budgets.held_musd(scope.value, ref)
            if held + est > cap:
                raise BudgetExceeded(
                    f"worst-case TTS reservation ({musd_to_usd(est)} USD) exceeds "
                    f"the remaining {scope.value} budget (cap {musd_to_usd(cap)} USD, "
                    f"held {musd_to_usd(held)} USD); refusing before the first "
                    f"paid call",
                    scope=scope.value,
                    scope_ref=ref,
                    cap_musd=cap,
                    held_musd=held,
                    requested_musd=est,
                )

        return StageOutput(
            output={
                "episode_id": episode.id,
                "lines": len(texts),
                "worst_case_tts_musd": est,
                "languages": sorted(lang.value for lang in ctx.languages),
                "voices": sorted(voice_rows),
            }
        )


# ==================================================================
# S5 — TTS synthesis per line (LINE scope; paid; resumable)
# ==================================================================


class TtsLineHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _line_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        line = ctx.repos.content.get_narration_line(subject.id)
        assert line is not None
        deliverable = ctx.repos.content.get_deliverable(line.deliverable_id)
        assert deliverable is not None
        lang = Language(deliverable.language)
        settings = ctx.config.settings
        episode = _episode(ctx)
        voice = _speaker_voice(ctx, episode, lang) if episode is not None else None
        model_key = None
        tts_params = None
        try:
            model_key = ctx.resolved_profile.model_key_for("tts")
            if voice is not None:
                model_key = parse_voice_settings(voice.settings_json).model_key
            entry = ctx.config.registry.models.get(model_key)
            if entry is not None and entry.tts_params is not None:
                tts_params = entry.tts_params.model_dump(mode="json")
        except Exception as exc:
            logger.debug("tts params/model resolution failed for line %s: %s", subject.id, exc)
        return _sha(
            {
                "stage": "s5_tts",
                "line_id": subject.id,
                "text": _text_to_send(ctx, lang, line.approved_text),
                "language": lang.value,
                "tts_nonce": line.tts_nonce,
                "voice": (
                    {"voice_id": voice.voice_id, "settings_json": voice.settings_json}
                    if voice is not None
                    else None
                ),
                "tts_model": model_key,
                "tts_params": tts_params,
                "tts_config": {
                    "keep_diacritics": settings.tts.keep_diacritics,
                    "use_context": settings.tts.use_context,
                    "context_max_chars": settings.tts.context_max_chars,
                },
                "conform": {
                    "sample_rate": settings.audio.sample_rate,
                    "trim_threshold_db": settings.audio.speech_trim.threshold_db,
                    "trim_keep_ms": settings.audio.speech_trim.keep_ms,
                    "speech_line_lufs": settings.audio.levels.speech_line_lufs,
                    "max_line_gain_db": settings.audio.levels.max_line_gain_db,
                },
            }
        )

    # -- internals --------------------------------------------------------

    def _voice(
        self, ctx: RunContext, episode: EpisodeRow, lang: Language
    ) -> tuple[str, Any, Any, CharacterVoiceRow]:
        narrator = ctx.repos.content.get_narrator(episode.series_id)
        if narrator is None:
            raise PreflightError(
                f"no narrator character for series {episode.series_id}; run S5a preflight"
            )
        voice = ctx.repos.audio.get_character_voice(
            series_id=episode.series_id,
            character_slug=narrator.role,
            language=lang.value,
        )
        if voice is None:
            raise PreflightError(
                f"missing voice for ({narrator.role}, {lang.value}); run S5a preflight"
            )
        model_key, entry, parsed = _effective_tts_entry(ctx, voice.settings_json)
        if entry is None:
            raise StudioError(f"voice selects unknown tts model_key {model_key!r}")
        if entry.placeholder:
            raise PreflightError(
                f"voice ({narrator.role}, {lang.value}) resolves to placeholder tts model "
                f"{model_key!r}; fix model_registry.yaml"
            )
        return model_key, entry, parsed, voice

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        line = ctx.repos.content.get_narration_line(subject.id)
        if line is None:
            raise PermanentProviderError(f"narration line not found: {subject.id}")
        deliverable = ctx.repos.content.get_deliverable(line.deliverable_id)
        if deliverable is None:
            raise PermanentProviderError(
                f"deliverable not found for line {line.id}: {line.deliverable_id}"
            )
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        lang = Language(deliverable.language)
        settings = ctx.config.settings
        sample_rate = int(settings.audio.sample_rate)
        work_dir = Path(settings.paths.projects) / str(ctx.episode_id) / "work"

        model_key, entry, voice_settings, voice_row = self._voice(ctx, episode, lang)

        approved = line.approved_text
        text_to_send = _text_to_send(ctx, lang, approved)

        # ---- context (previous/next), only when the model supports it
        all_lines = ctx.repos.content.narration_lines(deliverable.id)
        shot_order = {
            shot.id: shot.shot_no
            for shot in ctx.repos.content.shots_for_track(deliverable.track_id)
        }
        ordered = sorted(
            all_lines,
            key=lambda item: (shot_order.get(item.shot_id, 1 << 30), item.line_no),
        )
        index = next(i for i, item in enumerate(ordered) if item.id == line.id)
        supports_context = bool(
            entry.tts_params and entry.tts_params.supports_context
        )
        limit = int(settings.tts.context_max_chars)
        previous_text: str | None = None
        next_text: str | None = None
        if settings.tts.use_context and supports_context:
            previous_text = ordered[index - 1].approved_text[-limit:] if index > 0 else None
            next_text = (
                ordered[index + 1].approved_text[:limit]
                if index + 1 < len(ordered)
                else None
            )

        cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)
        words_per_second = settings.timing.words_per_second.get(lang.value)
        if words_per_second is None:
            raise ConfigError(
                f"timing.words_per_second has no entry for language {lang.value!r}"
            )
        expected_s = count_words(lang, text_to_send) / float(words_per_second)
        qc = settings.tts.qc
        auto_retries = int(qc.auto_retries)

        base_nonce = _base_nonce(line.tts_nonce)
        current_nonce = line.tts_nonce
        regenerations = int(current_nonce[len(base_nonce) + 2 :]) if current_nonce.startswith(
            f"{base_nonce}{_NONCE_RETRY_MARKER}"
        ) else 0
        paid_calls = 0
        last_qc: dict[str, Any] = {}
        conformed = None
        asset = None
        while conformed is None:
            # persist the nonce BEFORE the paid call (single statement)
            ctx.repos.content.set_line_tts_nonce(line.id, current_nonce)
            request = TTSRequest(
                text=text_to_send,
                voice_id=voice_row.voice_id,
                language=lang,
                settings=request_settings(voice_settings),
                previous_text=previous_text,
                next_text=next_text,
            )
            result = await ctx.gateway.tts(
                model_key,
                request,
                CallContext(
                    episode_id=episode.id,
                    stage_run_id=ctx.stage_run_id,
                    nonce=current_nonce,
                    purpose="narration",
                ),
            )
            paid_calls += 1
            asset = result.assets[0]
            raw_path = cas.resolve(asset.sha256)
            try:
                conformed = await conform_line(
                    str(raw_path),
                    cas=cas,
                    settings=settings,
                    mix_gain_db=voice_settings.mix_gain_db,
                    work_dir=str(work_dir),
                )
            finally:
                # raw TTS audio is kept (retention keep_n_days) so re-mixing
                # never repays; the gateway stores it as intermediate, so the
                # re-put upgrades the row in place (D1: never downgrades).
                ext = asset.rel_path.rsplit(".", 1)[-1] if "." in asset.rel_path else "audio"
                purge_after = format_ts(
                    ctx.clock.now()
                    + timedelta(days=int(settings.retention.keep_narration_audio_days))
                )
                cas.put_file(
                    raw_path,
                    kind="audio",
                    ext=ext,
                    retention="keep_n_days",
                    purge_after=purge_after,
                    move=False,
                )

            # ---- TTS QC: duration ratio + silence floor
            with wave.open(str(cas.resolve(conformed.asset.sha256)), "rb") as handle:
                frames = handle.readframes(handle.getnframes())
            samples = s16_le_samples(frames)
            rms = rms_dbfs(samples)
            conformed_s = conformed.samples / sample_rate
            ratio = (conformed_s / expected_s) if expected_s > 0 else 0.0
            last_qc = {
                "expected_s": expected_s,
                "conformed_s": conformed_s,
                "ratio": ratio,
                "rms_dbfs": rms,
            }
            qc_ok = (
                qc.min_duration_ratio <= ratio <= qc.max_duration_ratio
                and rms >= float(settings.audio.qc.silent_line_rms_db)
            )
            if qc_ok:
                break
            if regenerations >= auto_retries:
                raise TtsQualityError(
                    f"line {line.id} failed TTS QC after {paid_calls} paid attempt(s): "
                    f"expected {expected_s:.2f}s ({count_words(lang, text_to_send)} words @ "
                    f"{words_per_second} w/s), conformed {conformed_s:.2f}s "
                    f"(ratio {ratio:.2f}, bounds [{qc.min_duration_ratio}, "
                    f"{qc.max_duration_ratio}]), rms {rms:.1f} dBFS "
                    f"(floor {settings.audio.qc.silent_line_rms_db} dBFS)",
                )
            regenerations += 1
            current_nonce = f"{base_nonce}{_NONCE_RETRY_MARKER}{regenerations}"
            conformed = None  # regenerate with a fresh nonce (deliberately paid)

        assert asset is not None and conformed is not None
        ctx.repos.content.mark_line_voiced(
            line.id,
            asset_sha256=asset.sha256,
            duration_ms=asset.duration_ms,
            conformed_sha256=conformed.asset.sha256,
            conformed_samples=conformed.samples,
            voice_snapshot_json=voice_snapshot(
                language=lang,
                voice_id=voice_row.voice_id,
                settings=voice_settings,
                model_key=model_key,
                model_id=entry.model_id,
            ),
        )
        return StageOutput(
            output={
                "line_id": line.id,
                "deliverable_id": deliverable.id,
                "language": lang.value,
                "nonce": current_nonce,
                "paid_calls": paid_calls,
                "conformed_samples": conformed.samples,
                "duration_ms": asset.duration_ms,
                "qc": last_qc,
            }
        )


# ==================================================================
# S6 — timing reconciler (TRACK scope; free, local)
# ==================================================================


class TimingReconcilerHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _track_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        track = ctx.repos.content.get_track(subject.id)
        assert track is not None
        episode = _episode(ctx)
        assert episode is not None
        deliverables = [
            d
            for d in ctx.repos.content.list_deliverables(episode.id)
            if d.track_id == track.id
        ]
        lines: list[dict[str, Any]] = []
        for d in deliverables:
            for line in ctx.repos.content.narration_lines(d.id):
                lines.append(
                    {
                        "id": line.id,
                        "deliverable_id": d.id,
                        "shot_id": line.shot_id,
                        "conformed_samples": line.conformed_samples,
                        "approved_text": line.approved_text,
                    }
                )
        shots = ctx.repos.content.shots_for_track(track.id)
        settings = ctx.config.settings
        return _sha(
            {
                "stage": "s6_timing",
                "track_id": subject.id,
                "shots": [
                    {"id": s.id, "shot_no": s.shot_no, "min_duration_ms": s.min_duration_ms}
                    for s in shots
                ],
                "lines": lines,
                "sample_rate": settings.audio.sample_rate,
                "fps": settings.render.fps,
                "timing": {
                    "max_stretch": settings.timing.max_stretch,
                    "max_hold_ms": settings.timing.max_hold_ms,
                    "prefer_stretch_to_save_cost": settings.timing.prefer_stretch_to_save_cost,
                    "lead_in_ms": settings.timing.lead_in_ms,
                    "line_gap_ms": settings.timing.line_gap_ms,
                    "tail_ms": settings.timing.tail_ms,
                },
                "supported_durations_s": self._supported_durations(ctx),
            }
        )

    # -- timing math --------------------------------------------------------

    @staticmethod
    def _samples_per_frame(settings: Any) -> int:
        sample_rate = int(settings.audio.sample_rate)
        fps = int(settings.render.fps)
        if fps <= 0 or sample_rate % fps != 0:
            raise ConfigError(
                f"audio.sample_rate ({sample_rate}) must be an integer multiple of "
                f"render.fps ({fps})"
            )
        return sample_rate // fps

    @staticmethod
    def _supported_durations(ctx: RunContext) -> list[int]:
        video_key = ctx.resolved_profile.model_key_for("video")
        entry = ctx.resolved_profile.entries.get("video")
        durations = list(entry.supported_durations_s) if entry is not None else []
        if not durations:
            raise StudioError(
                f"video model {video_key!r} declares no supported_durations_s; "
                f"the timing reconciler cannot pick a clip duration"
            )
        return sorted(set(int(d) for d in durations))

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        from decimal import Decimal

        if ctx.config.settings.timing.j_cut_ms != 0:
            raise ConfigError("timing.j_cut_ms is not supported before Phase 5; set it to 0")

        track = ctx.repos.content.get_track(subject.id)
        if track is None:
            raise PermanentProviderError(f"track not found: {subject.id}")
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        fps = int(settings.render.fps)
        timing = settings.timing
        samples_per_frame = self._samples_per_frame(settings)
        sample_rate = int(settings.audio.sample_rate)
        durations = self._supported_durations(ctx)  # seconds
        supported_ms = [d * 1000 for d in durations]
        supported_frames = [d * fps for d in durations]

        shots = sorted(
            ctx.repos.content.shots_for_track(track.id), key=lambda item: item.shot_no
        )
        deliverables = sorted(
            (
                d
                for d in ctx.repos.content.list_deliverables(episode.id)
                if d.track_id == track.id and Language(d.language) in ctx.languages
            ),
            key=lambda item: (item.language, item.id),
        )
        # lines per (deliverable, shot)
        lines_by_deliverable: dict[str, list[NarrationLineRow]] = {
            d.id: ctx.repos.content.narration_lines(d.id) for d in deliverables
        }

        lead_in_samples = int(timing.lead_in_ms) * sample_rate // 1000
        gap_samples = int(timing.line_gap_ms) * sample_rate // 1000
        tail_samples = int(timing.tail_ms) * sample_rate // 1000

        def shot_span_samples(deliverable_id: str, shot_id: str) -> int:
            shot_lines = [
                line
                for line in lines_by_deliverable.get(deliverable_id, [])
                if line.shot_id == shot_id
            ]
            total = lead_in_samples
            for index, line in enumerate(shot_lines):
                if line.conformed_samples is None:
                    raise StudioError(
                        f"line {line.id} has no conformed audio; run S5 first"
                    )
                total += int(line.conformed_samples)
                if index + 1 < len(shot_lines):
                    total += gap_samples
            return total + tail_samples

        plan: list[dict[str, Any]] = []
        overflows: list[dict[str, Any]] = []
        for shot in shots:
            need_frames = 0
            min_frames = (
                math.ceil(int(shot.min_duration_ms) * fps / 1000)
                if shot.min_duration_ms is not None
                else 0
            )
            for d in deliverables:
                span = shot_span_samples(d.id, shot.id)
                need_frames = max(need_frames, math.ceil(span / samples_per_frame))
            need_frames = max(need_frames, min_frames)
            need_ms = math.ceil(need_frames * 1000 / fps)

            max_hold_frames = int(timing.max_hold_ms) * fps // 1000

            def capacity_stretch(frames: int, *, hold: int = max_hold_frames) -> int:
                return math.ceil(frames * float(timing.max_stretch)) + hold

            plain: int | None = None
            squeeze: int | None = None
            for duration_ms, clip_frames in zip(supported_ms, supported_frames, strict=True):
                if need_frames <= clip_frames:
                    plain = duration_ms
                    break
            if timing.prefer_stretch_to_save_cost:
                for duration_ms, clip_frames in zip(supported_ms, supported_frames, strict=True):
                    if need_frames <= capacity_stretch(clip_frames):
                        squeeze = duration_ms
                        break
            chosen: int | None
            if squeeze is not None and (plain is None or squeeze < plain):
                chosen = squeeze
            else:
                chosen = plain

            if chosen is None:
                max_clip_frames = supported_frames[-1]
                capacity_ms = math.ceil(capacity_stretch(max_clip_frames) * 1000 / fps)
                offending = [
                    {
                        "line_id": line.id,
                        "conformed_samples": line.conformed_samples,
                    }
                    for d in deliverables
                    for line in lines_by_deliverable.get(d.id, [])
                    if line.shot_id == shot.id
                ]
                overflows.append(
                    {
                        "shot_id": shot.id,
                        "shot_no": shot.shot_no,
                        "needed_ms": need_ms,
                        "max_capacity_ms": capacity_ms,
                        "offending_lines": offending,
                    }
                )
                continue

            clip_ms = chosen
            clip_frames = clip_ms * fps // 1000
            if need_frames <= clip_frames:
                speed = 1.0
            else:
                speed = max(1.0 / float(timing.max_stretch), clip_ms / need_ms)
            effective_frames = math.ceil(clip_frames / speed)
            hold_frames = max(0, need_frames - effective_frames)
            plan.append(
                {
                    "shot": shot,
                    "need_frames": need_frames,
                    "need_ms": need_ms,
                    "clip_duration_ms": clip_ms,
                    "speed_factor": speed,
                    "hold_frames": hold_frames,
                    "effective_frames": effective_frames,
                }
            )

        if overflows:
            for overflow in overflows:
                shot_row = next(s for s in shots if s.id == overflow["shot_id"])
                ctx.repos.content.set_shot_timing(
                    shot_row.id,
                    clip_duration_ms=supported_ms[-1],
                    speed_factor=1.0 / float(timing.max_stretch),
                    hold_frames=0,
                    timing_status="overflow",
                )
            details = "; ".join(
                f"shot {item['shot_no']} ({item['shot_id']}): needs {item['needed_ms']} ms, "
                f"max capacity {item['max_capacity_ms']} ms "
                f"({len(item['offending_lines'])} line(s))"
                for item in overflows
            )
            raise TimingOverflowError(
                f"narration exceeds clip capacity for {len(overflows)} shot(s): "
                f"{details}. Shorten or split the narration (studio narration edit); "
                f"no text was modified."
            )

        for item in plan:
            ctx.repos.content.set_shot_timing(
                item["shot"].id,
                clip_duration_ms=int(item["clip_duration_ms"]),
                speed_factor=float(item["speed_factor"]),
                hold_frames=int(item["hold_frames"]),
                timing_status="locked",
            )

        # ---- projections (video seconds + worst-case cost for the profile's model)
        projected_seconds = sum(int(item["clip_duration_ms"]) for item in plan) / 1000.0
        projected_cost: int | None = None
        video_note = None
        try:
            video_key = ctx.resolved_profile.model_key_for("video")
            video_entry = ctx.resolved_profile.entries.get("video")
            if video_entry is not None and isinstance(video_entry.pricing, PerSecond):
                projected_cost = usd_to_musd(
                    Decimal(str(projected_seconds)) * Decimal(str(video_entry.pricing.usd))
                )
            else:
                video_note = f"video model {video_key!r} is not per_second priced"
        except Exception as exc:  # resolution failure must not fail the lock
            video_note = f"video projection unavailable: {exc}"

        # ---- optional Gate-1 baseline drift comparison (S4 stores none today)
        drift: dict[str, Any] | None = None
        baselines: list[int] = []
        for deliverable in deliverables:
            baseline_output = _stage_output(ctx, "metadata_safety", deliverable.id)
            if not isinstance(baseline_output, dict):
                continue
            for key in ("projected_video_cost_musd", "gate1_video_estimate_musd"):
                value = baseline_output.get(key)
                if isinstance(value, int) and value >= 0:
                    baselines.append(value)
                    break
        if baselines and projected_cost is not None:
            baseline = max(baselines)
            warn_pct = float(timing.estimate_drift_warn_pct)
            if baseline == 0:
                exceeded = projected_cost > 0
                drift_pct: float | None = None if projected_cost == 0 else math.inf
            else:
                drift_pct = (projected_cost - baseline) * 100.0 / baseline
                exceeded = drift_pct > warn_pct
            if exceeded:
                ctx.repos.audit.append(
                    actor="system",
                    entity=f"episode:{episode.id}",
                    event="estimate_drift",
                    payload={
                        "episode_id": episode.id,
                        "track_id": track.id,
                        "baseline_musd": baseline,
                        "projected_musd": projected_cost,
                        "drift_pct": drift_pct,
                        "estimate_drift_warn_pct": warn_pct,
                    },
                )
                drift = {
                    "baseline_musd": baseline,
                    "projected_musd": projected_cost,
                    "drift_pct": drift_pct,
                }

        output: dict[str, Any] = {
            "track_id": track.id,
            "aspect": track.aspect,
            "shots_locked": len(plan),
            "shot_ids": [item["shot"].id for item in plan],
            "projected_video_seconds": projected_seconds,
            "projected_video_cost_musd": projected_cost,
            "video_note": video_note,
        }
        if drift is not None:
            output["estimate_drift"] = drift
        return StageOutput(output=output)


# ==================================================================
# S7 — audio plan (DELIVERABLE, deterministic, seeded)
# ==================================================================


def _deliverable_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    deliverables = {d.id: d for d in ctx.repos.content.list_deliverables(episode.id)}
    subjects: list[SubjectRef] = []
    for d in deliverables.values():
        if Language(d.language) not in ctx.languages:
            continue
        subjects.append(
            SubjectRef(
                scope=Scope.DELIVERABLE,
                id=d.id,
                lineage=(
                    (Scope.SERIES, episode.series_id),
                    (Scope.EPISODE, episode.id),
                    (Scope.TRACK, d.track_id),
                ),
            )
        )
    subjects.sort(key=lambda item: item.id)
    return subjects


def _library_row_payload(row: Any) -> dict[str, Any]:
    return {
        "sha256": row.sha256,
        "kind": row.kind,
        "moods": sorted(json.loads(row.moods_json or "[]")),
        "intensity": row.intensity,
        "loopable": int(row.loopable),
        "duration_ms": int(row.duration_ms),
        "loudness_lufs": row.loudness_lufs,
        "attribution_required": int(row.attribution_required),
    }


class AudioPlanHandler:
    """Build the music/ambience/SFX cue plan for one deliverable (§H8).

    Pure function of (timeline, scenes, active library, recent-use history,
    config) — deterministic across runs and across languages of the same
    episode (the tie-break key is per *episode*, not per deliverable).
    """

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _deliverable_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        track = ctx.repos.content.get_track(deliverable.track_id)
        assert track is not None
        settings = ctx.config.settings
        try:
            timeline_hash = build_timeline(ctx.repos.content, subject.id, settings).timeline_hash
        except Exception as exc:
            # run() will surface the real error; keep hashing deterministic
            timeline_hash = f"timeline-error:{exc}"
        scenes = ctx.repos.content.list_scenes(track.id)
        library = {
            kind: sorted(
                (_library_row_payload(row)
                 for row in ctx.repos.audio.list_library(kind=kind)),
                key=lambda row: row["sha256"],
            )
            for kind in ("music", "ambience", "sfx")
        }
        episode = _episode(ctx)
        assert episode is not None
        recent = self._recent_music_uses(ctx, episode.series_id, episode.id,
                                          settings.audio.music.recent_penalty_episodes)
        return _sha(
            {
                "stage": "s7_audio_plan",
                "deliverable_id": subject.id,
                "timeline_hash": timeline_hash,
                "render_fps": settings.render.fps,
                "sample_rate": settings.audio.sample_rate,
                "scenes": [
                    {
                        "scene_no": s.scene_no,
                        "shot_from": s.shot_from,
                        "shot_to": s.shot_to,
                        "music_mood": s.music_mood,
                        "intensity": s.intensity,
                        "ambience_tags": json.loads(s.ambience_tags_json or "[]"),
                    }
                    for s in scenes
                ],
                "library": library,
                "recent_music_uses": recent,
                "config": {
                    "levels": settings.audio.levels.model_dump(mode="json"),
                    "music": settings.audio.music.model_dump(mode="json"),
                    "sfx": settings.audio.sfx.model_dump(mode="json"),
                },
                "mood_affinity": ctx.config.vocab.mood_affinity,
            }
        )

    @staticmethod
    def _recent_music_uses(
        ctx: RunContext, series_id: str, current_episode_id: str, window: int
    ) -> list[str]:
        """shas used in the last `window` episodes of the series (excluding the current one)."""
        uses = ctx.repos.audio.music_uses_in_series(series_id)
        ordered: list[str] = []
        seen: set[str] = set()
        for episode_id, _created_at, _sha_used in uses:
            if episode_id == current_episode_id:
                continue
            if episode_id not in seen:
                ordered.append(episode_id)
                seen.add(episode_id)
        window_episodes = set(ordered[-window:]) if window > 0 else set()
        return sorted({sha for ep, _ts, sha in uses if ep in window_episodes})

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        if deliverable is None:
            raise PermanentProviderError(f"deliverable not found: {subject.id}")
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        track = ctx.repos.content.get_track(deliverable.track_id)
        if track is None:
            raise PermanentProviderError(f"track not found: {deliverable.track_id}")
        settings = ctx.config.settings
        fps = int(settings.render.fps)

        timeline = build_timeline(ctx.repos.content, subject.id, settings)
        scenes = ctx.repos.content.list_scenes(track.id)
        pieces = scene_pieces(timeline, scenes)
        library = {
            kind: ctx.repos.audio.list_library(kind=kind) for kind in ("music", "ambience", "sfx")
        }
        recent = set(
            self._recent_music_uses(
                ctx, episode.series_id, episode.id, settings.audio.music.recent_penalty_episodes
            )
        )
        plan = plan_audio(
            episode_id=episode.id,
            total_frames=timeline.total_frames,
            fps=fps,
            sample_rate=int(settings.audio.sample_rate),
            timeline_hash=timeline.timeline_hash,
            pieces=pieces,
            music_rows=library["music"],
            ambience_rows=library["ambience"],
            sfx_rows=library["sfx"],
            recent_shas=recent,
            settings=settings,
            vocab=ctx.config.vocab,
        )

        cues = [
            NewAudioCue(
                cue_index=cue.cue_index,
                kind=cue.kind,
                library_sha256=cue.library_sha256,
                scene_id=cue.scene_id,
                start_frame=cue.start_frame,
                end_frame=cue.end_frame,
                source_offset_ms=cue.source_offset_ms,
                gain_db=cue.gain_db,
                fade_in_ms=cue.fade_in_ms,
                fade_out_ms=cue.fade_out_ms,
                params_json=json.dumps(cue.params, ensure_ascii=False, sort_keys=True),
            )
            for cue in plan.cues
        ]
        ctx.repos.audio.replace_cues(subject.id, cues, plan.plan_hash)

        for row in plan.attribution:
            ctx.repos.audio.add_qc_result(
                deliverable_id=subject.id,
                subject_scope="deliverable",
                subject_id=subject.id,
                check_name="license_attribution",
                status="pass",
                detail_json=json.dumps(
                    {
                        "sha256": row.sha256,
                        "title": row.title,
                        "attribution_text": row.attribution_text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        for warning in plan.warnings:
            check_name = warning.split(":", 1)[0]
            ctx.repos.audio.add_qc_result(
                deliverable_id=subject.id,
                subject_scope="deliverable",
                subject_id=subject.id,
                check_name=check_name,
                status="warn",
                detail_json=json.dumps({"warning": warning}, sort_keys=True),
            )
        return StageOutput(
            output={
                "deliverable_id": subject.id,
                "plan_hash": plan.plan_hash,
                "timeline_hash": timeline.timeline_hash,
                "counts": plan.counts,
                "warnings": list(plan.warnings),
                "attribution_required": [row.sha256 for row in plan.attribution],
            }
        )


class AudioMixHandler:
    """Render, master and QC the deliverable's audio (§H12, stage S7b).

    Reuse semantics: if an ``audio_masters`` row already exists for
    ``(deliverable_id, input_hash)`` with a *live* master asset, the stage
    re-renders nothing (Phase 2's T-ENG-13 crash-resume invariant relies on
    this). Otherwise it re-renders deterministically, re-masters and
    re-QCs; failures raise before any ``audio_masters`` row is written and
    before the master FLAC reaches the CAS (the premaster and stems never
    leave ``work_dir``/CAS-intermediate respectively).
    """

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _deliverable_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        track = ctx.repos.content.get_track(deliverable.track_id)
        assert track is not None
        settings = ctx.config.settings
        try:
            timeline = build_timeline(ctx.repos.content, subject.id, settings)
            timeline_payload: dict[str, Any] = {
                "timeline_hash": timeline.timeline_hash,
                "total_samples": timeline.total_samples,
                "total_frames": timeline.total_frames,
                "line_shas": [line.conformed_sha256 for line in timeline.lines],
            }
        except Exception as exc:
            # run() surfaces the real error; hashing must stay deterministic
            timeline_payload = {"timeline_error": str(exc)}
        cues = ctx.repos.audio.list_cues(subject.id)
        cue_payload = [
            {
                "kind": cue.kind,
                "library_sha256": cue.library_sha256,
                "start_frame": cue.start_frame,
                "end_frame": cue.end_frame,
                "source_offset_ms": cue.source_offset_ms,
                "gain_db": round(float(cue.gain_db), 3),
                "fade_in_ms": cue.fade_in_ms,
                "fade_out_ms": cue.fade_out_ms,
                "params": json.loads(cue.params_json or "{}"),
            }
            for cue in cues
        ]
        plan_hash = cues[0].plan_hash if cues else None
        return _sha(
            {
                "stage": "s7b_audio_mix",
                "deliverable_id": subject.id,
                "timeline": timeline_payload,
                "plan_hash": plan_hash,
                "cues": cue_payload,
                "config": {
                    "duck": settings.audio.duck.model_dump(mode="json"),
                    "master": settings.audio.master.model_dump(mode="json"),
                    "render_fps": settings.render.fps,
                    "sample_rate": settings.audio.sample_rate,
                },
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        if deliverable is None:
            raise PermanentProviderError(f"deliverable not found: {subject.id}")
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        track = ctx.repos.content.get_track(deliverable.track_id)
        if track is None:
            raise PermanentProviderError(f"track not found: {deliverable.track_id}")
        settings = ctx.config.settings
        fps = int(settings.render.fps)
        sample_rate = int(settings.audio.sample_rate)

        timeline = build_timeline(ctx.repos.content, subject.id, settings)
        input_hash = self.input_hash(ctx, subject)

        cas = ContentStore(Path(settings.paths.cas), ctx.db, ctx.clock)

        existing = ctx.repos.audio.get_ready_master(
            deliverable_id=subject.id, input_hash=input_hash
        )
        if existing is not None:
            try:
                cas.resolve(existing.master_sha256)
            except AssetNotFoundError:
                logger.info(
                    "audio master %s lost its asset (purged?); re-rendering",
                    existing.master_sha256[:12],
                )
            else:
                return StageOutput(
                    output={
                        "deliverable_id": subject.id,
                        "master_sha256": existing.master_sha256,
                        "narration_stem_sha256": existing.narration_stem_sha256,
                        "total_samples": existing.total_samples,
                        "timeline_hash": timeline.timeline_hash,
                        "loudness_i": existing.loudness_i,
                        "duck_depth_db": existing.duck_depth_db,
                        "reused": True,
                    }
                )

        work_dir = (
            Path(settings.paths.projects)
            / str(ctx.episode_id)
            / "work"
            / f"s7b_{subject.id}"
        )
        await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)

        # ---- narration stem -> CAS (intermediate)
        cas_paths = {
            line.conformed_sha256: str(cas.resolve(line.conformed_sha256))
            for line in timeline.lines
        }
        stem_path = work_dir / "narration_stem.wav"
        await asyncio.to_thread(build_narration_stem, timeline, cas_paths, str(stem_path))
        stem_asset = await asyncio.to_thread(
            cas.put_file, stem_path, kind="audio", ext="wav",
            retention="intermediate", move=False,
        )
        stem_cas_path = str(cas.resolve(stem_asset.sha256))

        # ---- cues -> render items (resolve files from the library root)
        cue_rows = ctx.repos.audio.list_cues(subject.id)
        library_root = resolve_library_root(settings)
        library: dict[str, Any] = {}
        library_paths: dict[str, str] = {}
        for row in cue_rows:
            entry = ctx.repos.audio.get_library_entry(row.library_sha256)
            if entry is None:
                raise PermanentProviderError(
                    f"cue references library sha256 {row.library_sha256} "
                    f"which is no longer in audio_library"
                )
            candidate = str(library_root / entry.rel_path)
            if not await asyncio.to_thread(Path(candidate).is_file):
                raise PermanentProviderError(
                    f"library file missing on disk: {candidate} "
                    f"(sha256 {entry.sha256[:12]})"
                )
            library[entry.sha256] = entry
            library_paths[entry.sha256] = candidate
        cues = cues_from_rows(cue_rows, library_paths)

        duck_settings = settings.audio.duck
        duck = DuckSpec(
            mode=duck_settings.mode,
            depth_db=float(duck_settings.depth_db),
            pre_ms=float(duck_settings.pre_ms),
            attack_ms=float(duck_settings.attack_ms),
            release_ms=float(duck_settings.release_ms),
            merge_gap_ms=float(duck_settings.merge_gap_ms),
            ambience_fraction=float(duck_settings.ambience_fraction),
            sidechain_threshold=float(duck_settings.sidechain.threshold),
            sidechain_ratio=float(duck_settings.sidechain.ratio),
            sidechain_attack_ms=float(duck_settings.sidechain.attack_ms),
            sidechain_release_ms=float(duck_settings.sidechain.release_ms),
            sidechain_makeup=float(duck_settings.sidechain.makeup),
        )
        placements = [
            (int(line.start_sample), int(line.length_samples)) for line in timeline.lines
        ]

        # ---- render premaster (graph in a file; music-bus stems for QC)
        mix_result = await render_premaster(
            stem_path=Path(stem_cas_path),
            cues=cues,
            duck=duck,
            placements=placements,
            total_samples=timeline.total_samples,
            fps=fps,
            sample_rate=sample_rate,
            work_dir=work_dir,
            emit_music_bus_stems=True,
        )

        # ---- two-pass loudnorm master (24-bit FLAC)
        master_tmp = work_dir / "master.flac"
        master = await master_two_pass(
            mix_result.premaster_path,
            master_tmp,
            target_lufs=float(settings.audio.master.target_lufs),
            true_peak_db=float(settings.audio.master.true_peak_db),
            lra=float(settings.audio.master.lra),
            lufs_tolerance=float(settings.audio.master.lufs_tolerance),
            tp_ceiling_db=float(settings.audio.master.tp_ceiling_db),
            clip_peak_db=float(settings.audio.master.clip_peak_db),
            sample_rate=sample_rate,
            total_samples=timeline.total_samples,
        )

        # ---- persisted audio QC
        no_mood_match_warn = any(
            row.check_name == "music_no_mood_match"
            for row in ctx.repos.audio.list_qc_results(
                deliverable_id=subject.id, check_name="music_no_mood_match"
            )
        )
        qc_inputs = AudioQcInputs(
            deliverable_id=subject.id,
            master_path=str(master_tmp),
            narration_stem_path=stem_cas_path,
            total_samples=timeline.total_samples,
            sample_rate=sample_rate,
            placements=placements,
            cues=[
                {
                    "kind": cue.kind,
                    "library_sha256": cue.library_sha256,
                    "start_frame": cue.start_frame,
                    "end_frame": cue.end_frame,
                }
                for cue in cue_rows
            ],
            library={
                sha: {
                    "title": entry.title,
                    "license": entry.license,
                    "attribution_required": bool(entry.attribution_required),
                }
                for sha, entry in library.items()
            },
            fps=fps,
            music_pre_path=str(mix_result.music_pre_duck) if mix_result.music_pre_duck else None,
            music_post_path=str(mix_result.music_post_duck) if mix_result.music_post_duck else None,
            no_mood_match_warn=no_mood_match_warn,
        )
        qc_spec = QcSpec(
            min_duck_db=float(settings.audio.qc.min_duck_db),
            min_speech_to_music_db=float(settings.audio.qc.min_speech_to_music_db),
            silent_line_rms_db=float(settings.audio.qc.silent_line_rms_db),
            window_ms=int(settings.audio.qc.window_ms),
            exclude_onset_ms=int(settings.audio.qc.exclude_onset_ms),
            target_lufs=float(settings.audio.master.target_lufs),
            lufs_tolerance=float(settings.audio.master.lufs_tolerance),
            tp_ceiling_db=float(settings.audio.master.tp_ceiling_db),
            clip_peak_db=float(settings.audio.master.clip_peak_db),
        )
        results = await run_audio_qc(qc_inputs, qc_spec)
        for qc_row in qc_rows(subject.id, results):
            ctx.repos.audio.add_qc_result(**qc_row)
        qc_failures = [result for result in results if result.status == "fail"]
        if qc_failures:
            summary = ", ".join(
                f"{result.name}: {json.dumps(result.detail, ensure_ascii=False, sort_keys=True)}"
                for result in qc_failures
            )
            raise AudioQcError(
                f"audio QC failed for deliverable {subject.id}: {summary}"
            )

        # ---- success: master into the CAS, insert the row, supersede the rest
        master_asset = await asyncio.to_thread(
            cas.put_file, master_tmp, kind="audio", ext="flac",
            retention="intermediate", move=True,
        )
        duck_depth = next(
            (
                float(result.detail["avg_duck_db"])
                for result in results
                if result.name == "duck_depth" and result.detail.get("avg_duck_db") is not None
            ),
            None,
        )
        master_row = ctx.repos.audio.insert_master(
            deliverable_id=subject.id,
            input_hash=input_hash,
            timeline_hash=timeline.timeline_hash,
            master_sha256=master_asset.sha256,
            narration_stem_sha256=stem_asset.sha256,
            total_samples=timeline.total_samples,
            loudness_i=master.loudness_i,
            true_peak=master.true_peak,
            lra=master.lra,
            duck_depth_db=duck_depth,
            stats_json=json.dumps(
                {
                    "loudness_i": master.loudness_i,
                    "true_peak": master.true_peak,
                    "lra": master.lra,
                    "samples": master.samples,
                    "qc": [
                        {"name": r.name, "status": r.status, "detail": r.detail}
                        for r in results
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        superseded = ctx.repos.audio.supersede_masters(
            deliverable_id=subject.id, keep_id=master_row.id
        )
        return StageOutput(
            output={
                "deliverable_id": subject.id,
                "master_sha256": master_asset.sha256,
                "narration_stem_sha256": stem_asset.sha256,
                "total_samples": timeline.total_samples,
                "timeline_hash": timeline.timeline_hash,
                "loudness_i": master.loudness_i,
                "duck_depth_db": duck_depth,
                "superseded_masters": superseded,
                "reused": False,
            }
        )
