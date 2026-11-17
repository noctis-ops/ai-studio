"""Demo stage handlers. All external calls flow through the Gateway;
all handlers are idempotent.

Stages (BEGIN_PROMPT §H8):
    demo_script     episode      -> llm (demo_shots) -> creates shots per track
    demo_voice      deliverable  -> materializes narration_lines (Phase 3: TTS moved to s5_tts)
    demo_keyframes  shot         -> image per shot (current shot_attempts nonce)
    demo_clips      shot         -> video per shot (keyframe + narration duration)
    demo_finalize   deliverable  -> manifest JSON asset
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from studio.domain.models import EpisodeRow, ShotAttemptRow, ShotRow, TrackRow
    from studio.pipeline.context import RunContext

from studio.core.canonical import canonical_json
from studio.core.gateway import CallContext
from studio.domain.enums import Aspect, GateKind, Scope
from studio.domain.errors import PermanentProviderError
from studio.domain.refs import AssetRef, SubjectRef
from studio.pipeline.stage import StageOutput, StageSpec
from studio.pipeline.stages_audio import (
    AudioMixHandler,
    AudioPlanHandler,
    TimingReconcilerHandler,
    TtsLineHandler,
    TtsPreflightHandler,
)
from studio.providers.base import LLMRequest, MediaRequest

_SCRIPT_SCHEMA = "demo_shots"
_MANIFEST_VERSION = 1


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _seed_from_nonce(nonce: str) -> int:
    return int(hashlib.sha256(nonce.encode("utf-8")).hexdigest(), 16) % (2**31)


def _episode(ctx: RunContext) -> EpisodeRow | None:
    if not ctx.episode_id:
        return None
    return ctx.repos.content.get_episode(ctx.episode_id)


def _shot_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    shots = ctx.repos.content.list_shots(episode.id)
    return [
        SubjectRef(
            scope=Scope.SHOT,
            id=shot.id,
            lineage=(
                (Scope.SERIES, episode.series_id),
                (Scope.EPISODE, episode.id),
                (Scope.TRACK, shot.track_id),
            ),
        )
        for shot in sorted(shots, key=lambda s: (s.track_id, s.shot_no))
    ]


def _deliverable_subjects(ctx: RunContext) -> list[SubjectRef]:
    episode = _episode(ctx)
    if episode is None:
        return []
    deliverables = ctx.repos.content.list_deliverables(episode.id)
    return [
        SubjectRef(
            scope=Scope.DELIVERABLE,
            id=d.id,
            lineage=(
                (Scope.SERIES, episode.series_id),
                (Scope.EPISODE, episode.id),
                (Scope.TRACK, d.track_id),
            ),
        )
        for d in sorted(deliverables, key=lambda d: (d.track_id, d.language))
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


def _quantize_duration(needed_s: float, supported: list[int]) -> int:
    if not supported:
        return max(1, int(needed_s) if needed_s == int(needed_s) else int(needed_s) + 1)
    ordered = sorted(supported)
    for duration in ordered:
        if duration >= needed_s:
            return duration
    return ordered[-1]


def narration_text(shot: ShotRow) -> str:
    return f"shot {shot.shot_no}: {shot.prompt}"


# ==================================================================
# demo_script
# ==================================================================


def _ensure_scenes(ctx: RunContext, track_id: str) -> None:
    """Backfill deterministic demo scenes (idempotent).

    Phase 2 never created scene rows; Phase 3's S7 audio plan needs them for
    music segmentation. One scene per shot: the first two shots are
    'hopeful'/intensity 2 with room tone, the rest 'tense'/intensity 4 with
    wind. Tags match the generated demo library (demo/library.py).
    """
    if ctx.repos.content.list_scenes(track_id):
        return
    shots = sorted(
        ctx.repos.content.shots_for_track(track_id), key=lambda shot: shot.shot_no
    )
    for shot in shots:
        if shot.shot_no <= 2:
            mood, intensity, tags = "hopeful", 2, ["room_tone"]
        else:
            mood, intensity, tags = "tense", 4, ["wind"]
        ctx.repos.content.upsert_scene(
            track_id=track_id,
            scene_no=shot.shot_no,
            shot_from=shot.shot_no,
            shot_to=shot.shot_no,
            music_mood=mood,
            intensity=intensity,
            ambience_tags_json=json.dumps(tags),
        )



class DemoScriptHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
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

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        return _sha(
            {
                "stage": "demo_script",
                "episode_id": subject.id,
                "title": episode.title,
                "concept": episode.concept,
                "languages_json": episode.languages_json,
                "aspects_json": episode.aspects_json,
                "llm": ctx.resolved_profile.model_key_for("llm_creative"),
                "schema": _SCRIPT_SCHEMA,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        tracks = ctx.repos.content.list_tracks(episode.id)
        if not tracks:
            return StageOutput(
                output={"episode_id": episode.id, "shots_created": 0, "created": 0, "track_ids": []}
            )

        existing = ctx.repos.content.list_shots(episode.id)
        if existing:
            for track in tracks:
                _ensure_scenes(ctx, track.id)
            return StageOutput(
                output={
                    "episode_id": episode.id,
                    "shots_created": len(existing),
                    "created": 0,
                    "reused": True,
                    "track_ids": [t.id for t in tracks],
                }
            )

        model_key = ctx.resolved_profile.model_key_for("llm_creative")
        prompt = (
            f"Episode concept: {episode.concept}\n"
            "Plan exactly 3 shots. Return a JSON object with a shots array, "
            "each element having shot_no (1-based) and prompt (a one-line visual description)."
        )
        result = await ctx.gateway.llm(
            model_key,
            LLMRequest(
                system="You are a concise storyboard director.",
                prompt=prompt,
                schema_name=_SCRIPT_SCHEMA,
                max_tokens=1024,
                temperature=0.0,
            ),
            CallContext(
                episode_id=episode.id, stage_run_id=ctx.stage_run_id, nonce="0", purpose="script"
            ),
        )
        parsed = result.inline_json or {}
        shots = parsed.get("shots") if isinstance(parsed, dict) else None
        if not isinstance(shots, list) or not shots:
            raise PermanentProviderError("script stage: LLM returned no shots")
        created = 0
        for track in tracks:
            for item in shots:
                if not isinstance(item, dict):
                    continue
                shot_no = item.get("shot_no")
                ctx.repos.content.create_shot(
                    track_id=track.id,
                    shot_no=int(shot_no) if shot_no else created + 1,
                    prompt=str(item.get("prompt", "")).strip() or "untitled shot",
                )
                created += 1
        for track in tracks:
            _ensure_scenes(ctx, track.id)
        return StageOutput(
            output={
                "episode_id": episode.id,
                "shots_created": created,
                "created": created,
                "track_ids": [t.id for t in tracks],
            }
        )


# ==================================================================
# demo_voice
# ==================================================================


class DemoVoiceHandler:
    """Materialize `narration_lines` rows for the episode (Phase 2 role).

    Phase 3 note: actual synthesis happens in `s5_tts` (LINE scope, mock TTS
    in sandbox). This stage no longer synthesizes anything; it exists so the
    narration text exists as rows for the voicer — the demo analog of the
    production S3 materialization. Kept as a stage so Phase 2 pipeline tests
    (stage set, staleness graph) remain valid.
    """

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _deliverable_subjects(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        shots = sorted(
            ctx.repos.content.shots_for_track(deliverable.track_id),
            key=lambda s: s.shot_no,
        )
        return _sha(
            {
                "stage": "demo_voice",
                "deliverable_id": subject.id,
                "language": deliverable.language,
                "channel_id": deliverable.channel_id,
                "lines": [
                    {"shot_id": s.id, "shot_no": s.shot_no, "text": narration_text(s)}
                    for s in shots
                ],
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        if deliverable is None:
            raise PermanentProviderError(f"deliverable not found: {subject.id}")
        shots = sorted(
            ctx.repos.content.shots_for_track(deliverable.track_id),
            key=lambda s: s.shot_no,
        )
        written = 0
        for shot in shots:
            existing = [
                line
                for line in ctx.repos.content.narration_lines_for_shot(shot.id)
                if line.deliverable_id == deliverable.id
            ]
            if existing:
                continue
            text = narration_text(shot)
            ctx.repos.content.upsert_narration_line(
                deliverable_id=deliverable.id,
                shot_id=shot.id,
                line_no=shot.shot_no,
                text=text,
                asset_sha256=None,
                duration_ms=None,
            )
            written += 1
        return StageOutput(
            output={
                "deliverable_id": deliverable.id,
                "language": deliverable.language,
                "lines_materialized": written,
                "lines": len(shots),
            }
        )


# ==================================================================
# demo_keyframes
# ==================================================================


class DemoKeyframesHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _shot_subjects(ctx)

    def _attempt(self, ctx: RunContext, shot: ShotRow) -> ShotAttemptRow | None:
        return ctx.repos.content.current_shot_attempt(shot.id)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        shot = ctx.repos.content.get_shot(subject.id)
        assert shot is not None
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        attempt = self._attempt(ctx, shot)
        return _sha(
            {
                "stage": "demo_keyframes",
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no if attempt else 0,
                "nonce": attempt.nonce if attempt else None,
                "prompt": attempt.prompt_used if attempt else shot.prompt,
                "image": ctx.resolved_profile.model_key_for("keyframe"),
                "aspect": track.aspect,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        shot = ctx.repos.content.get_shot(subject.id)
        if shot is None:
            raise PermanentProviderError(f"shot not found: {subject.id}")
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        attempt = self._attempt(ctx, shot)
        if attempt is None:
            attempt = ctx.repos.content.create_shot_attempt(
                shot_id=shot.id, attempt_no=1, nonce=ctx.ids.new(), prompt_used=shot.prompt
            )
        model_key = ctx.resolved_profile.model_key_for("keyframe")
        seed = _seed_from_nonce(attempt.nonce)
        result = await ctx.gateway.image(
            model_key,
            MediaRequest(
                capability="image_reference_generate",
                params={"prompt": attempt.prompt_used},
                input_assets=[],
                aspect=Aspect(track.aspect),
                duration_s=None,
                seed=seed,
            ),
            CallContext(
                episode_id=ctx.episode_id,
                stage_run_id=ctx.stage_run_id,
                nonce="0",
                purpose="keyframe",
            ),
        )
        return StageOutput(
            output={
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no,
                "asset_sha256s": [a.sha256 for a in result.assets],
            }
        )


# ==================================================================
# demo_clips
# ==================================================================


class DemoClipsHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _shot_subjects(ctx)

    def _duration_s(self, ctx: RunContext, track: TrackRow, shot: ShotRow) -> int:
        entry = ctx.resolved_profile.entry_for("video")
        needed_ms = ctx.repos.content.narration_max_duration_ms(track.id, shot.id)
        needed_s = (needed_ms / 1000.0) if needed_ms else 0.0
        if needed_s <= 0:
            supported = sorted(entry.supported_durations_s)
            return supported[0] if supported else 5
        import math

        return _quantize_duration(math.ceil(needed_s), list(entry.supported_durations_s))

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        shot = ctx.repos.content.get_shot(subject.id)
        assert shot is not None
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        attempt = ctx.repos.content.current_shot_attempt(shot.id)
        keyframe = _stage_output(ctx, "demo_keyframes", shot.id) or {}
        return _sha(
            {
                "stage": "demo_clips",
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no if attempt else 0,
                "keyframe_sha256": (keyframe.get("asset_sha256s") or [None])[0],
                "duration_s": self._duration_s(ctx, track, shot),
                "video": ctx.resolved_profile.model_key_for("video"),
                "aspect": track.aspect,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        shot = ctx.repos.content.get_shot(subject.id)
        if shot is None:
            raise PermanentProviderError(f"shot not found: {subject.id}")
        track = ctx.repos.content.get_track(shot.track_id)
        assert track is not None
        attempt = ctx.repos.content.current_shot_attempt(shot.id)
        if attempt is None:
            raise PermanentProviderError(
                f"shot {shot.id} has no attempts; run demo_keyframes first"
            )
        keyframe = _stage_output(ctx, "demo_keyframes", shot.id)
        keyframe_shas = (keyframe or {}).get("asset_sha256s") or []
        if not keyframe_shas:
            raise PermanentProviderError(
                f"shot {shot.id} has no keyframe asset; run demo_keyframes first"
            )
        keyframe_row = ctx.repos.assets.get(keyframe_shas[0])
        if keyframe_row is None:
            raise PermanentProviderError(f"keyframe asset missing for shot {shot.id}")
        aspect = Aspect(track.aspect)
        duration_s = self._duration_s(ctx, track, shot)
        ref = AssetRef(
            sha256=keyframe_row.sha256,
            rel_path=keyframe_row.rel_path,
            kind=keyframe_row.kind,
            mime=keyframe_row.mime,
            size_bytes=keyframe_row.size_bytes,
            duration_ms=keyframe_row.duration_ms,
            width=keyframe_row.width,
            height=keyframe_row.height,
        )
        model_key = ctx.resolved_profile.model_key_for("video")
        seed = _seed_from_nonce(attempt.nonce)
        result = await ctx.gateway.video(
            model_key,
            MediaRequest(
                capability="image_to_video",
                params={"prompt": attempt.prompt_used},
                input_assets=[ref],
                aspect=aspect,
                duration_s=float(duration_s),
                seed=seed,
            ),
            CallContext(
                episode_id=ctx.episode_id,
                stage_run_id=ctx.stage_run_id,
                nonce="0",
                purpose="clip",
            ),
        )
        return StageOutput(
            output={
                "shot_id": shot.id,
                "attempt_no": attempt.attempt_no,
                "duration_s": duration_s,
                "asset_sha256s": [a.sha256 for a in result.assets],
            }
        )


# ==================================================================
# demo_finalize
# ==================================================================


class DemoFinalizeHandler:
    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _deliverable_subjects(ctx)

    def _clip_map(self, ctx: RunContext, track_id: str) -> dict[str, dict[str, Any]]:
        clips: dict[str, dict[str, Any]] = {}
        for row in ctx.repos.pipeline.list_stage_runs(ctx.run_id, stage="demo_clips"):
            if row.status != "succeeded" or not row.output_json:
                continue
            try:
                data = json.loads(row.output_json)
            except json.JSONDecodeError:
                continue
            shot_id = data.get("shot_id")
            if not shot_id:
                continue
            clips[str(shot_id)] = data
        return clips

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        assert deliverable is not None
        shots = sorted(
            ctx.repos.content.shots_for_track(deliverable.track_id),
            key=lambda s: s.shot_no,
        )
        clip_map = self._clip_map(ctx, deliverable.track_id)
        return _sha(
            {
                "stage": "demo_finalize",
                "deliverable_id": subject.id,
                "language": deliverable.language,
                "channel_id": deliverable.channel_id,
                "clips": [
                    {
                        "shot_id": s.id,
                        "sha256": (clip_map.get(s.id) or {}).get("asset_sha256s", [None])[0],
                    }
                    for s in shots
                ],
                "manifest_version": _MANIFEST_VERSION,
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        deliverable = ctx.repos.content.get_deliverable(subject.id)
        if deliverable is None:
            raise PermanentProviderError(f"deliverable not found: {subject.id}")
        episode = _episode(ctx)
        assert episode is not None
        track = ctx.repos.content.get_track(deliverable.track_id)
        assert track is not None
        shots = sorted(
            ctx.repos.content.shots_for_track(track.id),
            key=lambda s: s.shot_no,
        )
        clip_map = self._clip_map(ctx, track.id)
        missing = [s.shot_no for s in shots if s.id not in clip_map]
        if missing:
            raise PermanentProviderError(
                f"clips missing for shot(s) {missing}; run demo_clips first"
            )
        narration = ctx.repos.content.narration_lines(deliverable.id)
        manifest = {
            "manifest_version": _MANIFEST_VERSION,
            "series_id": episode.series_id,
            "episode_id": episode.id,
            "track_id": track.id,
            "aspect": track.aspect,
            "deliverable_id": deliverable.id,
            "language": deliverable.language,
            "channel_id": deliverable.channel_id,
            "clips": [
                {
                    "shot_no": s.shot_no,
                    "shot_id": s.id,
                    "duration_s": clip_map[s.id].get("duration_s"),
                    "asset_sha256": clip_map[s.id]["asset_sha256s"][0],
                }
                for s in shots
            ],
            "narration_lines": [
                {
                    "line_no": line.line_no,
                    "shot_id": line.shot_id,
                    "text": line.text,
                    "asset_sha256": line.asset_sha256,
                    "duration_ms": line.duration_ms,
                }
                for line in sorted(narration, key=lambda entry: entry.line_no)
            ],
        }
        from studio.core.cas import ContentStore

        cas = ContentStore(Path(ctx.config.settings.paths.cas), ctx.db, ctx.clock)
        with tempfile.NamedTemporaryFile(
            prefix="studio-manifest-", suffix=".json", delete=False
        ) as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            tmp = Path(handle.name)
        try:
            ref = cas.put_file(tmp, kind="manifest", ext="json", retention="export", move=True)
        finally:
            await asyncio.to_thread(tmp.unlink, missing_ok=True)
        return StageOutput(
            output={
                "deliverable_id": deliverable.id,
                "language": deliverable.language,
                "manifest_sha256": ref.sha256,
                "asset_sha256s": [ref.sha256],
            }
        )


# ==================================================================
# spec table
# ==================================================================


def demo_specs() -> list[StageSpec]:
    return [
        StageSpec(
            name="demo_script",
            scope=Scope.EPISODE,
            depends_on=(),
            handler=DemoScriptHandler(),
            gate_after=GateKind.G1_SCRIPT,
        ),
        StageSpec(
            name="demo_voice",
            scope=Scope.DELIVERABLE,
            depends_on=("demo_script",),
            handler=DemoVoiceHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="s5a_tts_preflight",
            scope=Scope.EPISODE,
            depends_on=("demo_script",),
            handler=TtsPreflightHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="s5_tts",
            scope=Scope.LINE,
            depends_on=("s5a_tts_preflight", "demo_voice"),
            handler=TtsLineHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="s6_timing",
            scope=Scope.TRACK,
            depends_on=("s5_tts",),
            handler=TimingReconcilerHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="s7_audio_plan",
            scope=Scope.DELIVERABLE,
            depends_on=("s6_timing",),
            handler=AudioPlanHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="s7b_audio_mix",
            scope=Scope.DELIVERABLE,
            depends_on=("s7_audio_plan", "s5_tts"),
            handler=AudioMixHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="demo_keyframes",
            scope=Scope.SHOT,
            depends_on=("demo_script",),
            handler=DemoKeyframesHandler(),
            gate_after=GateKind.G2_KEYFRAMES,
        ),
        StageSpec(
            name="demo_clips",
            scope=Scope.SHOT,
            depends_on=("demo_keyframes", "s6_timing"),
            handler=DemoClipsHandler(),
            gate_after=None,
        ),
        StageSpec(
            name="demo_finalize",
            scope=Scope.DELIVERABLE,
            depends_on=("demo_clips",),
            handler=DemoFinalizeHandler(),
            gate_after=GateKind.G3_FINAL_CUT,
        ),
    ]
