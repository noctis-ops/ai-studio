"""Free, fail-closed media preflight stages: ``s4p_media_preflight`` and
``s10p_clip_preflight``.

Split from ``studio.media.stages`` in Phase 5 (H5.8) as a pure move.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from studio.config.registry import ModelEntry
from studio.domain.enums import BudgetScope, Language, Profile
from studio.domain.errors import (
    BudgetExceeded,
    MediaConfigError,
    PermanentProviderError,
    PreflightError,
    StudioError,
)
from studio.domain.money import musd_to_usd, usd_to_musd
from studio.domain.refs import SubjectRef
from studio.media.common import (
    _S4_STAGE,
    _episode,
    _episode_subject,
    _model_entry,
    _pinned_bible,
    _sha,
    _stage_output,
    _track_shots,
    logger,
)
from studio.media.registry import (
    aspect_value,
    max_refs,
    model_confirmation_problems,
    supported_aspects,
    supported_durations_s,
)
from studio.pipeline.estimate_media import media_estimate, media_estimate_hash_parts
from studio.pipeline.stage import StageOutput

if TYPE_CHECKING:
    from studio.domain.models import EpisodeRow
    from studio.pipeline.context import RunContext

def _media_model_problems(
    ctx: RunContext, *, final: bool, need_refs: bool
) -> list[str]:
    """Every media-registry problem, listed at once (s4p/s10p preflights)."""
    settings = ctx.config.settings
    problems: list[str] = []
    for role in ("sheets", "keyframes", "clips"):
        try:
            key, entry = _model_entry(ctx, role)
        except MediaConfigError as exc:
            problems.append(f"{role}: {exc}")
            continue
        except StudioError as exc:
            problems.append(f"{role}: {exc}")
            continue
        if entry.provider == "fal":
            import os

            if not os.environ.get(settings.fal.key_env):
                problems.append(
                    f"{role} model {key!r} requires {settings.fal.key_env} to be set"
                )
        for problem in model_confirmation_problems(entry, final=final):
            problems.append(f"{role} model {key!r}: {problem}")
        if role == "clips":
            # supported_durations_s raises on an empty contract; the preflight
            # must list the problem, not crash the whole check.
            try:
                clips_durations = supported_durations_s(entry)
            except MediaConfigError:
                clips_durations = ()
            if not clips_durations:
                problems.append(f"clips model {key!r} declares no supported durations")
        if need_refs and role == "keyframes":
            if entry.supports_refs is not True:
                problems.append(
                    f"keyframes model {key!r} is not marked reference-capable "
                    f"(supports_refs={entry.supports_refs})"
                )
            if max_refs(entry) < 1:
                problems.append(f"keyframes model {key!r} declares max_refs < 1")
    return problems


def _aspect_problems(
    ctx: RunContext, entries: dict[str, tuple[str, ModelEntry]]
) -> list[str]:
    problems: list[str] = []
    track_aspects = {aspect for aspect in ctx.aspects}
    for role, (key, entry) in entries.items():
        try:
            supported = supported_aspects(entry)
        except MediaConfigError as exc:
            problems.append(f"{role}: {exc}")
            continue
        supported_values = {aspect.value for aspect in supported}
        for aspect in track_aspects:
            if aspect.value not in supported_values:
                problems.append(
                    f"{role} model {key!r} does not support track aspect {aspect.value}"
                )
            else:
                try:
                    aspect_value(entry, aspect)
                except MediaConfigError as exc:
                    problems.append(f"{role}: {exc}")
    return problems


def _budget_row_cap(
    ctx: RunContext, scope: BudgetScope, ref: str, default_usd: float | None
) -> int | None:
    row = ctx.repos.budgets.get(scope.value, ref)
    if row is not None:
        return row.cap_musd
    if default_usd is None:
        return None
    return usd_to_musd(str(default_usd))


def _remaining_musd(
    ctx: RunContext, scope: BudgetScope, ref: str, cap_musd: int | None
) -> int | None:
    if cap_musd is None:
        return None
    held = ctx.repos.budgets.held_musd(scope.value, ref)
    return cap_musd - held


def _worst_case_pause(
    ctx: RunContext,
    episode: EpisodeRow,
    *,
    worst_case_musd: int,
    label: str,
) -> None:
    """Whole-episode affordability guard; raises before any spend."""
    settings = ctx.config.settings
    month_ref = ctx.clock.month_tag()
    caps: list[tuple[BudgetScope, str, int | None]] = [
        (
            BudgetScope.EPISODE,
            episode.id,
            _budget_row_cap(
                ctx,
                BudgetScope.EPISODE,
                episode.id,
                float(settings.budgets.default_episode_cap_usd),
            ),
        ),
        (
            BudgetScope.MONTH,
            month_ref,
            _budget_row_cap(
                ctx,
                BudgetScope.MONTH,
                month_ref,
                float(settings.budgets.monthly_cap_usd),
            ),
        ),
    ]
    global_cap = settings.budgets.global_cap_usd
    if global_cap is not None:
        caps.append(
            (
                BudgetScope.GLOBAL,
                "global",
                _budget_row_cap(ctx, BudgetScope.GLOBAL, "global", float(global_cap)),
            )
        )
    for scope, ref, cap in caps:
        if cap is None:
            continue
        remaining = _remaining_musd(ctx, scope, ref, cap)
        if remaining is not None and worst_case_musd > remaining:
            raise BudgetExceeded(
                f"{label} worst-case cost ({musd_to_usd(worst_case_musd)} USD) exceeds "
                f"the remaining {scope.value} budget (cap {musd_to_usd(cap)} USD, "
                f"available {musd_to_usd(remaining)} USD); refusing before any spend",
                scope=scope.value,
                scope_ref=ref,
                cap_musd=cap,
                held_musd=cap - remaining,
                requested_musd=worst_case_musd,
            )


def _episode_estimate_totals(ctx: RunContext, episode: EpisodeRow) -> dict[str, Any] | None:
    """H2 estimate summed over deliverables (sheets counted once).

    Returns None when any estimate is unavailable — the model-confirmation
    problems already fail closed in that case; never emit a $0 total.
    """
    deliverables = ctx.repos.content.list_deliverables(episode.id)
    if not deliverables:
        return None
    expected = 0
    high = 0
    sheets_expected = 0
    sheets_high = 0
    estimates: list[dict[str, Any]] = []
    for deliverable in deliverables:
        estimate = media_estimate(ctx, deliverable.id)
        if not estimate.available:
            return None
        expected += (
            estimate.tts["expected_musd"]
            + estimate.keyframes["expected_musd"]
            + estimate.video["expected_musd"]
        )
        high += (
            estimate.tts["high_musd"]
            + estimate.keyframes["high_musd"]
            + estimate.video["high_musd"]
        )
        sheets_expected = max(sheets_expected, estimate.sheets["expected_musd"])
        sheets_high = max(sheets_high, estimate.sheets["high_musd"])
        estimates.append(estimate.as_dict())
    return {
        "expected_musd": expected + sheets_expected,
        "high_musd": high + sheets_high,
        "sheets_expected_musd": sheets_expected,
        "sheets_high_musd": sheets_high,
        "per_deliverable": estimates,
    }


def _s4_baseline_musd(ctx: RunContext, deliverable_ids: list[str]) -> int | None:
    baselines: list[int] = []
    for deliverable_id in deliverable_ids:
        output = _stage_output(ctx, _S4_STAGE, deliverable_id)
        if not isinstance(output, dict):
            continue
        for key in ("projected_video_cost_musd", "gate1_video_estimate_musd"):
            value = output.get(key)
            if isinstance(value, int) and value >= 0:
                baselines.append(value)
                break
    return max(baselines) if baselines else None


def _estimate_drift(
    ctx: RunContext, *, projected_musd: int
) -> dict[str, Any] | None:
    """Same shape as S6's drift check (§F/§I). Returns the drift dict when the
    S4 baseline exists and is exceeded; emits the `estimate_drift` audit."""
    episode = _episode(ctx)
    if episode is None:
        return None
    deliverables = [
        d.id for d in ctx.repos.content.list_deliverables(episode.id)
        if Language(d.language) in ctx.languages
    ]
    baseline = _s4_baseline_musd(ctx, deliverables)
    if baseline is None:
        return None
    import math

    warn_pct = float(ctx.config.settings.timing.estimate_drift_warn_pct)
    if baseline == 0:
        exceeded = projected_musd > 0
        drift_pct: float | None = None if projected_musd == 0 else math.inf
    else:
        drift_pct = (projected_musd - baseline) * 100.0 / baseline
        exceeded = drift_pct > warn_pct
    if not exceeded:
        return None
    ctx.repos.audit.append(
        actor="system",
        entity=f"episode:{episode.id}",
        event="estimate_drift",
        payload={
            "episode_id": episode.id,
            "track_id": None,
            "baseline_musd": baseline,
            "projected_musd": projected_musd,
            "drift_pct": drift_pct,
            "estimate_drift_warn_pct": warn_pct,
        },
    )
    return {
        "baseline_musd": baseline,
        "projected_musd": projected_musd,
        "drift_pct": drift_pct,
    }


def _clip_cost_musd(entry: ModelEntry, clip_duration_ms: int, shots: int) -> int:
    """Exact first-pass cost from S6's locked durations (§I output key)."""
    from decimal import Decimal

    from studio.config.registry import PerSecond, PerVideo

    if isinstance(entry.pricing, PerSecond):
        return usd_to_musd(Decimal(str(clip_duration_ms / 1000.0)) * entry.pricing.usd)
    if isinstance(entry.pricing, PerVideo):
        return usd_to_musd(Decimal(shots) * entry.pricing.usd)
    raise MediaConfigError(
        f"clip cost projection requires per_second or per_video pricing; "
        f"got {entry.pricing.unit!r}"
    )


