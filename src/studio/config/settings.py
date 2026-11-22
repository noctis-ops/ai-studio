"""Settings model + `${ENV}` interpolation.

The config files are the only place tuning knobs live; every tunable has a
typed field here. Unknown keys are rejected (`extra="forbid"`).
"""

from __future__ import annotations

import os
import re
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from studio.domain.enums import Aspect, Language, Profile
from studio.domain.errors import ConfigError

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_env(text: str, *, source: str = "<config>") -> str:
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"{source}: environment variable {name} is not set (referenced as ${{{name}}})"
            )
        return os.environ[name]

    return _ENV_REF.sub(_sub, text)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathSettings(_Strict):
    home: str
    db: str
    cas: str
    exports: str
    projects: str


class WorkerSettings(_Strict):
    max_concurrency: int = Field(default=1, ge=1)
    per_provider_concurrency: int = Field(default=1, ge=1)
    heartbeat_s: float = Field(default=20, gt=0)
    stale_after_s: float = Field(default=120, gt=0)
    idle_poll_s: float = Field(default=0.5, gt=0)
    job_retry_backoff_s: float = Field(default=30, gt=0)


class RetrySettings(_Strict):
    transient_max: int = Field(default=5, ge=1)
    backoff_base_s: float = Field(default=2, gt=0)
    backoff_cap_s: float = Field(default=60, gt=0)


class GatesSettings(_Strict):
    allow_auto_approve_in: list[Profile] = [Profile.SANDBOX, Profile.DRAFT]


class BudgetSettings(_Strict):
    default_episode_cap_usd: Decimal
    monthly_cap_usd: Decimal
    global_cap_usd: Decimal | None = None
    warn_pct: int = Field(default=80, ge=0, le=100)


class PricingSettings(_Strict):
    llm_tokens_per_char_worst_case: float = Field(default=0.5, gt=0)


class MockSettings(_Strict):
    tts_chars_per_second: int = Field(default=15, gt=0)
    video_width_16x9: int = Field(default=480, gt=0)
    video_height_16x9: int = Field(default=270, gt=0)
    fps: int = Field(default=24, gt=0)
    poll_ticks: int = Field(default=2, ge=0)


class RetentionSettings(_Strict):
    keep_raw_clips_days: int = Field(default=30, ge=0)
    keep_narration_audio_days: int = Field(default=30, ge=0)


class DoctorSettings(_Strict):
    probe_timeout_s: float = Field(default=30, gt=0)


class RenderSettings(_Strict):
    fps: int = Field(default=24, ge=1)


class TtsQcSettings(_Strict):
    min_duration_ratio: float = Field(default=0.4, gt=0)
    max_duration_ratio: float = Field(default=2.5, gt=0)
    auto_retries: int = Field(default=1, ge=0)


class TtsSettings(_Strict):
    request_timeout_s: float = Field(default=120, gt=0)
    use_context: bool = True
    context_max_chars: int = Field(default=300, ge=0)
    keep_diacritics: bool = True
    qc: TtsQcSettings = TtsQcSettings()


class TimingSettings(_Strict):
    max_stretch: float = Field(default=1.15, ge=1.0)
    max_hold_ms: int = Field(default=500, ge=0)
    j_cut_ms: int = Field(default=0)
    prefer_stretch_to_save_cost: bool = True
    estimate_drift_warn_pct: float = Field(default=25, ge=0)
    # The prompt references lead_in_ms, line_gap_ms and tail_ms from `timing`
    # without giving values; defaults chosen by the implementer (recorded).
    lead_in_ms: int = Field(default=150, ge=0)
    line_gap_ms: int = Field(default=120, ge=0)
    tail_ms: int = Field(default=200, ge=0)
    # S6 QC needs words-per-second per language; calibrate with `studio audio
    # calibrate` and adjust (recorded assumption).
    words_per_second: dict[str, float] = {"ar": 2.5, "en": 2.5}


class AudioLevelsSettings(_Strict):
    speech_line_lufs: float = -20.0
    max_line_gain_db: float = Field(default=9.0, gt=0)
    music_lufs: float = -24.0
    ambience_lufs: float = -34.0
    sfx_lufs: float = -22.0
    intensity_offset_db: dict[int, float] = {1: -8.0, 2: -5.0, 3: -3.0, 4: -1.0, 5: 0.0}
    intensity_ramp_ms: int = Field(default=800, ge=0)


class SpeechTrimSettings(_Strict):
    threshold_db: float = -50.0
    keep_ms: int = Field(default=60, ge=0)


