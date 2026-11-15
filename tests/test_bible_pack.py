"""T-PACK-* (PHASE2_IMPLEMENTATION_PROMPT §G) — deterministic context packer."""

from __future__ import annotations

import json
import math
from pathlib import Path

from conftest import make_home

from studio.config.loader import load_all
from studio.core.clock import FakeClock
from studio.core.ids import SequentialIds
from studio.db import Database, migrate
from studio.db.repos import build_repos
from studio.domain.enums import Scope
from studio.domain.refs import SubjectRef
from studio.domain.series_bible import BibleContent, CharacterBible, SeriesBible
from studio.pipeline.bible_pack import (
    DeliverableSummary,
    EpisodeSummary,
    pack_context,
)
from studio.pipeline.context import RunContext
from studio.production import seed_production

REPO_ROOT = Path(__file__).resolve().parents[1]


def _character(name: str, tokens: list[str] | None = None) -> CharacterBible:
    return CharacterBible(
        name=name,
        description=f"وصفٌ للشخصية {name}: هادئٌ وصبورٌ ويراقب",
        visual_tokens=tokens or ["معطفٌ كهرماني"],
        arc="قوسُها",
    )


def _bible(*, series_id: str = "s-1", version: int = 1,
           premise: str = "حارسُ فانوسٍ يتعلمُ أنَّ الضوءَ يُقتسم",
           world: str = "ميناءٌ صخريٌّ", characters=("نُور", "العاصفة")) -> SeriesBible:
    content = BibleContent(
        title="فانوسُ الفجرِ",
        premise=premise,
        world=world,
        characters=[_character(name) for name in characters],
        style_tokens=["دفءٌ سينمائي", "حبيباتُ فيلم"],
        tone=["حنون"],
        continuity_notes="نُور لا يغادرُ الميناء",
    )
    return SeriesBible.from_content(series_id, version, content)


def _episode() -> EpisodeSummary:
    return EpisodeSummary(
        episode_id="ep-1",
        series_id="s-1",
        title="الحلقةُ الأولى",
        concept="حَارِسُ فانوسٍ يتعلمُ أنَّ الضوءَ يُقتسمُ",
        languages=["ar"],
        aspects=["9:16"],
    )


def _prior(**over) -> list[DeliverableSummary]:
    base = [
        DeliverableSummary(
            deliverable_id="d-late", track_id="t-1", language="ar", channel_id="c",
            created_at="2026-11-05T00:00:00.000Z", title="متأخر", summary="متأخر"
        ),
        DeliverableSummary(
            deliverable_id="d-early", track_id="t-1", language="ar", channel_id="c",
            created_at="2026-11-01T00:00:00.000Z", title="مبكر", summary="مبكر"
        ),
        DeliverableSummary(
            deliverable_id="d-mid", track_id="t-1", language="ar", channel_id="c",
            created_at="2026-11-03T00:00:00.000Z", title="وسط", summary="وسط"
        ),
    ]
    return base


# --------------------------------------------------------------- T-PACK-01


def test_same_inputs_identical_sha():
    first = pack_context(bible=_bible(), episode=_episode(),
                         prior_deliverables=_prior(), stage_input="اكتب الأغنية")
    second = pack_context(bible=_bible(), episode=_episode(),
                          prior_deliverables=_prior(), stage_input="اكتب الأغنية")
    assert first.pack_sha256 == second.pack_sha256
    assert first.text == second.text


# --------------------------------------------------------------- T-PACK-02


def test_oversize_bible_truncated_floor_survives():
    big_world = "عالمٌ ضائِعٌ «" * 4000 + "»"
    bible = _bible(world=big_world)
    pack = pack_context(bible=bible, episode=_episode(),
                        prior_deliverables=[], stage_input="اكتب الأسطر",
                        token_budget=500)
    assert "…[truncated" in pack.text
    # hard floor: title + premise + every character name survive
    assert "فانوسُ الفجرِ" in pack.text
    assert "حارسُ فانوسٍ يتعلمُ أنَّ الضوءَ يُقتسم" in pack.text
    for character in ("نُور", "العاصفة"):
        assert f"- {character}: " in pack.text
    assert "style_tokens:" in pack.text
    assert "SERIES_BIBLE" in pack.truncated_sections


# --------------------------------------------------------------- T-PACK-03


def test_budget_uses_half_token_per_char_and_hard_cap():
    big_world = "ض" * 6000
    pack = pack_context(bible=_bible(world=big_world), episode=_episode(),
                        prior_deliverables=_prior() + _prior(),
                        stage_input="ح" * 2000,
                        token_budget=500)
    assert math.ceil(pack.n_chars * 0.5) <= 500
    assert pack.n_chars == len(pack.text)


