"""Phase 5 step 2: config, migration 007, StageSpec flags, barrier, checklist.

Fully offline. The migration tests exercise the *upgrade* path (a database that
already holds Phase ≤ 4 rows), because that is where the table rebuild and the
H5.7 gate re-kind can silently destroy data.
"""

from __future__ import annotations

import itertools
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from conftest import make_home

from studio.config.loader import load_all
from studio.config.presets import (
    ExportPresets,
    load_export_presets_file,
)
from studio.core.clock import FakeClock
from studio.db import Database, migrate
from studio.db.migrate import MIGRATIONS_DIR
from studio.db.repos import build_repos
from studio.domain.enums import GateKind, Scope
from studio.domain.errors import ConfigError, GatePolicyError
from studio.domain.refs import SubjectRef
from studio.pipeline.barrier import episode_closed_problem, free_stage_gate_problems
from studio.pipeline.checklist import (
    REQUIRED_ITEMS,
    checklist_problems,
    normalize_checklist,
)
from studio.pipeline.stage import StageSpec


class _Ids:
    _counter = itertools.count(1)

    def new(self) -> str:
        return f"p5-{next(self._counter):06d}"


def _config(tmp_path, monkeypatch, **overrides):
    home = make_home(tmp_path, settings_overrides=overrides or None)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    return home, load_all(home)


def _repos(tmp_path):
    home = make_home(tmp_path, filename="repos")
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    return db, build_repos(db, clock, _Ids())


# ---------------------------------------------------------------------------
# settings (§F)
# ---------------------------------------------------------------------------


def test_phase5_settings_sections_load(tmp_path, monkeypatch):
    _home, config = _config(tmp_path, monkeypatch)
    settings = config.settings
    assert settings.encode.encoder == "auto"
    assert settings.encode.nvenc.cq == 19
    assert settings.encode.probe.cache_file.endswith("encoder_probe.json")
    assert settings.assemble.segment_container == "mkv"
    assert settings.assemble.max_inputs_per_graph == 24
    assert settings.assemble.av_sync.fail_ms > settings.assemble.av_sync.warn_ms
    assert settings.animatic.short_side_px == 540
    assert settings.qc.render.video_scan is True
    assert settings.export.root == "exports"
    assert settings.export.draft_root == "exports/_draft"
    assert settings.bible.proposals.model_role == "creative"
    assert settings.retention.rejected_attempt_days == 7


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"assemble": {"av_sync": {"warn_ms": 100, "fail_ms": 100}}}, "av_sync.fail_ms"),
        ({"qc": {"render": {"true_peak_warn_db": -0.5, "true_peak_fail_db": -1.0}}}, "true_peak"),
        ({"export": {"root": "exports", "draft_root": "exports"}}, "draft_root"),
        ({"encode": {"encoder": "x265"}}, "encode.encoder"),
        ({"assemble": {"segment_container": "mp4"}}, "segment_container"),
    ],
)
def test_phase5_settings_validation(tmp_path, monkeypatch, overrides, fragment):
    with pytest.raises(ConfigError) as excinfo:
        _config(tmp_path, monkeypatch, **overrides)
    assert fragment in str(excinfo.value)


# ---------------------------------------------------------------------------
# export presets (§F / T-EXP-04)
# ---------------------------------------------------------------------------


def _write_presets(home: Path, mutate) -> Path:
    path = home / "configs" / "export_presets.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_export_presets_cover_every_channel(tmp_path, monkeypatch):
    _home, config = _config(tmp_path, monkeypatch)
    assert set(config.presets.channels) == set(config.channels.channels)
    preset = config.presets.for_channel("short_ar_tall")
    assert preset.aspect.value == "9:16"
    assert (preset.width, preset.height) == (1080, 1920)
    assert preset.limits.max_duration_s == 180


