"""Phase 2 unit tests: production helpers, live-smoke guards, prompt loader."""

from __future__ import annotations

import itertools
import os

import pytest
from conftest import make_home

from studio.config.loader import load_all
from studio.config.prompts import load_prompt
from studio.config.registry import RegistryError
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import Aspect, Language
from studio.domain.errors import ConfigError, StudioError
from studio.production import project_llm_cost_musd, seed_production


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"prod-{next(self._counter):06d}"


def _config(tmp_path, monkeypatch, filename="prod"):
    home = make_home(tmp_path, filename=filename)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    return load_all(home), home


def _repos(home, clock=None, ids=None):
    from studio.core.clock import FakeClock

    ids = ids or _Ids()
    clock = clock or FakeClock()
    db = Database(home / "data" / "studio.db")
    migrate(db)
    return build_repos(db, clock, ids), ids


# ---------------------------------------------------------- seed_production


def test_seed_requires_non_empty_concept(tmp_path, monkeypatch):
    config, home = _config(tmp_path, monkeypatch, "seed_a")
    repos, ids = _repos(home)
    with pytest.raises(StudioError, match="must not be empty"):
        seed_production(config, repos, concept="   ", series_id=None,
                        languages=None, aspects=None, ids=ids)


def test_seed_rejects_unknown_series(tmp_path, monkeypatch):
    config, home = _config(tmp_path, monkeypatch, "seed_b")
    repos, ids = _repos(home)
    with pytest.raises(StudioError, match="series not found"):
        seed_production(config, repos, concept="فانوس", series_id="no-such-series",
                        languages=None, aspects=None, ids=ids)


def test_seed_rejects_duplicate_aspect(tmp_path, monkeypatch):
    config, home = _config(tmp_path, monkeypatch, "seed_c")
    repos, ids = _repos(home)
    with pytest.raises(StudioError, match="duplicate aspect"):
        seed_production(config, repos, concept="فانوس", series_id=None,
                        languages=["ar"], aspects=["9:16", "9:16"], ids=ids)


def test_seed_reuses_series_and_counts_deliverables(tmp_path, monkeypatch):
    config, home = _config(tmp_path, monkeypatch, "seed_d")
    repos, ids = _repos(home)
    first = seed_production(config, repos, concept="فانوس", series_id=None,
                            languages=["ar"], aspects=["9:16"], ids=ids)
    second = seed_production(config, repos, concept="فانوس جديد",
                             series_id=first.series_id,
                             languages=["ar", "en"], aspects=["9:16", "16:9"], ids=ids)
    assert second.series_id == first.series_id
    assert second.series_created is False
    assert len(second.track_ids) == 2
    assert len(second.deliverable_ids) == 4


# -------------------------------------------------- project_llm_cost_musd


def test_projection_rows_and_monotonicity(tmp_path, monkeypatch):
    config, home = _config(tmp_path, monkeypatch, "proj")
    from studio.config.prompts import load_prompt

    director, _sha = load_prompt("director_prompt", home=home)
    resolved = config.registry.resolve_llm_subset("draft")
    rows = project_llm_cost_musd(
        config, resolved, n_tracks=2, n_deliverables=4,
        director_chars=len(director), pack_cap_chars=12000, tashkeel_retries=1,
    )
    labels = [label for label, _ in rows]
    assert labels[0].startswith("S1 bible_upsert")
    assert any(label.startswith("S2 narration_script") for label in labels)
    assert sum(1 for label in labels if label.startswith("S2 tashkeel_verify")) == 2
    assert sum(1 for label in labels if label.startswith("S3 shot_plan")) == 2
    assert sum(1 for label in labels if label.startswith("S4 metadata_safety")) == 4
    assert all(musd > 0 for _label, musd in rows)
    total = sum(musd for _label, musd in rows)
    rows_big = project_llm_cost_musd(
        config, resolved, n_tracks=1, n_deliverables=1,
        director_chars=len(director) * 2, pack_cap_chars=24000, tashkeel_retries=0,
    )
    assert rows_big[0][1] > 0
    assert total > 0


def test_projection_uses_subset_models(tmp_path, monkeypatch):
    config, _home = _config(tmp_path, monkeypatch, "proj2")
    resolved = config.registry.resolve_llm_subset("sandbox")  # mock pricing = 0
    rows = project_llm_cost_musd(
        config, resolved, n_tracks=1, n_deliverables=1,
        director_chars=100, pack_cap_chars=1000, tashkeel_retries=0,
    )
    assert all(musd == 0 for _label, musd in rows)


# ------------------------------------------------------------ live smoke


def test_live_smoke_refuses_without_flag(monkeypatch):
    for name in ("STUDIO_LIVE_LLM", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from studio.live_smoke import main, missing_env

    assert set(missing_env()) == {"STUDIO_LIVE_LLM", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    assert main() == 2


def test_live_smoke_run_smoke_refuses_and_requests_are_small():
    from studio.live_smoke import run_smoke, smoke_requests

    for name in ("STUDIO_LIVE_LLM", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(name, None)
    with pytest.raises(RuntimeError, match="STUDIO_LIVE_LLM"):
        import asyncio

        asyncio.run(run_smoke())
    reqs = smoke_requests()
    assert reqs["bible_upsert"].expect_json is True
    assert reqs["bible_upsert"].max_tokens <= 256
    assert reqs["tashkeel_verify"].temperature == pytest.approx(0.2)


# ------------------------------------------------------------ prompt loader


def test_load_prompt_roundtrip_and_sha(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path

    home = make_home(tmp_path, filename="prompt")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    text, sha = load_prompt("director_prompt", home=home)
    assert "DIRECTOR" in text
    assert sha == hashlib.sha256(
        Path(home / "configs" / "prompts" / "director_prompt.txt").read_bytes()
    ).hexdigest()


def test_load_prompt_rejects_bad_names_and_missing(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="prompt2")
    with pytest.raises(ConfigError):
        load_prompt("../secrets", home=home)
    with pytest.raises(ConfigError):
        load_prompt("does_not_exist", home=home)


def test_full_resolve_profile_draft_still_raises(tmp_path, monkeypatch):
    config, _home = _config(tmp_path, monkeypatch, "proj3")
    with pytest.raises(RegistryError):
        config.registry.resolve_profile("draft")


def test_resolve_languages_types(tmp_path, monkeypatch):
    config, _home = _config(tmp_path, monkeypatch, "proj4")
    from studio.config.languages import resolve_languages

    langs = resolve_languages(config.settings, ["ar"])
    assert langs == [Language.AR]
    aspect = Aspect("9:16")
    assert aspect.value == "9:16"
