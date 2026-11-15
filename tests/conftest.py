"""Shared fixtures: a throwaway STUDIO_HOME per test, a migrated DB,
a FakeClock, deterministic IDs, and a gateway wired to the mock providers."""

from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_SRC = REPO_ROOT / "configs"

# T-LIVE-01: the live smoke test is opt-in; when STUDIO_LIVE_LLM != '1' it is
# not collected at all (not merely skipped) — it must never run in CI.
collect_ignore = []
if os.environ.get("STUDIO_LIVE_LLM") != "1":
    collect_ignore.append("test_live_smoke.py")

FAKE_SECRET = "falkey-0123456789abcdef0123456789abcdef"


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def make_home(
    tmp_path: Path,
    *,
    settings_overrides: dict | None = None,
    registry_overrides: dict | None = None,
    channels_overrides: dict | None = None,
    filename: str = "home",
) -> Path:
    home = tmp_path / filename
    shutil.copytree(CONFIGS_SRC, home / "configs")
    if settings_overrides:
        path = home / "configs" / "settings.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        _deep_merge(data, settings_overrides)
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if registry_overrides:
        path = home / "configs" / "model_registry.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        _deep_merge(data, registry_overrides)
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if channels_overrides:
        path = home / "configs" / "channel_profiles.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        _deep_merge(data, channels_overrides)
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return home


# Mock prices that make budget math non-degenerate. Zero is otherwise a
# legitimate price for the sandbox profile (it exists to be free).
PAID_MOCK_PRICES = {
    "models": {
        "mock.llm": {"pricing": {"unit": "per_1m_tokens", "in_usd": 2.0, "out_usd": 4.0}},
        "mock.tts": {"pricing": {"unit": "per_1k_chars", "usd": 0.25}},
        "mock.image": {"pricing": {"unit": "per_image", "usd": 1.0}},
        "mock.video": {"pricing": {"unit": "per_second", "usd": 2.0}},
    }
}


@pytest.fixture
def studio_home(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    return home


@pytest.fixture
def paid_home(tmp_path, monkeypatch):
    home = make_home(tmp_path, registry_overrides=PAID_MOCK_PRICES, filename="paid_home")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    return home


@pytest.fixture
def config(studio_home):
    from studio.config.loader import load_all

    return load_all(studio_home)


@pytest.fixture
def clock():
    from studio.core.clock import FakeClock

    return FakeClock()


@pytest.fixture
def ids():
    from studio.core.ids import SequentialIds

    return SequentialIds("test")


@pytest.fixture
def db(studio_home):
    from studio.db import Database, migrate

    database = Database(studio_home / "data" / "studio.db")
    migrate(database)
    yield database


@pytest.fixture
def repos(db, clock, ids):
    from studio.db import build_repos

    return build_repos(db, clock, ids)


@pytest.fixture
def ledger(db, clock, config, ids):
    from studio.core.budget import LedgerService

    return LedgerService(db, clock, config.settings, ids)


@pytest.fixture
def cas(db, clock, config):
    from pathlib import Path as _Path

    from studio.core.cas import ContentStore

    return ContentStore(_Path(config.settings.paths.cas), db, clock)


def build_gateway(*, db, config, clock, ids, cas=None, ledger=None, faults=None,
                  providers=None, seed=7):
    from pathlib import Path as _Path

    from studio.core.budget import LedgerService
    from studio.core.cas import ContentStore
    from studio.core.clock import no_op_sleep
    from studio.core.gateway import ProviderGateway

    cas_store = cas or ContentStore(_Path(config.settings.paths.cas), db, clock)
    ledger_service = ledger or LedgerService(db, clock, config.settings, ids)
    return ProviderGateway(
        db=db,
        settings=config.settings,
        registry=config.registry,
        cas=cas_store,
        ledger=ledger_service,
        clock=clock,
        ids=ids,
        faults=faults,
        providers=providers,
        poll_interval_s=0.0,
        sleep=no_op_sleep,
        rng=random.Random(seed),
    )


@pytest.fixture
def gateway(db, config, clock, ids, cas, ledger):
    return build_gateway(db=db, config=config, clock=clock, ids=ids, cas=cas, ledger=ledger)


def pytest_collection_modifyitems(items):
    """Auto-skip ffmpeg-marked tests when ffmpeg/ffprobe are not on PATH."""
    import shutil

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    skip = __import__("pytest").mark.skip(reason="ffmpeg/ffprobe not on PATH")
    for item in items:
        if "ffmpeg" in item.keywords:
            item.add_marker(skip)