def test_export_presets_missing_channel_is_refused(tmp_path, monkeypatch):
    home, config = _config(tmp_path, monkeypatch)
    _write_presets(home, lambda d: d["channels"].pop("yt_en_wide"))
    with pytest.raises(ConfigError) as excinfo:
        load_export_presets_file(
            home / "configs" / "export_presets.yaml", channels=config.channels
        )
    assert "missing presets" in str(excinfo.value)


def test_export_presets_extra_channel_is_refused(tmp_path, monkeypatch):
    home, config = _config(tmp_path, monkeypatch)
    _write_presets(home, lambda d: d["channels"].update({"ghost": d["channels"]["short_en_tall"]}))
    with pytest.raises(ConfigError) as excinfo:
        load_export_presets_file(
            home / "configs" / "export_presets.yaml", channels=config.channels
        )
    assert "without a channel" in str(excinfo.value)


def test_export_presets_aspect_must_match_channel(tmp_path, monkeypatch):
    home, config = _config(tmp_path, monkeypatch)

    def mutate(data):
        data["channels"]["short_ar_tall"]["aspect"] = "16:9"

    _write_presets(home, mutate)
    with pytest.raises(ConfigError) as excinfo:
        load_export_presets_file(
            home / "configs" / "export_presets.yaml", channels=config.channels
        )
    assert "does not match channel aspect" in str(excinfo.value)


def test_export_presets_empty_and_bad_yaml(tmp_path, monkeypatch):
    home, config = _config(tmp_path, monkeypatch)
    path = home / "configs" / "export_presets.yaml"
    path.write_text("channels: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_export_presets_file(path, channels=config.channels)
    assert "non-empty" in str(excinfo.value)
    path.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_export_presets_file(path, channels=config.channels)
    path.write_text("channels: [\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_export_presets_file(path, channels=config.channels)
    missing = home / "configs" / "nope.yaml"
    with pytest.raises(ConfigError):
        load_export_presets_file(missing, channels=config.channels)


def test_export_presets_for_unknown_channel_raises():
    presets = ExportPresets(channels={})
    with pytest.raises(ConfigError):
        presets.for_channel("nope")


# ---------------------------------------------------------------------------
# migration 007 (upgrade path + H5.7 re-kind)
# ---------------------------------------------------------------------------


def _legacy_db(tmp_path) -> tuple[Database, Path]:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("00[1-6]_*.sql")):
        shutil.copy(path, legacy / path.name)
    db = Database(tmp_path / "legacy.db")
    assert migrate(db, migrations_dir=legacy) == [1, 2, 3, 4, 5, 6]
    return db, legacy


def _seed(db: Database) -> None:
    conn = db.connect()
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO series(id, name, slug, created_at) VALUES ('s1', 'S', 's', ?)", (ts,)
    )
    conn.execute(
        "INSERT INTO episodes(id, series_id, title, concept, status, languages_json,"
        " aspects_json, created_at) VALUES ('e1', 's1', 'T', 'c', 'completed', ?, ?, ?)",
        (json.dumps(["ar"]), json.dumps(["9:16"]), ts),
    )
    conn.execute(
        "INSERT INTO pipeline_runs(id, episode_id, profile, status, config_snapshot_json,"
        " created_at, updated_at) VALUES ('r1', 'e1', 'draft', 'completed', '{}', ?, ?)",
        (ts, ts),
    )
    for gate_id, gate, stage in (
        ("g-metadata", "G3_FINAL_CUT", "metadata_safety"),
        ("g-render", "G3_FINAL_CUT", "render_final"),
        ("g-script", "G1_SCRIPT", "s3_script"),
    ):
        conn.execute(
            "INSERT INTO approval_gates(id, run_id, gate, stage, scope, subject_id, round,"
            " status, snapshot_hash, note, created_at, decided_at) VALUES"
            " (?, 'r1', ?, ?, NULL, NULL, 1, 'approved', NULL, NULL, ?, NULL)",
            (gate_id, gate, stage, ts),
        )
    conn.commit()


