"""Media pipeline stages (Phase 4 sections H-L) — compatibility shim.

The Phase 4 implementation lived in this single 2 284-line module. Phase 5
H5.8 split it, as a pure move, into cohesive modules:

* :mod:`studio.media.preflight` — ``s4p_media_preflight`` / ``s10p_clip_preflight``
* :mod:`studio.media.sheets`    — ``s8a_character_sheets`` (Gate G0) + cost helpers
* :mod:`studio.media.keyframes` — ``s8_keyframes`` (Gate G2 is created on this stage)
* :mod:`studio.media.clips`     — ``s10_clips``
* :mod:`studio.media.retry`     — shared attempt/retry policy (crash-safe nonces)
* :mod:`studio.media.common`    — shared lookups, request builders, reference assets

DAG (wired by `specs.py` / `demo_specs()`):

    s4p_media_preflight (EPISODE, free, fail closed)
        → s5a_tts_preflight → … → s7b_audio_mix
        → s8a_character_sheets (subjects: missing (character, role); Gate G0)
        → s8_keyframes       (SHOT, paid)
        → s9_animatic        (DELIVERABLE, free; Gate G2)
        → s10p_clip_preflight (EPISODE, free, after G2)
        → s10_clips          (SHOT, paid; locked duration; conform)

Shared design decisions (recorded in PHASE4_REPORT.md and PHASE5_REPORT.md):

- **Crash resume repays nothing.** Shot attempts are created before the first
  paid call; the attempt's nonce is persisted in one statement with the
  prompt spec before every submit. After a crash the handler rebuilds the
  cache key from the persisted attempt, so the gateway either resumes the
  in-flight call (adoption) or hits the provider cache. A regeneration
  changes the nonce (``base.qN``); a resume keeps it.
- **Sanitizer state survives crashes.** ``prompt_spec_json`` and
  ``refusal_log_json`` are written before each submit, so a crash after a
  refusal resumes at the sanitized prompt; the sanitizer LLM rewrite itself
  is memoized by the gateway (nonce ``san{level}``), so no extra paid call
  happens either way. Sanitizer rewrites count toward
  ``media.max_paid_attempts_per_shot``.
- **Fail-fast, not a circuit breaker.** A non-retryable QC failure or an
  exhausted attempt budget fails the stage with :class:`MediaCircuitOpen`
  (``failed_policy`` after a sanitizer cap) plus an audit row, so a broken
  endpoint cannot burn money one shot at a time. The Phase 4
  ``media.circuit_breaker_consecutive_failures`` knob was parsed but never
  read; Phase 5 H5.1 removed it.
- **No visual LLM anywhere.** All QC is ffmpeg/ffprobe (``qc/visual.py``).
- Sheets are keyed on the *pinned bible character name* (the same identity
  S3's continuity gate validates and shot plans reference), not on the
  ``characters`` table — production episodes have bible characters but no
  ``characters`` rows.
"""

from __future__ import annotations

from studio.domain.errors import (
    ContentPolicyError,
    FailedPolicy,
    MediaAttemptExhausted,
    MediaCircuitOpen,
    MediaConfigError,
    MediaJobFailedError,
    PreflightError,
    VisualQcError,
)
from studio.media.clips import ClipGenHandler
from studio.media.common import (
    _approved_sheet_assets,
    _character_visual_spec,
    _episode,
    _image_request,
    _json,
    _model_entry,
    _pinned_bible,
    _ref_assets_for_shot,
    _sha,
    _shot_camera,
    _shot_characters,
    _shot_plan,
    _shot_subjects,
    _stage_output,
    _track_shots,
    _video_request,
)
from studio.media.keyframes import KeyframeHandler
from studio.media.preflight import ClipPreflightHandler, MediaPreflightHandler
from studio.media.retry import (
    _ATTEMPT_COLUMNS,
    _NONCE_RETRY_MARKER,
    _attempt_persist,
    _base_nonce,
    _create_attempt,
    _ensure_attempt,
    _generate_with_retries,
    _GenerationOutcome,
    _media_failure_audit,
    _PreSubmitState,
    _prompt_spec_payload,
    _qc_status,
    _resume_state,
)
from studio.media.sheets import (
    CharacterSheetHandler,
    _estimate_clip_cost,
    _estimate_image_cost,
    _pixel_dims_for,
    _sheet_precheck_subjects,
    _sheet_spec_hash,
)
from studio.qc.visual import QcCheck, qc_image, qc_video

__all__ = [
    "_ATTEMPT_COLUMNS",
    "_NONCE_RETRY_MARKER",
    "CharacterSheetHandler",
    "ClipGenHandler",
    "ClipPreflightHandler",
    "ContentPolicyError",
    "FailedPolicy",
    "KeyframeHandler",
    "MediaAttemptExhausted",
    "MediaCircuitOpen",
    "MediaConfigError",
    "MediaJobFailedError",
    "MediaPreflightHandler",
    "PreflightError",
    "QcCheck",
    "VisualQcError",
    "_GenerationOutcome",
    "_PreSubmitState",
    "_approved_sheet_assets",
    "_attempt_persist",
    "_base_nonce",
    "_character_visual_spec",
    "_create_attempt",
    "_ensure_attempt",
    "_episode",
    "_estimate_clip_cost",
    "_estimate_image_cost",
    "_generate_with_retries",
    "_image_request",
    "_json",
    "_media_failure_audit",
    "_model_entry",
    "_pinned_bible",
    "_pixel_dims_for",
    "_prompt_spec_payload",
    "_qc_status",
    "_ref_assets_for_shot",
    "_resume_state",
    "_sha",
    "_sheet_precheck_subjects",
    "_sheet_spec_hash",
    "_shot_camera",
    "_shot_characters",
    "_shot_plan",
    "_shot_subjects",
    "_stage_output",
    "_track_shots",
    "_video_request",
    "qc_image",
    "qc_video",
]
