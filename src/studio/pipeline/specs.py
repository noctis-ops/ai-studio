"""Phase 4 production DAG composition (H1 / §M).

`text_specs()` is deliberately left frozen: Phase 2 E2E tests build that DAG
with an LLM-subset engine and expect it to complete at G3. This module appends
the audio stages and, after them, the media stages (s4p → s5a → ... → s10).
"""

from __future__ import annotations

from studio.domain.enums import GateKind, Scope
from studio.media.stages import (
    CharacterSheetHandler,
    ClipGenHandler,
    ClipPreflightHandler,
    KeyframeHandler,
    MediaPreflightHandler,
)
from studio.pipeline.stage import StageSpec
from studio.pipeline.stages_audio import (
    AudioMixHandler,
    AudioPlanHandler,
    TimingReconcilerHandler,
    TtsLineHandler,
    TtsPreflightHandler,
)
from studio.pipeline.stages_render import (
    AnimaticHandler,
    AssembleHandler,
    RenderQcHandler,
)
from studio.pipeline.stages_text import text_specs

__all__ = ["audio_specs", "media_specs", "production_specs"]


def audio_specs() -> list[StageSpec]:
    """S5a → S5 → S6 → S7 → S7b, appended after `metadata_safety`."""
    return [
        StageSpec(
            name="s5a_tts_preflight",
            scope=Scope.EPISODE,
            depends_on=("s4p_media_preflight",),
            handler=TtsPreflightHandler(),
            gate_after=None,
            spends_money=False,
        ),
        StageSpec(
            name="s5_tts",
            scope=Scope.LINE,
            depends_on=("s5a_tts_preflight",),
            handler=TtsLineHandler(),
            gate_after=None,
            spends_money=True,
        ),
        StageSpec(
            name="s6_timing",
            scope=Scope.TRACK,
            depends_on=("s5_tts",),
            handler=TimingReconcilerHandler(),
            gate_after=None,
            spends_money=False,
        ),
        StageSpec(
            name="s7_audio_plan",
            scope=Scope.DELIVERABLE,
            depends_on=("s6_timing",),
            handler=AudioPlanHandler(),
            gate_after=None,
            spends_money=False,
        ),
        StageSpec(
            name="s7b_audio_mix",
            scope=Scope.DELIVERABLE,
            depends_on=("s7_audio_plan", "s5_tts"),
            handler=AudioMixHandler(),
            gate_after=None,
            spends_money=False,
        ),
    ]


def media_specs(*, after: tuple[str, ...] = ("s7b_audio_mix",)) -> list[StageSpec]:
    """S8a (G0) → S8 → S9 (G2) → S10p → S10 → S11 → S12 (G3), after audio.

    Phase 5 moved G2 from S8 to S9: the human reviews the *animatic* (real
    stills at real timing over the real master), and rejecting there re-runs
    only the named shots' keyframes (``gate_reject_stage``).
    """
    return [
        StageSpec(
            name="s8a_character_sheets",
            scope=Scope.SERIES,
            depends_on=after,
            handler=CharacterSheetHandler(),
            gate_after=GateKind.G0_CHARACTERS,
            spends_money=True,
        ),
        StageSpec(
            name="s8_keyframes",
            scope=Scope.SHOT,
            depends_on=("s8a_character_sheets",),
            handler=KeyframeHandler(),
            gate_after=None,
            spends_money=True,
        ),
        StageSpec(
            name="s9_animatic",
            scope=Scope.DELIVERABLE,
            depends_on=("s8_keyframes", "s7b_audio_mix"),
            handler=AnimaticHandler(),
            gate_after=GateKind.G2_KEYFRAMES,
            spends_money=False,
            gate_reject_stage="s8_keyframes",
        ),
        StageSpec(
            name="s10p_clip_preflight",
            scope=Scope.EPISODE,
            depends_on=("s9_animatic",),
            handler=ClipPreflightHandler(),
            gate_after=None,
            spends_money=False,
        ),
        StageSpec(
            name="s10_clips",
            scope=Scope.SHOT,
            depends_on=("s10p_clip_preflight",),
            handler=ClipGenHandler(),
            gate_after=None,
            spends_money=True,
        ),
        StageSpec(
            name="s11_assemble",
            scope=Scope.DELIVERABLE,
            depends_on=("s10_clips", "s7b_audio_mix"),
            handler=AssembleHandler(),
            gate_after=None,
            spends_money=False,
        ),
        # Gate G3 lives on the render, never on text: rejecting it re-runs the
        # free assembly (a new render sha) rather than any paid stage.
        StageSpec(
            name="s12_render_qc",
            scope=Scope.DELIVERABLE,
            depends_on=("s11_assemble",),
            handler=RenderQcHandler(),
            gate_after=GateKind.G3_FINAL_CUT,
            gate_reject_stage="s11_assemble",
            spends_money=False,
        ),
    ]


def preflight_specs(*, after: tuple[str, ...] = ("metadata_safety",)) -> list[StageSpec]:
    """s4p_media_preflight — free, fail-closed, before any media/audio spend."""
    return [
        StageSpec(
            name="s4p_media_preflight",
            scope=Scope.EPISODE,
            depends_on=after,
            handler=MediaPreflightHandler(),
            gate_after=None,
            spends_money=False,
        )
    ]


def production_specs() -> list[StageSpec]:
    """Full production DAG.

    Appended after the terminal text stage(s), even though the
    metadata_safety G3 round currently sits there (§M / R4):
    s4p → s5a → s5 → s6 → s7 → s7b → s8a(G0) → s8 → s9(G2) → s10p → s10 → s11.
    """
    return [*text_specs(), *preflight_specs(), *audio_specs(), *media_specs()]