def test_migration_007_upgrades_legacy_rows(tmp_path):
    db, _legacy = _legacy_db(tmp_path)
    _seed(db)
    assert migrate(db) == [7]
    conn = db.connect()
    # H5.7: the metadata round is re-kinded, the render round is not.
    gates = {row["id"]: (row["gate"], row["stage"]) for row in conn.execute(
        "SELECT id, gate, stage FROM approval_gates"
    )}
    assert gates["g-metadata"] == ("G1_SCRIPT", "metadata_safety")
    assert gates["g-render"] == ("G3_FINAL_CUT", "render_final")
    assert gates["g-script"] == ("G1_SCRIPT", "s3_script")
    # The rebuild must not cascade-wipe children (FKs are off for this file).
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    # New columns exist and are NULL for legacy rows.
    row = conn.execute("SELECT source_ref, source_license, published_at FROM episodes").fetchone()
    assert tuple(row) == (None, None, None)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'episodes_new'"
    ).fetchall()
    # Idempotent.
    assert migrate(db) == []


def test_episodes_status_accepts_published_and_archived(tmp_path):
    db, _legacy = _legacy_db(tmp_path)
    _seed(db)
    migrate(db)
    conn = db.connect()
    for status in ("published", "archived"):
        conn.execute("UPDATE episodes SET status = ? WHERE id = 'e1'", (status,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE episodes SET status = 'nonsense' WHERE id = 'e1'")


def test_gate_checklist_and_presented_roundtrip(tmp_path):
    _db, repos = _repos(tmp_path)
    series = repos.content.create_series(name="S", slug="s")
    episode = repos.content.create_episode(
        series_id=series.id,
        title="T",
        concept="c",
        languages_json='["ar"]',
        aspects_json='["9:16"]',
    )
    assert repos.content.get_episode(episode.id) is not None
    run = repos.pipeline.create_run(
        episode_id=episode.id, profile="draft", auto_approve=False, config_snapshot_json="{}"
    )
    presented = json.dumps({"verdict": "ok"})
    gate = repos.gates.create(
        run_id=run.id,
        gate=GateKind.G3_FINAL_CUT.value,
        stage="s12_render_qc",
        scope=None,
        subject_id=None,
        round=1,
        snapshot_hash="abc",
        presented_json=presented,
    )
    assert gate.presented_json == presented
    assert gate.checklist_json is None
    checklist = json.dumps({item: True for item in REQUIRED_ITEMS})
    assert repos.gates.get(gate.id).checklist_json is None  # type: ignore[union-attr]
    decided = repos.gates.decide(gate.id, status="approved", note=None, checklist_json=checklist)
    assert decided is not None and decided.checklist_json == checklist


# ---------------------------------------------------------------------------
# gate checklist (§J)
# ---------------------------------------------------------------------------


def test_checklist_final_requires_every_item():
    with pytest.raises(GatePolicyError) as excinfo:
        normalize_checklist({"reviewed_full_render": True}, profile="final")
    assert "missing" in str(excinfo.value)


def test_checklist_final_requires_review():
    full = {item: True for item in REQUIRED_ITEMS}
    full["reviewed_full_render"] = False
    with pytest.raises(GatePolicyError):
        normalize_checklist(full, profile="final")
    full["reviewed_full_render"] = True
    assert normalize_checklist(full, profile="final")["ai_disclosure"] is True


def test_checklist_final_rejects_auto_values():
    full = {item: True for item in REQUIRED_ITEMS}
    full["licenses_confirmed"] = "auto"
    with pytest.raises(GatePolicyError):
        normalize_checklist(full, profile="final")


def test_checklist_unknown_key_and_draft_fill():
    with pytest.raises(GatePolicyError):
        normalize_checklist({"reviewd_full_render": True}, profile="draft")
    draft = normalize_checklist({"reviewed_full_render": True}, profile="draft")
    assert draft["reviewed_full_render"] is True
    assert draft["licenses_confirmed"] == "auto"
    assert draft["ai_disclosure"] is True


def test_checklist_auto_markers():
    auto = normalize_checklist(None, profile="draft", auto=True)
    assert auto == {item: "auto" for item in REQUIRED_ITEMS}


def test_checklist_problems_reports_gaps():
    assert checklist_problems(None) == ["no checklist recorded on the G3 round"]
    problems = checklist_problems({"reviewed_full_render": True, "ai_disclosure": "auto"})
    assert any("licenses_confirmed: missing" in p for p in problems)
    assert any("auto-filled" in p for p in problems)
    blank = checklist_problems({item: True for item in REQUIRED_ITEMS})
    assert blank == []


# ---------------------------------------------------------------------------
# StageSpec flags + barrier (§S15 / H5.7)
# ---------------------------------------------------------------------------


def _spec(**kwargs) -> StageSpec:
    from studio.media.stages import ClipPreflightHandler

    base = dict(
        name="x",
        scope=Scope.EPISODE,
        depends_on=(),
        handler=ClipPreflightHandler(),
        gate_after=None,
    )
    base.update(kwargs)
    return StageSpec(**base)  # type: ignore[arg-type]


def test_stage_spec_phase5_flags_default_off():
    spec = _spec()
    assert spec.requires_cleared_gates is False
    assert spec.gate_reject_stage is None
    flagged = _spec(requires_cleared_gates=True, gate_reject_stage="s8_keyframes")
    assert flagged.requires_cleared_gates is True
    assert flagged.gate_reject_stage == "s8_keyframes"


def test_episode_closed_problem(tmp_path):
    _db, repos = _repos(tmp_path)
    series = repos.content.create_series(name="S", slug="s")
    episode = repos.content.create_episode(
        series_id=series.id,
        title="T",
        concept="c",
        languages_json='["ar"]',
        aspects_json='["9:16"]',
    )
    run = repos.pipeline.create_run(
        episode_id=episode.id, profile="draft", auto_approve=False, config_snapshot_json="{}"
    )
    assert episode_closed_problem(repos, run) is None
    for status in ("published", "archived"):
        repos.content.set_episode_status(episode.id, status)
        problem = episode_closed_problem(repos, run)
        assert problem is not None and status in problem


def test_free_stage_gate_problems_uses_ancestors(tmp_path):
    """A free stage with `requires_cleared_gates` is blocked exactly like a paid one."""
    from studio.pipeline.dag import Dag
    from studio.pipeline.specs import production_specs

    _db, repos = _repos(tmp_path)
    dag = Dag(production_specs())
    series = repos.content.create_series(name="S", slug="s")
    episode = repos.content.create_episode(
        series_id=series.id,
        title="T",
        concept="c",
        languages_json='["ar"]',
        aspects_json='["9:16"]',
    )
    run = repos.pipeline.create_run(
        episode_id=episode.id, profile="draft", auto_approve=False, config_snapshot_json="{}"
    )
    # No succeeded ancestor runs: nothing to approve yet.
    assert free_stage_gate_problems(
        repos=repos, dag=dag, run_id=run.id, stage_id="s10p_clip_preflight"
    ) == []
    # A succeeded gated ancestor without any gate round is a problem. Since
    # Phase 5 the gated stage before S10p is S9 (the animatic), not S8.
    from studio.domain.enums import Scope

    repos.pipeline.create_stage_run(
        run_id=run.id,
        stage="s9_animatic",
        subject=SubjectRef(scope=Scope.DELIVERABLE, id="dlv-1"),
        status="succeeded",
    )
    problems = free_stage_gate_problems(
        repos=repos, dag=dag, run_id=run.id, stage_id="s10p_clip_preflight"
    )
    assert problems and "no gate round exists" in problems[0]
