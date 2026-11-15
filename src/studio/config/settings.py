"""Settings model + `${ENV}` interpolation.

The config files are the only place tuning knobs live; every tunable has a
typed field here. Unknown keys are rejected (`extra="forbid"`).
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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


class TextPipelineSettings(_Strict):
    context_token_budget: int = Field(default=6000, ge=500, le=100000)
    tashkeel_retry: int = Field(default=1, ge=0, le=2)
    s4_after_g1: bool = True


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
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "<root>"
            detail = str(first.get("msg", exc))
        else:
            loc, detail = "<root>", str(exc)
        raise ConfigError(f"{source}: {loc}: {detail}") from exc
