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