# --------------------------------------------------------------- T-PACK-04


def test_sections_fixed_order_and_known():
    pack = pack_context(bible=_bible(), episode=_episode(),
                        prior_deliverables=_prior(), stage_input="اكتب")
    positions = []
    for header in ("# SERIES_BIBLE", "# EPISODE", "# PRIOR_WORK", "# STAGE_INPUT"):
        index = pack.text.find(header)
        assert index != -1, f"missing {header}"
        positions.append(index)
    assert positions == sorted(positions)
    for line in pack.text.splitlines():
        if line.startswith("# "):
            assert line in ("# SERIES_BIBLE", "# EPISODE", "# PRIOR_WORK", "# STAGE_INPUT")


def test_no_bible_no_bible_section():
    pack = pack_context(bible=None, episode=_episode(),
                        prior_deliverables=[], stage_input="اكتب")
    assert "# SERIES_BIBLE" not in pack.text
    assert "# EPISODE" in pack.text


# --------------------------------------------------------------- T-PACK-05


def test_prior_work_sorted_and_omitted_when_empty():
    pack = pack_context(bible=_bible(), episode=_episode(),
                        prior_deliverables=list(reversed(_prior())), stage_input="اكتب")
    index = pack.text.index("# PRIOR_WORK")
    section = pack.text[index:]
    assert section.index("d-early") < section.index("d-mid") < section.index("d-late")
    empty = pack_context(bible=_bible(), episode=_episode(),
                         prior_deliverables=[], stage_input="اكتب")
    assert "# PRIOR_WORK" not in empty.text


# --------------------------------------------------------------- T-PACK-06


def test_pack_sha_feeds_stage_input_hash(tmp_path, monkeypatch):
    """Changing the packed context changes the S2 input_hash; pack feeds the DAG key."""
    import hashlib

    from studio.pipeline.stages_text import NarrationScriptHandler

    home = make_home(tmp_path, filename="pack06")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = SequentialIds("pack")
    repos = build_repos(db, clock, ids)

    seeded = seed_production(config, repos, concept="فانوسُ الفجر", series_id=None,
                             languages=["ar"], aspects=["9:16"], ids=ids)
    run_row = repos.pipeline.create_run(
        episode_id=seeded.episode_id, profile="sandbox",
        auto_approve=False, config_snapshot_json=None,
    )
    resolved = config.registry.resolve_llm_subset("sandbox")

    from studio.core.budget import LedgerService
    from studio.core.cas import ContentStore
    from studio.core.gateway import CallContext, ProviderGateway  # noqa: F401

    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    ledger = LedgerService(db, clock, config.settings, ids)
    gateway = ProviderGateway(
        db=db, settings=config.settings, registry=config.registry,
        cas=cas, ledger=ledger, clock=clock, ids=ids,
        poll_interval_s=0.0,
    )
    ctx = RunContext(
        db=db, repos=repos, gateway=gateway, clock=clock, ids=ids, config=config,
        resolved_profile=resolved, run_id=run_row.id, episode_id=seeded.episode_id,
        languages=[], aspects=[], stage_run_id=None,
    )
    handler = NarrationScriptHandler()
    subject = SubjectRef(scope=Scope.EPISODE, id=seeded.episode_id, lineage=())

    content_a = BibleContent(
        title="فانوسُ الفجرِ", premise="المقدمةُ الأولى",
        characters=[_character("نُور")], style_tokens=["دفء"],
    )
    content_b = BibleContent(
        title="فانوسُ الفجرِ", premise="مقدمةٌ أخرى تمامًا مختلفة",
        characters=[_character("نُور")], style_tokens=["دفء"],
    )
    repos.bible.create_version(seeded.series_id, content_a, created_by="s1")
    hash_a = handler.input_hash(ctx, subject)
    repos.bible.create_version(seeded.series_id, content_b, created_by="s1")
    hash_b = handler.input_hash(ctx, subject)
    assert hash_a != hash_b

    # and the hash really is a function of the pack bytes
    pack = pack_context(bible=_bible(), episode=_episode(),
                        prior_deliverables=[], stage_input="x")
    assert hashlib.sha256(pack.text.encode("utf-8")).hexdigest() == pack.pack_sha256
    json.dumps({"hash_a": hash_a, "hash_b": hash_b})  # both are plain stable strings
    assert len(hash_a) == 64 and len(hash_b) == 64
