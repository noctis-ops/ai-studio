"""Model registry: discriminated pricing models, profile resolution, export guard.

Key guarantees (enforced here, relied on everywhere else):
- profile roles reference existing keys with the matching capability
- `placeholder` is *derived* (any string containing `<CONFIRM`)
- non-simulated placeholders can never resolve — not even in sandbox
- `draft`/`final` refuse non-simulated zero-priced models unless the operator
  acknowledged them free; zero prices would silently disable budget protection
- `registry_snapshot` is deterministic and goes into pipeline_runs.config_snapshot_json
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.domain.enums import Profile
from studio.domain.errors import CommercialUseNotVerified, ConfigError, RegistryError

ZERO = Decimal(0)


class Per1mTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: Literal["per_1m_tokens"]
    in_usd: Decimal = Field(ge=0)
    out_usd: Decimal = Field(ge=0)


class Per1kChars(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: Literal["per_1k_chars"]
    usd: Decimal = Field(ge=0)


class PerImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: Literal["per_image"]
    usd: Decimal = Field(ge=0)


class PerSecond(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: Literal["per_second"]
    usd: Decimal = Field(ge=0)


Pricing = Annotated[Per1mTokens | Per1kChars | PerImage | PerSecond, Field(discriminator="unit")]


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    capability: str
    model_id: str | None = None
    endpoint_id: str | None = None
    simulated: bool = False
    supported_aspects: list[str] = []
    supported_durations_s: list[int] = []
    max_resolution: str | None = None
    pricing: Pricing
    pricing_verified_on: date | None = None
    commercial_use_verified: bool = False
    commercial_use_note: str | None = None
    pricing_acknowledged_free: bool = False
    param_map: dict[str, str] = {}
    needs_confirmation: bool = False

    @property
    def model_ref(self) -> str:
        return self.model_id or self.endpoint_id or ""

    @property
    def placeholder(self) -> bool:
        return "<CONFIRM" in (self.model_id or "") or "<CONFIRM" in (self.endpoint_id or "")

    @property
    def is_zero_price(self) -> bool:
        if isinstance(self.pricing, Per1mTokens):
            return self.pricing.in_usd == ZERO and self.pricing.out_usd == ZERO
        return self.pricing.usd == ZERO


class ProviderLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=1, ge=1)
    min_interval_s: float = Field(default=0.0, ge=0)


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_version: int
    models: dict[str, ModelEntry]
    profiles: dict[str, dict[str, str]]
    limits: dict[str, ProviderLimit] = {}


ROLE_CAPABILITY: dict[str, str] = {
    "llm_creative": "llm",
    "llm_light": "llm",
    "tts": "tts",
    "keyframe": "image_reference_generate",
    "video": "image_to_video",
}


class ResolvedProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    registry_version: int
    model_keys: dict[str, str]
    entries: dict[str, ModelEntry]

    def model_key_for(self, role: str) -> str:
        try:
            return self.model_keys[role]
        except KeyError as exc:
            raise ConfigError(f"profile {self.name!r} has no role {role!r}") from exc

    def entry_for(self, role: str) -> ModelEntry:
        try:
            return self.entries[role]
        except KeyError as exc:
            raise ConfigError(f"profile {self.name!r} has no role {role!r}") from exc

    @property
    def uses_only_simulated(self) -> bool:
        return all(entry.simulated for entry in self.entries.values())


class _AuditSink(Protocol):
    def append(self, actor: str, entity: str, event: str, payload: dict[str, Any]) -> object: ...


def _zero_price_problem(entry: ModelEntry) -> str:
    unit = entry.pricing.unit
    if isinstance(entry.pricing, Per1mTokens):
        return f"pricing {unit} in_usd={entry.pricing.in_usd} out_usd={entry.pricing.out_usd}"
    return f"pricing {unit} usd={entry.pricing.usd}"


class ModelRegistry:
    def __init__(self, data: RegistryFile, *, source: str = "<registry>") -> None:
        self._data = data
        self.source = source
        self._validate_profiles()

    # ---- validation -------------------------------------------------------

    def _validate_profiles(self) -> None:
        for profile_name, role_map in self._data.profiles.items():
            for role, key in role_map.items():
                if role not in ROLE_CAPABILITY:
                    raise RegistryError(
                        f"{self.source}: profiles.{profile_name}.{role}: unknown role"
                    )
                entry = self._data.models.get(key)
                if entry is None:
                    raise RegistryError(
                        f"{self.source}: profiles.{profile_name}.{role}: missing model {key!r}"
                    )
                want = ROLE_CAPABILITY[role]
                if entry.capability != want:
                    raise RegistryError(
                        f"{self.source}: profiles.{profile_name}.{role}: model {key!r} capability "
                        f"{entry.capability!r}, expected {want!r}"
                    )

    # ---- accessors ----------------------------------------------------------

    @property
    def registry_version(self) -> int:
        return self._data.registry_version

    @property
    def models(self) -> dict[str, ModelEntry]:
        return dict(self._data.models)

    @property
    def limits(self) -> dict[str, ProviderLimit]:
        return dict(self._data.limits)

    @property
    def profiles(self) -> dict[str, dict[str, str]]:
        return {name: dict(role_map) for name, role_map in self._data.profiles.items()}

    # ---- resolution ----------------------------------------------------------

    def resolve_profile(self, name: str) -> ResolvedProfile:
        role_map = self._data.profiles.get(name)
        if role_map is None:
            known = ", ".join(sorted(self._data.profiles))
            raise RegistryError(f"{self.source}: unknown profile {name!r} (known: {known})")
        model_keys: dict[str, str] = {}
        entries: dict[str, ModelEntry] = {}
        for role, key in role_map.items():
            entry = self._data.models.get(key)
            if entry is None:
                raise RegistryError(f"{self.source}: profiles.{name}.{role}: missing model {key!r}")
            want = ROLE_CAPABILITY[role]
            if entry.capability != want:
                raise RegistryError(
                    f"{self.source}: profiles.{name}.{role}: model {key!r} has capability "
                    f"{entry.capability!r}, expected {want!r}"
                )
            model_keys[role] = key
            entries[role] = entry
        for role in sorted(entries):
            entry = entries[role]
            if not entry.simulated and entry.placeholder:
                raise RegistryError(
                    f"{self.source}: model {model_keys[role]!r} (role {role}) has an unresolved "
                    f"placeholder model_ref {entry.model_ref!r}; fix model_registry.yaml first"
                )
        if name in (Profile.DRAFT.value, Profile.FINAL.value):
            for role in sorted(entries):
                entry = entries[role]
                if entry.simulated:
                    continue
                if entry.is_zero_price and not entry.pricing_acknowledged_free:
                    raise RegistryError(
                        f"{self.source}: model {model_keys[role]!r} (role {role}) has zero price "
                        f"({_zero_price_problem(entry)}); refusing to resolve profile {name!r}. "
                        f"Enter real prices or set pricing_acknowledged_free: true"
                    )
        return ResolvedProfile(
            name=name,
            registry_version=self._data.registry_version,
            model_keys=model_keys,
            entries=entries,
        )

    # ---- snapshot / export guard ----------------------------------------------

    def registry_snapshot(self, resolved: ResolvedProfile) -> dict[str, Any]:
        models: dict[str, Any] = {}
        for role in sorted(resolved.model_keys):
            key = resolved.model_keys[role]
            entry = resolved.entries[role]
            models[role] = {
                "model_key": key,
                "provider": entry.provider,
                "model_ref": entry.model_ref,
                "capability": entry.capability,
                "simulated": entry.simulated,
                "pricing": entry.pricing.model_dump(mode="json"),
            }
        return {
            "registry_version": resolved.registry_version,
            "profile": resolved.name,
            "models": models,
        }

    def assert_exportable(
        self,
        resolved: ResolvedProfile,
        *,
        override: bool = False,
        audit: _AuditSink | None = None,
    ) -> None:
        if resolved.name != Profile.FINAL.value:
            return
        unverified = sorted(
            role for role, entry in resolved.entries.items() if not entry.commercial_use_verified
        )
        if not unverified:
            return
        if not override:
            refs = ", ".join(f"{role}={resolved.model_keys[role]}" for role in unverified)
            raise CommercialUseNotVerified(
                f"final profile uses models without verified commercial use: {refs}; "
                f"verify licences or pass override=True (writes an audit event)"
            )
        if audit is None:
            # Fail closed: an override nobody records is not an override.
            raise ConfigError("commercial_use override requires an audit sink")
        audit.append(
            actor="operator",
            entity=f"profile:{resolved.name}",
            event="commercial_use_override",
            payload={
                "unverified_roles": unverified,
                "models": {role: resolved.model_keys[role] for role in unverified},
            },
        )


def load_registry_file(path: Path) -> ModelRegistry:
    source = str(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    try:
        parsed = RegistryFile.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "<root>"
            detail = str(first.get("msg", exc))
        else:
            loc, detail = "<root>", str(exc)
        raise RegistryError(f"{source}: {loc}: {detail}") from exc
    return ModelRegistry(parsed, source=source)
