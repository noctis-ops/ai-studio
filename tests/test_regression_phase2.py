"""T-REGR-02 — Phase 1 registry contracts hold after Phase 2.

T-REGR-01 (145/145 + coverage ≥ 90% + ruff + mypy) is enforced by CI and
reported in PHASE2_REPORT.md, not re-implemented here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_home

from studio.config.loader import load_all
from studio.config.registry import RegistryError

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_draft_still_refuses_without_verifying_media_models(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="regr02")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)  # shipped configs, zero overrides
    with pytest.raises(RegistryError, match="placeholder"):
        config.registry.resolve_profile("draft")


def test_sandbox_resolves_fully_and_subset_resolves_draft(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="regr_sb")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    full = config.registry.resolve_profile("sandbox")
    assert set(full.model_keys)  # demo roles all resolve (mock.*)
    subset = config.registry.resolve_llm_subset("draft")
    assert subset.model_keys["llm_creative"] == "llm.light"
    assert subset.model_keys["llm_light"] == "llm.light"


def test_shipped_registry_llm_prices_untouched(tmp_path, monkeypatch):
    home = make_home(tmp_path, filename="regr_prices")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    creative = config.registry.models["llm.creative"]
    assert float(creative.pricing.in_usd) == 3.0
    assert float(creative.pricing.out_usd) == 15.0
    light = config.registry.models["llm.light"]
    assert float(light.pricing.in_usd) == 0.15
    assert float(light.pricing.out_usd) == 0.6


def test_begin_prompt_untouched():
    import shutil
    import subprocess

    git = shutil.which("git")
    assert git, "git must be on PATH for this check"
    out = subprocess.run(  # noqa: S603 - argv is fixed; no untrusted input
        [git, "diff", "--name-only", "main", "--", "BEGIN_PROMPT.md"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    assert out.stdout.strip() == ""
