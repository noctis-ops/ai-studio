"""T-PRM-01, T-SAN-01..03 (PHASE4 §P prompts & sanitizer tests).

Fully offline: scripted in-process providers injected through the gateway.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import make_home

from studio.config.loader import load_all
from studio.core.budget import LedgerService
from studio.core.cache_key import make_cache_key
from studio.core.cas import ContentStore
from studio.core.clock import FakeClock, no_op_sleep
from studio.core.gateway import ProviderGateway
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import Aspect, Language
from studio.domain.errors import (
    ContentPolicyError,
    FailedPolicy,
    TransientProviderError,
)
from studio.domain.refs import AssetRef  # noqa: F401  (re-exported for clarity)
from studio.media import stages as media_stages
from studio.media.prompts import (
    compose_keyframe_prompt,
    compose_motion_prompt_spec,
    prompt_version_sha,
)
from studio.media.sanitizer import (
    apply_lexicon,
    sanitize_scene_text,
    validate_rewrite,
)
from studio.pipeline.context import RunContext
from studio.providers.base import (
    JobHandle,
    JobState,
    LLMResult,
    MediaPayload,
    MediaRequest,
)
from studio.qc.visual import QcCheck


def _env(tmp_path, monkeypatch, *, filename="p4prm", settings_overrides=None):
    home = make_home(tmp_path, filename=filename, settings_overrides=settings_overrides)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    from studio.core.ids import SequentialIds

    ids = SequentialIds(filename)
    repos = build_repos(db, clock, ids)
    return home, config, db, clock, ids, repos


def _gateway(config, db, clock, ids, *, providers):
    return ProviderGateway(
        db=db,
        settings=config.settings,
        registry=config.registry,
        cas=ContentStore(Path(config.settings.paths.cas), db, clock),
        ledger=LedgerService(db, clock, config.settings, ids),
        clock=clock,
        ids=ids,
        providers=providers,
        poll_interval_s=0.0,
        sleep=no_op_sleep,
    )


def _ctx(config, db, repos, gateway, clock, ids):
    return RunContext(
        db=db,
        repos=repos,
        gateway=gateway,
        clock=clock,
        ids=ids,
        config=config,
        resolved_profile=config.registry.resolve_profile("sandbox"),
        run_id="run-test",
        episode_id=None,
        languages=[Language.AR],
        aspects=[Aspect.TALL],
        stage_run_id=None,
        gate_cleared=True,
    )


class _ScriptedImage:
    """In-process image provider: refuse N times, then succeed."""

    def __init__(self, outcomes: list[str], png: Path) -> None:
        self.outcomes = list(outcomes)
        self.png = png
        self.submits: list[dict] = []

    async def submit(self, req: MediaRequest) -> JobHandle:
        self.submits.append(dict(req.params))
        outcome = self.outcomes[min(len(self.submits) - 1, len(self.outcomes) - 1)]
        if outcome == "refuse":
            raise ContentPolicyError("fal 422 content policy", billed=False)
        if outcome == "transient":
            raise TransientProviderError("fal 500 transient")
        return JobHandle(
            remote_request_id=f"req-{len(self.submits)}",
            meta={"status_url": "https://q/status", "response_url": "https://q/resp",
                  "cancel_url": "https://q/cancel"},
        )

    async def poll(self, handle: JobHandle) -> JobState:
        return JobState(state="succeeded")

    async def fetch(self, handle: JobHandle):
        import shutil
        import tempfile

        with tempfile.NamedTemporaryFile(prefix="scripted-img-", suffix=".png",
                                         delete=False) as handle_tmp:
            tmp = Path(handle_tmp.name)
        shutil.copy(self.png, tmp)
        return [MediaPayload(local_path=tmp, ext="png", mime="image/png")]


class _FakeLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    async def complete(self, req):
        self.calls.append(req.prompt)
        return LLMResult(text=self.text, parsed_json=None, tokens_in=20, tokens_out=20)


def _png(tmp_path: Path) -> Path:
    path = tmp_path / "img.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return path


def _pass_qc(path):
    async def run():
        return (
            [QcCheck(name="ok", status="pass", retryable=False, value=1, threshold=0)],
            None,
            {},
        )

    return run()


def _fail_qc(path):
    async def run():
        return (
            [QcCheck(name="black", status="fail", retryable=True, value=0.0, threshold=1.0)],
            None,
            {},
        )

    return run()


def _make_prompt_factory(home):
    def make_prompt(scene_override: str | None):
        spec = compose_keyframe_prompt(
            style_tokens=["warm cinema"],
            characters=[("نور", "calm keeper")],
            shot_visual="the lighthouse keeper faces the storm",
            ref_clause_template=None,
            home=None,
        )
        if scene_override is not None:
            spec = spec.with_scene(scene_override)
        return spec

    def make_request(spec, seed, nonce):
        return MediaRequest(
            capability="image_reference_generate",
            params={"prompt": spec.full_prompt, "negative": spec.negative},
            aspect=Aspect.TALL,
            seed=seed,
            prompt_version_sha=spec.prompt_version_sha,
        )

    return make_prompt, make_request


# ======================================================================
# T-PRM-01 — deterministic composition, locked parts, prompt-version sha
# ======================================================================


def test_prm_01_composition_is_deterministic():
    a = compose_keyframe_prompt(
        style_tokens=["warm cinema"],
        characters=[("نور", "calm keeper"), ("العاصفة", "living storm")],
        shot_visual="the keeper watches the horizon",
        ref_clause_template="The character {name} must look like reference {n}.",
        home=None,
    )
    b = compose_keyframe_prompt(
        style_tokens=["warm cinema"],
        characters=[("نور", "calm keeper"), ("العاصفة", "living storm")],
        shot_visual="the keeper watches the horizon",
        ref_clause_template="The character {name} must look like reference {n}.",
        home=None,
    )
    assert a == b
    assert len(a.prompt_version_sha) == 64


def test_prm_01_locked_parts_byte_identical_across_sanitize_levels():
    base = compose_motion_prompt_spec(
        motion="the keeper turns toward the light. Gentle natural motion.",
        home=None,
    )
    sanitized = base.with_scene("a figure turns toward the glow, implied")
    assert sanitized.locked_prefix == base.locked_prefix
    assert sanitized.locked_suffix == base.locked_suffix
    assert sanitized.negative == base.negative
    assert sanitized.prompt_version_sha == base.prompt_version_sha
    assert base.scene_text in base.full_prompt
    assert sanitized.scene_text in sanitized.full_prompt
    assert base.scene_text not in sanitized.full_prompt


def test_prm_01_prompt_version_sha_is_in_the_cache_key():
    kwargs = dict(
        capability="image_reference_generate",
        model_key="mock.image",
        model_ref="mock-image",
        params={"prompt": "x"},
        input_asset_sha256s=["abc"],
        seed=7,
        nonce="n",
    )
    a = make_cache_key(prompt_version_sha="a" * 64, **kwargs)
    b = make_cache_key(prompt_version_sha="b" * 64, **kwargs)
    assert a != b
    # and a stable sha keeps the key stable
    assert a == make_cache_key(prompt_version_sha="a" * 64, **kwargs)


def test_prm_01_prompt_version_sha_matches_template_bytes():
    assert prompt_version_sha(None) == compose_motion_prompt_spec(
        motion="anything", home=None
    ).prompt_version_sha


# ======================================================================
# T-SAN-01 — refusal → level 1 → success; resume; cap → failed_policy
# ======================================================================


def test_san_01_refusal_then_level1_success_logged_and_resumable(
    tmp_path, monkeypatch
):
    home, config, db, clock, ids, repos = _env(tmp_path, monkeypatch)
    png = _png(tmp_path)
    image = _ScriptedImage(["refuse", "ok"], png)
    llm = _FakeLLM("a figure faces the gathering dark, implied")
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.image": image, "llm.light": llm, "mock.llm": llm,
    })
    ctx = _ctx(config, db, repos, gateway, clock, ids)
    make_prompt, make_request = _make_prompt_factory(home)
    persisted: list[dict] = []

    async def run():
        return await media_stages._generate_with_retries(
            ctx,
            episode_id=None,
            subject_key="shot-1",
            purpose="keyframe",
            kind="image",
            model_key="mock.image",
            make_prompt=make_prompt,
            make_request=make_request,
            base_nonce="nonce-1",
            max_paid_attempts=3,
            auto_retries=1,
            qc_run=_pass_qc,
            persist=persisted.append,
        )

    outcome = asyncio.run(run())
    assert outcome.sanitize_level == 1
    # the sanitizer was invoked exactly once, through the gateway
    assert len(llm.calls) == 1
    # the two paid submits used different prompts (never an identical resubmit)
    assert len(image.submits) == 2
    assert image.submits[0]["prompt"] != image.submits[1]["prompt"]
    assert "a lighthouse keeper faces the storm" not in image.submits[1]["prompt"]
    # the refusal log records original + rewritten prompts
    assert outcome.refusal_log
    entry = outcome.refusal_log[0]
    assert "the lighthouse keeper faces the storm" in entry["original"]
    assert entry["rewritten"] == "a figure faces the gathering dark, implied"
    # persisted payloads capture the level + log
    levels = [p.get("sanitize_level") for p in persisted]
    assert 1 in levels


def test_san_01_cap_yields_failed_policy_and_parks(
    tmp_path, monkeypatch
):
    home, config, db, clock, ids, repos = _env(
        tmp_path, monkeypatch, settings_overrides={"sanitizer": {"max_levels": 1}}
    )
    png = _png(tmp_path)
    image = _ScriptedImage(["refuse"], png)
    llm = _FakeLLM("a figure faces the gathering dark, implied")
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.image": image, "mock.llm": llm, "llm.light": llm,
    })
    ctx = _ctx(config, db, repos, gateway, clock, ids)
    make_prompt, make_request = _make_prompt_factory(home)
    persisted: list[dict] = []

    async def run():
        with pytest.raises(FailedPolicy):
            await media_stages._generate_with_retries(
                ctx,
                episode_id=None,
                subject_key="shot-2",
                purpose="keyframe",
                kind="image",
                model_key="mock.image",
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce="nonce-2",
                max_paid_attempts=5,
                auto_retries=1,
                qc_run=_pass_qc,
                persist=persisted.append,
            )

    asyncio.run(run())
    failed = [p for p in persisted if p.get("qc_status") == "failed_policy"]
    assert failed, "expected a failed_policy persist payload"
    assert failed[0]["policy_blocked"] == 1
    log = json.loads(failed[0]["refusal_log_json"])
    assert log and log[0]["rewritten"] == "a figure faces the gathering dark, implied"


def test_san_01_identical_prompt_never_resubmitted(
    tmp_path, monkeypatch
):
    home, config, db, clock, ids, repos = _env(tmp_path, monkeypatch)
    png = _png(tmp_path)
    image = _ScriptedImage(["refuse", "ok"], png)
    llm = _FakeLLM("a figure faces the gathering dark, implied")
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.image": image, "mock.llm": llm, "llm.light": llm,
    })
    ctx = _ctx(config, db, repos, gateway, clock, ids)
    make_prompt, make_request = _make_prompt_factory(home)

    async def run():
        await media_stages._generate_with_retries(
            ctx,
            episode_id=None,
            subject_key="shot-3",
            purpose="keyframe",
            kind="image",
            model_key="mock.image",
            make_prompt=make_prompt,
            make_request=make_request,
            base_nonce="nonce-3",
            max_paid_attempts=3,
            auto_retries=1,
            qc_run=_pass_qc,
            persist=None,
        )

    asyncio.run(run())
    prompts = [s["prompt"] for s in image.submits]
    assert len(prompts) == len(set(prompts)), "an identical prompt was resubmitted"


def test_san_01_crash_after_refusal_resumes_at_sanitized_prompt(
    tmp_path, monkeypatch
):
    home, config, db, clock, ids, repos = _env(tmp_path, monkeypatch)
    png = _png(tmp_path)
    image = _ScriptedImage(["ok"], png)
    llm = _FakeLLM("SHOULD-NOT-BE-CALLED")
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.image": image, "mock.llm": llm, "llm.light": llm,
    })
    ctx = _ctx(config, db, repos, gateway, clock, ids)
    make_prompt, make_request = _make_prompt_factory(home)
    resume = media_stages._PreSubmitState(
        nonce="nonce-4",
        sanitize_level=1,
        refusal_log=[{"level": 1, "original": "x", "rewritten": "a figure faces the dark"}],
        scene_override="a figure faces the dark",
    )

    async def run():
        return await media_stages._generate_with_retries(
            ctx,
            episode_id=None,
            subject_key="shot-4",
            purpose="keyframe",
            kind="image",
            model_key="mock.image",
            make_prompt=make_prompt,
            make_request=make_request,
            base_nonce="nonce-4",
            max_paid_attempts=3,
            auto_retries=1,
            qc_run=_pass_qc,
            persist=None,
            resume_state=resume,
        )

    outcome = asyncio.run(run())
    assert llm.calls == [], "resume must not call the sanitizer LLM again"
    assert len(image.submits) == 1
    assert "a figure faces the dark" in image.submits[0]["prompt"]
    assert outcome.sanitize_level == 1


# ======================================================================
# T-SAN-02 — rewrite validation falls through to the lexicon
# ======================================================================


def test_san_02_validate_rewrite_rules():
    assert validate_rewrite("a storm hits", "", max_chars=100) is False
    assert validate_rewrite("a storm hits", "   ", max_chars=100) is False
    assert validate_rewrite("a storm hits", "x" * 101, max_chars=100) is False
    # new proper nouns are rejected
    assert validate_rewrite("a storm hits", "Katrina arrives tomorrow", max_chars=100) is False
    # same proper nouns (or none) are fine
    assert validate_rewrite("Noor faces the storm", "Noor watches the squall", max_chars=100) is True
    assert validate_rewrite("a storm hits", "a squall gathers", max_chars=100) is True


def test_san_02_apply_lexicon_whole_word_case_insensitive():
    lexicon = {"storm": "squall", "blood": "rain"}
    assert apply_lexicon("The Storm rages; blood-red sky", lexicon) == \
        "The squall rages; rain-red sky"


def test_san_02_invalid_llm_rewrite_falls_through_to_lexicon(
    tmp_path, monkeypatch
):
    home, config, db, clock, ids, _repos = _env(tmp_path, monkeypatch)
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.llm": _FakeLLM("Katrina arrives with fury"),  # new proper noun → invalid
    })
    lexicon = {"storm": "squall"}

    async def run():
        return await sanitize_scene_text(
            gateway=gateway,
            model_key="mock.llm",
            scene_text="the storm hits the harbor",
            level=2,
            episode_id=None,
            stage_run_id=None,
            max_chars=200,
            lexicon=lexicon,
            home=home,
        )

    out = asyncio.run(run())
    assert out == "the squall hits the harbor"


def test_san_02_empty_llm_rewrite_falls_through_to_lexicon(
    tmp_path, monkeypatch
):
    home, config, db, clock, ids, _repos = _env(tmp_path, monkeypatch)
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.llm": _FakeLLM(""),
    })

    async def run():
        return await sanitize_scene_text(
            gateway=gateway,
            model_key="mock.llm",
            scene_text="the storm hits",
            level=2,
            episode_id=None,
            stage_run_id=None,
            max_chars=200,
            lexicon={"storm": "squall"},
            home=home,
        )

    assert asyncio.run(run()) == "the squall hits"


# ======================================================================
# T-SAN-03 — sanitizer never invoked for transient or QC failures
# ======================================================================


def test_san_03_transient_error_skips_sanitizer(tmp_path, monkeypatch):
    home, config, db, clock, ids, repos = _env(tmp_path, monkeypatch)
    png = _png(tmp_path)
    image = _ScriptedImage(["transient"], png)
    llm = _FakeLLM("unused")
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.image": image, "mock.llm": llm,
    })
    ctx = _ctx(config, db, repos, gateway, clock, ids)
    make_prompt, make_request = _make_prompt_factory(home)

    async def run():
        with pytest.raises(TransientProviderError):
            await media_stages._generate_with_retries(
                ctx,
                episode_id=None,
                subject_key="shot-5",
                purpose="keyframe",
                kind="image",
                model_key="mock.image",
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce="nonce-5",
                max_paid_attempts=3,
                auto_retries=1,
                qc_run=_pass_qc,
                persist=None,
            )

    asyncio.run(run())
    assert llm.calls == [], "sanitizer must not run for transient errors"


def test_san_03_qc_failure_skips_sanitizer(tmp_path, monkeypatch):
    home, config, db, clock, ids, repos = _env(tmp_path, monkeypatch)
    png = _png(tmp_path)
    image = _ScriptedImage(["ok"], png)
    llm = _FakeLLM("unused")
    gateway = _gateway(config, db, clock, ids, providers={
        "mock.image": image, "mock.llm": llm,
    })
    ctx = _ctx(config, db, repos, gateway, clock, ids)
    make_prompt, make_request = _make_prompt_factory(home)

    async def run():
        with pytest.raises(media_stages.VisualQcError):
            await media_stages._generate_with_retries(
                ctx,
                episode_id=None,
                subject_key="shot-6",
                purpose="keyframe",
                kind="image",
                model_key="mock.image",
                make_prompt=make_prompt,
                make_request=make_request,
                base_nonce="nonce-6",
                max_paid_attempts=3,
                auto_retries=1,
                qc_run=_fail_qc,
                persist=None,
            )

    asyncio.run(run())
    assert llm.calls == [], "sanitizer must not run for QC failures"
