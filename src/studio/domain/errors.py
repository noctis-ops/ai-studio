"""Typed error hierarchy.

Contract (enforced by the gateway and the worker):
- `BudgetExceeded`/`KillSwitchTripped` mean the provider was never invoked.
- `TransientProviderError` is the only class retried by the retry helper.
- `SimulatedCrash` (BaseException, core/faults.py) must propagate untouched.
"""

from __future__ import annotations

from .enums import ErrorClass


class StudioError(Exception):
    """Base class for every error raised deliberately by the studio."""


class ConfigError(StudioError):
    """Configuration file invalid, unreadable, or referencing missing values."""


class RegistryError(ConfigError):
    """Model registry / profile resolution failed."""


class CommercialUseNotVerified(StudioError):
    """A model without a verified commercial licence was used for a final profile."""


class ProviderError(StudioError):
    error_class: ErrorClass = ErrorClass.PERMANENT


class TransientProviderError(ProviderError):
    """429/5xx/timeout-style failures that may succeed on retry."""

    error_class = ErrorClass.TRANSIENT


class PolicyRefusalError(ProviderError):
    """The provider refused the content on policy grounds."""

    error_class = ErrorClass.POLICY


class PermanentProviderError(ProviderError):
    """A failure that retrying will not fix."""

    error_class = ErrorClass.PERMANENT


class ZeroByteAssetError(PermanentProviderError):
    """A paid asset came back empty; the provider likely billed anyway."""

    error_class = ErrorClass.PERMANENT


class ProviderQuotaExceeded(PermanentProviderError):
    """Provider-side quota exhausted (e.g. ElevenLabs `quota_exceeded`).

    Treated like a budget halt by the engine: the run parks at halted_budget,
    the stage_run goes back to pending, and an audit event with reason
    `provider_quota` is written — never a retry loop.
    """

    error_class = ErrorClass.PERMANENT


class BudgetExceeded(StudioError):
    def __init__(
        self,
        message: str,
        *,
        scope: str,
        scope_ref: str,
        cap_musd: int | None,
        held_musd: int,
        requested_musd: int,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.scope_ref = scope_ref
        self.cap_musd = cap_musd
        self.held_musd = held_musd
        self.requested_musd = requested_musd


class KillSwitchTripped(BudgetExceeded):
    """A scope's kill switch is set; no paid call may proceed until reset."""


class GatePolicyError(StudioError):
    """A gate operation violates the (profile, run) policy."""


class ProviderNotImplementedError(StudioError):
    """A real provider adapter is requested in a phase where it does not exist yet."""


class AssetNotFoundError(StudioError):
    """CAS lookup failed, or the asset was purged/never stored."""


class MediaToolError(StudioError):
    """ffmpeg/ffprobe exited non-zero or is missing."""


class PreflightError(StudioError):
    """S5a preflight failed before any spend (missing voice, over-budget worst case, ...)."""


class TtsQualityError(PermanentProviderError):
    """TTS QC rejected a line after `auto_retries` paid regenerations."""

    error_class = ErrorClass.PERMANENT


class TimelineOverflowError(StudioError):
    """Narration does not fit inside its shot (exact numbers in the message)."""


class TimingOverflowError(TimelineOverflowError):
    """S6 reconciler found shots whose narration exceeds clip capacity.

    Every overflowing shot is listed in the message with exact numbers; the
    narration text is never modified.
    """


class LibraryEmptyError(StudioError):
    """The plan needs a library item but the (active) library has none."""


class MasteringError(MediaToolError):
    """Loudness/true-peak verification after mastering failed."""


class AudioQcError(StudioError):
    """An audio QC check failed."""


class AudioPlanError(StudioError):
    """The audio plan (S7) could not be built deterministically."""


# ---------------------------------------------------------------------------
# Phase 4 (media) errors
# ---------------------------------------------------------------------------


class GateNotCleared(StudioError):
    """H0: a paid stage tried to run with an uncleared ancestor gate."""


class MediaConfigError(ConfigError):
    """Invalid/unverified media model, aspect, duration or chain config."""


class ContentPolicyError(PolicyRefusalError):
    """fal returned a policy refusal / NSFW flag.

    ``billed=False`` means the provider did not charge (release the
    reservation); ``billed=True`` means the job was accepted and charged
    (settle in full). Never store an asset for a billed refusal.
    """

    error_class = ErrorClass.POLICY

    def __init__(self, message: str, *, billed: bool) -> None:
        super().__init__(message)
        self.billed = billed


class JobPendingTimeoutError(TransientProviderError):
    """Poll exhaustion for a remote job that stays alive.

    The remote job may still be running and will bill; the call row must
    stay ``submitted`` so a later attempt adopts it (zero re-submits).
    """


class MediaJobFailedError(PermanentProviderError):
    """A fal job reached a failed state after acceptance.

    Settlement follows ``media.failed_job_billing`` (charge | free); the
    default is the conservative ``charge``.
    """


class MediaCircuitOpen(StudioError):
    """Too many consecutive shot failures; stop the stage before burning money."""


class MediaAttemptExhausted(StudioError):
    """``max_paid_attempts_per_shot`` reached without an accepted attempt."""


class VisualQcError(StudioError):
    """A non-retryable visual QC failure (aspect, resolution, duration ...)."""


class FailedPolicy(StudioError):
    """Sanitizer exhausted ``max_levels``; the shot parks for a human."""


# ---- Phase 5 ---------------------------------------------------------------


class GeometryError(StudioError):
    """A clip's geometry cannot be conformed to the destination tolerance."""


class InsufficientDisk(StudioError):
    """Free disk is below ``assemble.min_free_disk_factor`` x the estimated need."""


class EncoderUnavailable(StudioError):
    """The requested encoder is missing or failed its probe (fail closed)."""


class EpisodeClosed(StudioError):
    """The episode is published/archived: paid stages are refused (§S15)."""


class AssetPurged(StudioError):
    """A cache hit points at a purged asset; fail closed, call no provider (§H5.6)."""


class ExportNotReady(StudioError):
    """An export was requested for a deliverable with no approved render."""
