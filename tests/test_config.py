"""T-CFG-* and T-LANG-* (BEGIN_PROMPT section I)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml
from conftest import CONFIGS_SRC, make_home

from studio.config.channels import channel_for, load_channels_file
from studio.config.loader import load_all
from studio.config.settings import load_settings_file
from studio.domain.enums import Aspect, Language, Profile
from studio.domain.errors import CommercialUseNotVerified, ConfigError, RegistryError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_yaml(path: Path, data: object) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --------------------------------------------------------------- T-CFG-01


def test_studio_home_interpolation_and_errors(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    cfg = load_all(home)
    assert cfg.settings.paths.home == str(home.resolve())
    assert cfg.settings.paths.db == str((home / "data" / "studio.db").resolve())

    # unset variable referenced as ${VAR} raises ConfigError
    bad = _write_yaml(
        tmp_path / "settings_bad.yaml",
        yaml.safe_load((home / "configs" / "settings.yaml").read_text(encoding="utf-8"))
        | {"paths": {"home": "${STUDIO_HOME_UNSET_VAR}"}},
    )
    with pytest.raises(ConfigError, match="STUDIO_HOME_UNSET_VAR"):
        load_settings_file(bad)

    # unknown key is rejected
    data = yaml.safe_load((home / "configs" / "settings.yaml").read_text(encoding="utf-8"))
    data["bogus_unknown_key"] = 1
    with pytest.raises(ConfigError):
        load_settings_file(_write_yaml(tmp_path / "settings_extra.yaml", data))


# --------------------------------------------------------------- T-CFG-02


@pytest.mark.parametrize(
    ("languages", "ok"),
    [(["ar"], True), (["ar", "en"], True), (["en", "ar"], True),
     ([], False), (["fr"], False), (["ar", "ar"], False), (["ar", "en", "fr"], False)],
)
def test_languages_validation(tmp_path, monkeypatch, languages, ok):
    monkeypatch.setenv("STUDIO_HOME", str(tmp_path))
    home = make_home(tmp_path / "h", settings_overrides={"languages": languages})
    monkeypatch.setenv("STUDIO_HOME", str(home))
    if ok:
        assert load_all(home).settings.languages == [str(x) for x in languages]
    else:
        with pytest.raises(ConfigError):
            load_all(home)


@pytest.mark.parametrize(
    ("aspects", "ok"),
    [(["9:16"], True), (["9:16", "16:9"], True), (["16:9"], True),
     ([], False), (["4:3"], False), (["9:16", "9:16"], False)],
)
def test_aspects_validation(tmp_path, monkeypatch, aspects, ok):
    home = make_home(tmp_path / "h", settings_overrides={"aspects": aspects})
    monkeypatch.setenv("STUDIO_HOME", str(home))
    if ok:
        assert load_all(home).settings.aspects == [str(x) for x in aspects]
    else:
        with pytest.raises(ConfigError):
            load_all(home)


# --------------------------------------------------------------- T-CFG-03


def test_profile_with_missing_model_key_raises(tmp_path, monkeypatch):
    home = make_home(tmp_path / "h", registry_overrides={"profiles": {"sandbox": {"tts": "nope.missing"}}})
    monkeypatch.setenv("STUDIO_HOME", str(home))
    with pytest.raises(RegistryError):
        load_all(home)


def test_profile_with_wrong_capability_raises(tmp_path, monkeypatch):
    home = make_home(tmp_path / "h", registry_overrides={"profiles": {"sandbox": {"video": "mock.llm"}}})
    monkeypatch.setenv("STUDIO_HOME", str(home))
    with pytest.raises(RegistryError, match="capability"):
        load_all(home)


# --------------------------------------------------------------- T-CFG-04


_REAL_PRICES = {
    "models": {
        "llm.light": {"pricing": {"unit": "per_1m_tokens", "in_usd": 0.15, "out_usd": 0.6}},
        "llm.creative": {"pricing": {"unit": "per_1m_tokens", "in_usd": 3.0, "out_usd": 15.0}},
        "tts.main": {
            "model_id": "real-tts-model",
            "pricing": {"unit": "per_1k_chars", "usd": 0.3},
            "pricing_verified_on": "2026-11-01",
            "commercial_use_verified": True,
            "needs_confirmation": False,
        },
        "image.keyframe": {"pricing": {"unit": "per_image", "usd": 0.02}},
        "video.i2v.draft": {"pricing": {"unit": "per_second", "usd": 0.1}},
        "video.i2v.final": {"pricing": {"unit": "per_second", "usd": 0.6}},
    }
}


def test_draft_profile_refused_on_shipped_registry(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    cfg = load_all(home)
    with pytest.raises(RegistryError):
        cfg.registry.resolve_profile(Profile.DRAFT.value)
    assert cfg.registry.resolve_profile(Profile.SANDBOX.value).name == "sandbox"


def test_draft_profile_resolves_with_real_prices(tmp_path, monkeypatch):
    home = make_home(tmp_path / "h", registry_overrides=_REAL_PRICES)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    cfg = load_all(home)
    resolved = cfg.registry.resolve_profile(Profile.DRAFT.value)
    assert resolved.name == "draft"
    assert resolved.model_keys["video"] == "video.i2v.draft"


# --------------------------------------------------------------- T-CFG-05


def test_final_profile_export_guard(tmp_path, monkeypatch, repos):
    # real prices everywhere except commercial-use verification on image.keyframe
    overrides = yaml.safe_load(yaml.safe_dump(_REAL_PRICES))
    overrides["models"]["image.keyframe"]["commercial_use_verified"] = False
    overrides["models"]["video.i2v.final"]["commercial_use_verified"] = False
    home = make_home(tmp_path / "h", registry_overrides=overrides)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    cfg = load_all(home)
    resolved = cfg.registry.resolve_profile(Profile.FINAL.value)

    with pytest.raises(CommercialUseNotVerified):
        cfg.registry.assert_exportable(resolved, audit=repos.audit)

    cfg.registry.assert_exportable(resolved, override=True, audit=repos.audit)
    assert repos.audit.has_event("commercial_use_override", "profile:final")

    # all-verified registry passes with no override and no extra audit row
    overrides["models"]["image.keyframe"]["commercial_use_verified"] = True
    overrides["models"]["video.i2v.final"]["commercial_use_verified"] = True
    home2 = make_home(tmp_path / "h2", registry_overrides=overrides)
    cfg2 = load_all(home2)
    resolved2 = cfg2.registry.resolve_profile(Profile.FINAL.value)
    cfg2.registry.assert_exportable(resolved2, audit=repos.audit)


# --------------------------------------------------------------- T-CFG-06


def test_registry_snapshot_deterministic_and_persisted(tmp_path, monkeypatch, db, repos):
    home = make_home(tmp_path / "h")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    cfg = load_all(home)
    resolved = cfg.registry.resolve_profile(Profile.SANDBOX.value)
    snap1 = cfg.registry.registry_snapshot(resolved)
    snap2 = cfg.registry.registry_snapshot(resolved)
    assert snap1 == snap2
    assert set(snap1["models"]) == {"llm_creative", "llm_light", "tts", "keyframe", "video"}

    conn = db.connect()
    now = "2026-01-01T00:00:00.000Z"
    conn.execute(
        "INSERT INTO series(id, name, slug, created_at) VALUES ('s1', 's', 's-1', ?)", (now,)
    )
    conn.execute(
        "INSERT INTO episodes(id, series_id, title, concept, status, languages_json, "
        "aspects_json, created_at) VALUES ('e1', 's1', 't', 'c', 'pending', '[\"ar\"]', '[\"9:16\"]', ?)",
        (now,),
    )
    snapshot_json = json.dumps(snap1, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    run_row = repos.pipeline.create_run(
        episode_id="e1",
        profile=Profile.SANDBOX.value,
        auto_approve=False,
        config_snapshot_json=snapshot_json,
    )
    row = db.connect().execute(
        "SELECT config_snapshot_json FROM pipeline_runs WHERE id = ?", (run_row.id,)
    ).fetchone()
    persisted = json.loads(row["config_snapshot_json"])
    assert persisted == json.loads(snapshot_json)
    assert persisted["profile"] == "sandbox"


# --------------------------------------------------------------- T-LANG-01

_ALLOWED_PREFIXES = ("config/", "language/")
_ALLOWED_FILES = {"domain/enums.py"}


# Phase 4 packages: the lint must keep covering them (T-LANG-01).
_PHASE4_PACKAGES = ("media", "qc", "video")


def test_no_hardcoded_language_codes_outside_allowlist():
    root = REPO_ROOT / "src" / "studio"
    offenders: list[tuple[str, int, str]] = []
    scanned: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _ALLOWED_FILES or any(rel.startswith(p) for p in _ALLOWED_PREFIXES):
            continue
        scanned.append(rel)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in ("ar", "en"):
                offenders.append((rel, node.lineno, node.value))
    # the lint really did scan the Phase 4 packages, not just the text stack
    for package in _PHASE4_PACKAGES:
        assert any(rel.startswith(f"{package}/") for rel in scanned), (
            f"T-LANG-01 lint did not scan src/studio/{package}/"
        )
    assert offenders == [], f"hardcoded language codes outside allow-list: {offenders}"


# --------------------------------------------------------------- T-LANG-02


@pytest.mark.parametrize(
    ("language", "aspect", "channel"),
    [
        (Language.AR, Aspect.WIDE, "yt_ar_wide"),
        (Language.EN, Aspect.WIDE, "yt_en_wide"),
        (Language.AR, Aspect.TALL, "short_ar_tall"),
        (Language.EN, Aspect.TALL, "short_en_tall"),
    ],
)
def test_channel_for_configured_pairs(config, language, aspect, channel):
    assert channel_for(language, aspect, config.channels) == channel


def test_channel_for_unknown_pair_raises(tmp_path):
    channels_path = _write_yaml(
        tmp_path / "channels.yaml",
        {"channels": {"yt_ar_wide": {"language": "ar", "aspect": "16:9", "platform": "youtube"}}},
    )
    channels = load_channels_file(channels_path)
    with pytest.raises(ConfigError, match="no channel configured"):
        channel_for(Language.EN, Aspect.TALL, channels)


def test_shipped_channel_profiles_cover_ar_en_wide_tall():
    data = yaml.safe_load((CONFIGS_SRC / "channel_profiles.yaml").read_text(encoding="utf-8"))
    pairs = {(c["language"], c["aspect"]) for c in data["channels"].values()}
    assert pairs == {("ar", "16:9"), ("en", "16:9"), ("ar", "9:16"), ("en", "9:16")}