class MusicSettings(_Strict):
    fade_in_ms: int = Field(default=1000, ge=0)
    final_fade_ms: int = Field(default=2000, ge=0)
    crossfade_ms: int = Field(default=1500, ge=0)
    recent_penalty_episodes: int = Field(default=3, ge=0)
    recent_penalty: float = Field(default=1.0, ge=0)
    loop_penalty: float = Field(default=0.5, ge=0)
    segment_intensity_break: int = Field(default=2, ge=1)


class SidechainSettings(_Strict):
    threshold: float = Field(default=0.05, gt=0)
    ratio: float = Field(default=8.0, gt=0)
    attack_ms: float = Field(default=20.0, gt=0)
    release_ms: float = Field(default=350.0, gt=0)
    makeup: float = Field(default=1.0, gt=0)


class DuckSettings(_Strict):
    mode: str = "envelope"
    depth_db: float = Field(default=10.0, gt=0)
    pre_ms: float = Field(default=150.0, ge=0)
    attack_ms: float = Field(default=120.0, ge=0)
    release_ms: float = Field(default=450.0, ge=0)
    merge_gap_ms: float = Field(default=700.0, ge=0)
    ambience_fraction: float = Field(default=0.5, ge=0, le=1)
    sidechain: SidechainSettings = SidechainSettings()


class SfxSettings(_Strict):
    transition_enabled: bool = True
    transition_tag: str = "transition"
    transition_offset_ms: int = -150


class MasterSettings(_Strict):
    target_lufs: float = -14.0
    true_peak_db: float = -1.5
    lra: float = Field(default=11.0, gt=0)
    lufs_tolerance: float = Field(default=1.0, gt=0)
    tp_ceiling_db: float = -1.0
    clip_peak_db: float = -0.1


class AudioQcSettings(_Strict):
    min_duck_db: float = Field(default=8.0, gt=0)
    min_speech_to_music_db: float = Field(default=10.0, gt=0)
    silent_line_rms_db: float = -45.0
    window_ms: int = Field(default=100, gt=0)
    exclude_onset_ms: int = Field(default=150, ge=0)


class AudioSettings(_Strict):
    sample_rate: int = Field(default=48000, ge=8000)
    library_root: str = "library"
    scan_extensions: list[str] = [".wav", ".mp3", ".flac", ".ogg"]
    levels: AudioLevelsSettings = AudioLevelsSettings()
    speech_trim: SpeechTrimSettings = SpeechTrimSettings()
    music: MusicSettings = MusicSettings()
    sfx: SfxSettings = SfxSettings()
    duck: DuckSettings = DuckSettings()
    master: MasterSettings = MasterSettings()
    qc: AudioQcSettings = AudioQcSettings()


class TextPipelineSettings(_Strict):
    context_token_budget: int = Field(default=6000, ge=500, le=100000)
    tashkeel_retry: int = Field(default=1, ge=0, le=2)
    s4_after_g1: bool = True


# ---------------------------------------------------------------------------
# Phase 4 (media) settings — all QC/poll thresholds are config, not constants
# ---------------------------------------------------------------------------


class FalSettings(_Strict):
    queue_base_url: str = "https://queue.fal.run"
    key_env: str = "FAL_KEY"
    request_timeout_s: float = Field(default=60.0, gt=0)
    download_timeout_s: float = Field(default=120.0, gt=0)
    download_retries: int = Field(default=3, ge=0)


class MediaPollSettings(_Strict):
    initial_s: float = Field(default=3.0, gt=0)
    max_s: float = Field(default=15.0, gt=0)
    backoff: float = Field(default=1.5, gt=1.0)
    max_wait_s: float = Field(default=900.0, gt=0)


class MediaImageQcSettings(_Strict):
    min_short_side_px: int = Field(default=512, ge=1)
    aspect_tolerance_pct: float = Field(default=2.0, gt=0)
    min_bytes: int = Field(default=5000, ge=0)
    min_luma_mean: float = Field(default=8.0, ge=0)
    min_luma_stddev: float = Field(default=3.0, ge=0)
    auto_retries: int = Field(default=2, ge=0)


class MediaVideoQcSettings(_Strict):
    aspect_tolerance_pct: float = Field(default=2.0, gt=0)
    min_bytes: int = Field(default=20000, ge=0)
    black_fail_ratio: float = Field(default=0.15, gt=0, le=1)
    black_pic_th: float = Field(default=0.98, gt=0, le=1)
    black_min_d_s: float = Field(default=0.1, gt=0)
    freeze_warn_ratio: float = Field(default=0.5, ge=0, le=1)
    freeze_fail_ratio: float = Field(default=0.85, ge=0, le=1)
    first_frame_black_luma: float = Field(default=8.0, ge=0)
    first_frame_ssim_warn: float = Field(default=0.6, ge=0, le=1)
    first_frame_ssim_fail: float = Field(default=0.0, ge=0, le=1)
    auto_retries: int = Field(default=1, ge=0)