class MediaPreflightHandler:
    """Collects every media problem at once, then halts before any spend."""

    _STAGE = "s4p_media_preflight"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _episode_subject(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        settings = ctx.config.settings
        return _sha(
            {
                "stage": self._STAGE,
                "episode_id": subject.id,
                "profile": ctx.resolved_profile.name,
                "languages": sorted(lang.value for lang in ctx.languages),
                "aspects": sorted(aspect.value for aspect in ctx.aspects),
                "media_estimate": media_estimate_hash_parts(ctx),
                "sanitizer": {
                    "max_levels": settings.sanitizer.max_levels,
                    "model_key": settings.sanitizer.model_key,
                    "max_scene_chars": settings.sanitizer.max_scene_chars,
                },
                "media": {
                    "max_paid_attempts_per_shot": settings.media.max_paid_attempts_per_shot,
                    "max_inline_bytes": settings.media.max_inline_bytes,
                    "refs_overflow": settings.media.refs_overflow,
                    "ref_max_side_px": settings.media.ref_max_side_px,
                    "chain_mode": settings.media.chain_mode,
                },
                "character_sheets": {
                    "roles": list(settings.character_sheets.roles),
                    "aspect": settings.character_sheets.aspect,
                },
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        settings = ctx.config.settings
        problems: list[str] = []
        final = Profile(ctx.resolved_profile.name) is Profile.FINAL

        # ---- registry: every model resolves, confirmed, prices verified
        entries: dict[str, tuple[str, ModelEntry]] = {}
        for role in ("sheets", "keyframes", "clips"):
            try:
                entries[role] = _model_entry(ctx, role)
            except Exception as exc:  # reported by _media_model_problems below
                logger.debug("media model for role %s unresolved: %s", role, exc)
        bible = _pinned_bible(ctx, episode.id)
        characters = [c.name for c in bible.characters] if bible is not None else []
        problems.extend(_media_model_problems(ctx, final=final, need_refs=bool(characters)))
        problems.extend(_aspect_problems(ctx, entries))
        if settings.media.chain_mode != "off":
            problems.append(
                f"media.chain_mode is {settings.media.chain_mode!r}; "
                f"only 'off' is supported before Phase 5"
            )
        # ---- bible character names must be unique (sheet identity)
        if characters:
            seen: set[str] = set()
            duplicates_set: set[str] = set()
            for name in characters:
                if name in seen:
                    duplicates_set.add(name)
                seen.add(name)
            if duplicates_set:
                problems.append(
                    f"series bible lists duplicate character names: "
                    f"{', '.join(sorted(duplicates_set))}"
                )

        if problems:
            raise PreflightError(
                "media preflight failed before any spend:\n- " + "\n- ".join(sorted(problems))
            )

        # ---- affordability: halt when the expected total exceeds the
        # remaining episode or monthly budget; warn when only high does.
        totals = _episode_estimate_totals(ctx, episode)
        affordability: dict[str, Any] | None = None
        if totals is None:
            ctx.repos.audit.append(
                actor="system",
                entity=f"episode:{episode.id}",
                event="media_affordability_skipped",
                payload={"episode_id": episode.id,
                         "reason": "media estimate unavailable (see preflight model checks)"},
            )
        else:
            from studio.domain.enums import GLOBAL_SCOPE_REF

            month_ref = ctx.clock.month_tag()
            caps: list[tuple[BudgetScope, str, int | None]] = [
                (
                    BudgetScope.EPISODE,
                    episode.id,
                    _budget_row_cap(
                        ctx, BudgetScope.EPISODE, episode.id,
                        float(settings.budgets.default_episode_cap_usd),
                    ),
                ),
                (
                    BudgetScope.MONTH,
                    month_ref,
                    _budget_row_cap(
                        ctx, BudgetScope.MONTH, month_ref,
                        float(settings.budgets.monthly_cap_usd),
                    ),
                ),
            ]
            if settings.budgets.global_cap_usd is not None:
                caps.append(
                    (
                        BudgetScope.GLOBAL,
                        GLOBAL_SCOPE_REF,
                        _budget_row_cap(
                            ctx, BudgetScope.GLOBAL, GLOBAL_SCOPE_REF,
                            float(settings.budgets.global_cap_usd),
                        ),
                    )
                )
            for scope, ref, cap in caps:
                if cap is None:
                    continue
                remaining = _remaining_musd(ctx, scope, ref, cap)
                if remaining is None:
                    continue
                if totals["expected_musd"] > remaining:
                    raise BudgetExceeded(
                        f"expected media spend ({musd_to_usd(totals['expected_musd'])} USD) "
                        f"exceeds the remaining {scope.value} budget "
                        f"(cap {musd_to_usd(cap)} USD, available {musd_to_usd(remaining)} USD); "
                        f"refusing before any spend",
                        scope=scope.value,
                        scope_ref=ref,
                        cap_musd=cap,
                        held_musd=cap - remaining,
                        requested_musd=int(totals["expected_musd"]),
                    )
                if totals["high_musd"] > remaining:
                    ctx.repos.audit.append(
                        actor="system",
                        entity=f"episode:{episode.id}",
                        event="media_estimate_high_exceeds_budget",
                        payload={
                            "episode_id": episode.id,
                            "scope": scope.value,
                            "scope_ref": ref,
                            "cap_musd": cap,
                            "remaining_musd": remaining,
                            "expected_musd": totals["expected_musd"],
                            "high_musd": totals["high_musd"],
                        },
                    )
            affordability = {
                "expected_musd": totals["expected_musd"],
                "high_musd": totals["high_musd"],
                "sheets_expected_musd": totals["sheets_expected_musd"],
                "headroom_by_scope": {
                    scope.value: (
                        _remaining_musd(ctx, scope, ref, cap)
                         if cap is not None else None
                    )
                    for scope, ref, cap in caps
                },
            }

        # ---- per-deliverable estimate additive keys (for the operator)
        estimate_out: dict[str, Any] | None = None
        for deliverable in ctx.repos.content.list_deliverables(episode.id):
            if estimate_out is None:
                estimate_out = media_estimate(ctx, deliverable.id).additive_output()
        return StageOutput(
            output={
                "episode_id": episode.id,
                "languages": sorted(lang.value for lang in ctx.languages),
                "aspects": sorted(aspect.value for aspect in ctx.aspects),
                "characters": characters,
                "models": {role: key for role, (key, _e) in sorted(entries.items())},
                "estimate": estimate_out,
                "affordability": affordability,
            }
        )


class ClipPreflightHandler:
    """Rechecks model readiness and halts if the exact first-pass clip cost
    exceeds the remaining budget. Retries are protected per call by the
    gateway reservation."""

    _STAGE = "s10p_clip_preflight"

    def subjects(self, ctx: RunContext) -> list[SubjectRef]:
        return _episode_subject(ctx)

    def input_hash(self, ctx: RunContext, subject: SubjectRef) -> str:
        episode = _episode(ctx)
        assert episode is not None
        shots = _track_shots(ctx)
        clips_key = None
        durations: list[int] = []
        price_unit = None
        try:
            clips_key, clips_entry = _model_entry(ctx, "clips")
            durations = list(supported_durations_s(clips_entry))
            price_unit = clips_entry.pricing.unit
        except Exception as exc:
            logger.debug("clips model unresolved during preflight hash: %s", exc)
        settings = ctx.config.settings
        return _sha(
            {
                "stage": self._STAGE,
                "episode_id": subject.id,
                "profile": ctx.resolved_profile.name,
                "video_model_key": clips_key,
                "price_unit": price_unit,
                "supported_durations_s": durations,
                "locked": [
                    {"shot_id": shot.id, "clip_duration_ms": shot.clip_duration_ms}
                    for shot in shots
                ],
                "fps": settings.render.fps,
                "media_estimate": media_estimate_hash_parts(ctx),
            }
        )

    async def run(self, ctx: RunContext, subject: SubjectRef) -> StageOutput:
        episode = _episode(ctx)
        if episode is None:
            raise PermanentProviderError(f"episode not found: {ctx.episode_id}")
        final = Profile(ctx.resolved_profile.name) is Profile.FINAL
        problems = _media_model_problems(ctx, final=final, need_refs=False)

        shots = _track_shots(ctx)
        try:
            clips_key, clips_entry = _model_entry(ctx, "clips")
        except Exception as exc:
            problems.append(f"clips: {exc}")
            clips_key, clips_entry = None, None

        supported_ms: list[int] = []
        if clips_entry is not None:
            try:
                supported_ms = [d * 1000 for d in supported_durations_s(clips_entry)]
            except MediaConfigError as exc:
                problems.append(f"clips: {exc}")
        for shot in shots:
            if shot.clip_duration_ms is None or shot.timing_status != "locked":
                problems.append(
                    f"shot {shot.shot_no} ({shot.id}) has no locked duration; run S6 first"
                )
                continue
            if supported_ms and int(shot.clip_duration_ms) not in supported_ms:
                problems.append(
                    f"shot {shot.shot_no} ({shot.id}) locked duration "
                    f"{shot.clip_duration_ms} ms is not in the clips model's "
                    f"supported durations {sorted(supported_ms)}; re-run S6"
                )
        if problems:
            raise PreflightError(
                "clip preflight failed; no video call will be made:\n- "
                + "\n- ".join(sorted(problems))
            )
        assert clips_entry is not None and clips_key is not None

        # ---- exact first-pass worst case (locked durations only)
        from decimal import Decimal

        from studio.config.registry import PerSecond, PerVideo

        total = 0
        seconds = 0.0
        for shot in shots:
            locked_ms = int(shot.clip_duration_ms or 0)
            seconds += locked_ms / 1000.0
            if isinstance(clips_entry.pricing, PerSecond):
                total += usd_to_musd(Decimal(locked_ms) / Decimal(1000) * clips_entry.pricing.usd)
            elif isinstance(clips_entry.pricing, PerVideo):
                total += usd_to_musd(Decimal(1) * clips_entry.pricing.usd)
            # zero-priced simulated models (sandbox) reserve nothing
        _worst_case_pause(
            ctx,
            episode,
            worst_case_musd=total,
            label="first-pass clip generation",
        )
        ctx.repos.audit.append(
            actor="system",
            entity=f"episode:{episode.id}",
            event="clip_preflight_passed",
            payload={
                "episode_id": episode.id,
                "shots": len(shots),
                "video_model_key": clips_key,
                "first_pass_worst_case_musd": total,
                "locked_seconds": seconds,
            },
        )
        return StageOutput(
            output={
                "episode_id": episode.id,
                "shots": len(shots),
                "video_model_key": clips_key,
                "first_pass_worst_case_musd": total,
                "locked_seconds": seconds,
            }
        )