class MediaClipSettings(_Strict):
    duration_tolerance_ms: int = Field(default=500, ge=0)
    conform_crf: int = Field(default=14, ge=0, le=51)
    strip_audio: bool = True


class MediaQcSettings(_Strict):
    image: MediaImageQcSettings = MediaImageQcSettings()
    video: MediaVideoQcSettings = MediaVideoQcSettings()


class MediaSettings(_Strict):
    poll: MediaPollSettings = MediaPollSettings()
    max_paid_attempts_per_shot: int = Field(default=4, ge=1)
    failed_job_billing: Literal["charge", "free"] = "charge"
    assumed_retry_rate: dict[str, float] = {"image": 0.25, "video": 0.30}
    ref_max_side_px: int = Field(default=1024, ge=16)
    ref_jpeg_quality: int = Field(default=90, ge=1, le=100)
    max_inline_bytes: int = Field(default=8_000_000, ge=1)
    refs_overflow: Literal["primary_only", "fail"] = "primary_only"
    chain_mode: str = "off"
    clip: MediaClipSettings = MediaClipSettings()
    qc: MediaQcSettings = MediaQcSettings()


class CharacterSheetsSettings(_Strict):
    roles: list[str] = ["ref_full"]
    aspect: str = "9:16"


class SanitizerSettings(_Strict):
    max_levels: int = Field(default=2, ge=0)
    model_key: str = "llm.light"
    max_scene_chars: int = Field(default=600, ge=1)


# ---------------------------------------------------------------------------
# H5.1: deprecated settings
#
# A knob that no code reads is a lie in the config file. Removed keys stay
# loadable once (an operator's existing settings.yaml must not break), but they
# are dropped before validation, reported as a DeprecationWarning, surfaced as
# a `studio doctor` INFO row and written to the audit log by the CLI.
# ---------------------------------------------------------------------------

_DEPRECATED_SETTINGS: dict[str, str] = {
    "media.circuit_breaker_consecutive_failures": "ignored: media stage is fail-fast",
}


def strip_deprecated_settings(data: dict[str, Any], *, source: str = "<config>") -> list[str]:
    """Remove deprecated keys from a raw settings mapping; return the notes."""
    notes: list[str] = []
    for dotted, message in _DEPRECATED_SETTINGS.items():
        parts = dotted.split(".")
        node: Any = data
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and parts[-1] in node:
            node.pop(parts[-1])
            note = f"{source}: {dotted} is deprecated and {message}"
            warnings.warn(note, DeprecationWarning, stacklevel=3)
            notes.append(note)
    return notes


def deprecated_settings_in_file(path: Path) -> list[str]:
    """Doctor helper: which deprecated keys does this file still contain?"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    found: list[str] = []
    for dotted in _DEPRECATED_SETTINGS:
        parts = dotted.split(".")
        node: Any = data
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and parts[-1] in node:
            found.append(dotted)
    return found


def _dedupe(values: list[str], *, what: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        if raw in seen:
            raise ConfigError(f"{what} contains duplicates: {raw!r}")
        seen.add(raw)
        out.append(raw)
    return out


class Settings(_Strict):
    paths: PathSettings
    languages: list[Language]
    aspects: list[Aspect]
    default_profile: Profile = Profile.DRAFT
    worker: WorkerSettings = WorkerSettings()
    retry: RetrySettings = RetrySettings()
    gates: GatesSettings = GatesSettings()
    budgets: BudgetSettings
    pricing: PricingSettings = PricingSettings()
    mock: MockSettings = MockSettings()
    retention: RetentionSettings = RetentionSettings()
    text_pipeline: TextPipelineSettings = TextPipelineSettings()
    doctor: DoctorSettings = DoctorSettings()
    render: RenderSettings = RenderSettings()
    tts: TtsSettings = TtsSettings()
    timing: TimingSettings = TimingSettings()
    audio: AudioSettings = AudioSettings()
    fal: FalSettings = FalSettings()
    media: MediaSettings = MediaSettings()
    character_sheets: CharacterSheetsSettings = CharacterSheetsSettings()
    sanitizer: SanitizerSettings = SanitizerSettings()

    @model_validator(mode="after")
    def _check_cross_field(self) -> Settings:
        fps = self.render.fps
        sample_rate = self.audio.sample_rate
        if sample_rate % fps != 0:
            raise ConfigError(
                f"render.fps ({fps}) must divide audio.sample_rate ({sample_rate}) exactly"
            )
        if self.timing.j_cut_ms != 0:
            raise ConfigError("timing.j_cut_ms is not supported before Phase 5; set it to 0")
        if self.audio.duck.attack_ms > self.audio.duck.pre_ms:
            raise ConfigError(
                f"audio.duck.attack_ms ({self.audio.duck.attack_ms}) must be <= "
                f"audio.duck.pre_ms ({self.audio.duck.pre_ms})"
            )
        if self.audio.duck.mode not in ("envelope", "sidechain"):
            raise ConfigError(
                f"audio.duck.mode must be 'envelope' or 'sidechain', got {self.audio.duck.mode!r}"
            )
        if self.tts.qc.max_duration_ratio < self.tts.qc.min_duration_ratio:
            raise ConfigError(
                "tts.qc.max_duration_ratio must be >= tts.qc.min_duration_ratio"
            )
        allowed = {member.value for member in Language}
        for key in self.timing.words_per_second:
            if key not in allowed:
                raise ConfigError(
                    f"timing.words_per_second has unsupported language {key!r} "
                    f"(allowed: {sorted(allowed)})"
                )
        for key, value in self.timing.words_per_second.items():
            if value <= 0:
                raise ConfigError(f"timing.words_per_second[{key!r}] must be > 0")
        for level in self.audio.levels.intensity_offset_db:
            if level not in (1, 2, 3, 4, 5):
                raise ConfigError(
                    f"audio.levels.intensity_offset_db has intensity {level}; expected 1..5"
                )
        # ---- Phase 4 config validation (§E2) -------------------------------
        if self.media.chain_mode == "last_frame":
            raise ConfigError("last_frame chaining not supported before Phase 5")
        if self.media.chain_mode != "off":
            raise ConfigError(
                f"media.chain_mode must be 'off', got {self.media.chain_mode!r}"
            )
        for name, ratio in self.media.assumed_retry_rate.items():
            if not 0 < ratio <= 1:
                raise ConfigError(
                    f"media.assumed_retry_rate[{name!r}] must be in (0, 1], got {ratio}"
                )
        if not 0 < self.media.qc.video.black_fail_ratio <= 1:
            raise ConfigError("media.qc.video.black_fail_ratio must be in (0, 1]")
        if not self.character_sheets.roles:
            raise ConfigError("character_sheets.roles must not be empty")
        if self.character_sheets.aspect not in {member.value for member in Aspect}:
            raise ConfigError(
                f"character_sheets.aspect has unsupported value {self.character_sheets.aspect!r}"
            )
        if self.media.poll.max_s < self.media.poll.initial_s:
            raise ConfigError("media.poll.max_s must be >= media.poll.initial_s")
        return self

    @field_validator("languages", mode="before")
    @classmethod
    def _validate_languages(cls, v: object) -> object:
        if not isinstance(v, list):
            raise ConfigError("languages must be a list")
        raw = [str(x) for x in v]
        if not raw:
            raise ConfigError("languages must be non-empty")
        _dedupe(raw, what="languages")
        allowed = {member.value for member in Language}
        bad = [x for x in raw if x not in allowed]
        if bad:
            raise ConfigError(
                f"unsupported language(s): {', '.join(bad)} (allowed: {', '.join(sorted(allowed))})"
            )
        return raw

    @field_validator("aspects", mode="before")
    @classmethod
    def _validate_aspects(cls, v: object) -> object:
        if not isinstance(v, list):
            raise ConfigError("aspects must be a list")
        raw = [str(x) for x in v]
        if not raw:
            raise ConfigError("aspects must be non-empty")
        _dedupe(raw, what="aspects")
        allowed = {member.value for member in Aspect}
        bad = [x for x in raw if x not in allowed]
        if bad:
            raise ConfigError(
                f"unsupported aspect(s): {', '.join(bad)} (allowed: {', '.join(sorted(allowed))})"
            )
        return raw

    def resolved_paths(self, home: Path) -> Settings:
        """Anchor every relative path under `home`, returning a copy."""

        def anchor(rel: str) -> str:
            p = Path(rel)
            return str(p if p.is_absolute() else (home / p).resolve())

        paths = PathSettings(
            home=str(home.resolve()),
            db=anchor(self.paths.db),
            cas=anchor(self.paths.cas),
            exports=anchor(self.paths.exports),
            projects=anchor(self.paths.projects),
        )
        return self.model_copy(update={"paths": paths})


def load_settings_file(path: Path) -> Settings:
    settings, _notes = load_settings_file_with_report(path)
    return settings


def load_settings_file_with_report(path: Path) -> tuple[Settings, list[str]]:
    """Load settings, dropping deprecated keys with a DeprecationWarning."""
    source = str(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read: {exc}") from exc
    text = interpolate_env(raw, source=source)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    notes = strip_deprecated_settings(data, source=source)
    try:
        return Settings.model_validate(data), notes
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "<root>"
            detail = str(first.get("msg", exc))
        else:
            loc, detail = "<root>", str(exc)
        raise ConfigError(f"{source}: {loc}: {detail}") from exc
