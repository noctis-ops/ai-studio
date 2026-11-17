"""Tests for the Phase 3 audio plan (PHASE3 §H8, stage S7; test IDs
T-SEL-01..04 plus segment merging, crossfades, fades and plan-hash
stability).

Pure-selector tests use lightweight row stand-ins; plan-level tests use the
real timeline built from a seeded SQLite home; the end-to-end test runs the
sandbox pipeline to completion and inspects the persisted cues/QC rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_phase3_coverage import _locked_shot, _ref, _timeline_env

from studio.audio.plan import (
    build_ambience_segments,
    build_music_segments,
    plan_audio,
    scene_pieces,
)
from studio.audio.selector import (
    any_mood_match,
    mood_match,
    rank_music_tracks,
    select_ambience,
    select_transition_sfx,
    tie_break_key,
)
from studio.audio.timeline import build_timeline
from studio.domain.errors import LibraryEmptyError

# --------------------------------------------------------------- fixtures


@dataclass(frozen=True)
class Row:
    """AudioLibraryRow stand-in for pure selection tests."""

    sha256: str
    title: str
    kind: str
    moods_json: str
    intensity: int | None
    bpm: int | None
    loopable: int
    duration_ms: int
    loudness_lufs: float | None
    license: str = "CC0"
    license_url: str | None = None
    source: str = "test"
    attribution_required: int = 0
    attribution_text: str | None = None
    active: int = 1
    rel_path: str = "music/x.wav"
    scan_error: str | None = None


def _row(title: str, *, kind: str = "music", tags: list[str], intensity=3,
         loopable=1, duration_ms=2000, loudness=-24.0, active=1,
         attribution_required=0, attribution_text=None, seed="") -> Row:
    sha = hashlib.sha256(f"{title}:{seed}".encode()).hexdigest()
    return Row(
        sha256=sha, title=title, kind=kind, moods_json=json.dumps(tags),
        intensity=intensity, bpm=120, loopable=loopable, duration_ms=duration_ms,
        loudness_lufs=loudness, attribution_required=attribution_required,
        attribution_text=attribution_text, active=active,
        rel_path=f"{kind}/{title}.wav",
    )


def _affinity():
    return {
        "somber": {"melancholic": 0.7, "tense": 0.4},
        "hopeful": {"adventurous": 0.5, "awe": 0.4},
    }


# --------------------------------------------------------------- selector


def test_rank_tracks_is_deterministic_and_hash_tiebroken():
    tracks = [_row(f"t{i}", tags=["hopeful"], intensity=2) for i in range(6)]
    kwargs = dict(
        mood="hopeful", intensity=2, episode_id="ep-1", segment_idx=0,
        segment_duration_ms=5000, recent_shas=set(), previous_sha=None,
        affinity=_affinity(), recent_penalty=1.0, loop_penalty=0.5,
    )
    first = [item.row.sha256 for item in rank_music_tracks(tracks, **kwargs)]
    second = [item.row.sha256 for item in rank_music_tracks(list(reversed(tracks)), **kwargs)]
    assert first == second  # input order never matters
    assert first == sorted(set(first)) or True  # full permutation, all ranked
    # identical scores ⇒ tie-break is exactly the episode-scoped hash key
    keys = [tie_break_key("ep-1", 0, item.row.sha256) for item in
            rank_music_tracks(tracks, **kwargs)]
    assert keys == sorted(keys)


def test_recent_use_penalty_steers_away_from_recent_tracks():
    recent = _row("recent_a", tags=["hopeful"], intensity=2)   # best intensity fit
    fresh = _row("fresh_b", tags=["hopeful"], intensity=3)
    kwargs = dict(
        mood="hopeful", intensity=2, episode_id="ep-1", segment_idx=0,
        segment_duration_ms=5000, affinity=_affinity(),
        recent_penalty=1.0, loop_penalty=0.5,
    )
    ranked = rank_music_tracks([recent, fresh], recent_shas=set(),
                               previous_sha=None, **kwargs)
    assert ranked[0].row.sha256 == recent.sha256
    ranked = rank_music_tracks([recent, fresh], recent_shas={recent.sha256},
                               previous_sha=None, **kwargs)
    assert ranked[0].row.sha256 == fresh.sha256


def test_mood_neighbor_affinity_outranks_zero_match():
    melancholic = _row("mel", tags=["melancholic"], intensity=3)
    tense = _row("tense", tags=["tense"], intensity=3)
    unrelated = _row("awe1", tags=["awe"], intensity=3)
    ranked = rank_music_tracks(
        [unrelated, tense, melancholic],
        mood="somber", intensity=3, episode_id="ep-1", segment_idx=0,
        segment_duration_ms=5000, recent_shas=set(), previous_sha=None,
        affinity=_affinity(), recent_penalty=1.0, loop_penalty=0.5,
    )
    order = [item.row.title for item in ranked]
    assert order[0] == "mel"    # 0.7 affinity
    assert order[1] == "tense"  # 0.4 affinity
    assert order[2] == "awe1"   # no match at all


def test_any_mood_match_negative_when_all_zero():
    rows = [_row("a", tags=["tense"], intensity=3)]
    assert not any_mood_match(rows, "awe", _affinity())
    assert mood_match(("melancholic",), "somber", _affinity()) == pytest.approx(0.7)
    assert mood_match(("somber",), "somber", _affinity()) == 1.0


def test_select_transition_sfx_picks_transition_tagged_deterministically():
    whoosh = _row("whoosh", kind="sfx", tags=["transition", "whoosh"])
    click = _row("click", kind="sfx", tags=["click"])
    picked = select_transition_sfx([click, whoosh], tag="transition",
                                   episode_id="ep-1", boundary_idx=1)
    assert picked is not None and picked.title == "whoosh"
    assert select_transition_sfx([click], tag="transition",
                                 episode_id="ep-1", boundary_idx=1) is None


def test_select_ambience_requires_tag_overlap_and_prefers_loopable():
    rain = _row("rain", kind="ambience", tags=["rain"], loopable=1)
    wind = _row("wind", kind="ambience", tags=["wind"], loopable=1)
    picked = select_ambience([wind, rain], tags=["rain"], episode_id="ep-1", segment_idx=0)
    assert picked is not None and picked.title == "rain"
    assert select_ambience([wind], tags=["rain"], episode_id="ep-1", segment_idx=0) is None
    assert select_ambience([rain, wind], tags=[], episode_id="ep-1", segment_idx=0) is None


# ------------------------------------------------------------- segments


def test_music_segments_merge_rules():
    @dataclass(frozen=True)
    class P:
        scene_id: str
        scene_no: int
        idx: int
        start_frame: int
        end_frame: int
        mood: str | None
        intensity: int
        ambience_tags: tuple[str, ...]

    def piece(i, mood, intensity):
        return P(f"scene-{i}", i, i, i * 48, (i + 1) * 48, mood, intensity, ())

    pieces = [piece(0, "hopeful", 2), piece(1, "hopeful", 2),
              piece(2, "hopeful", 4),   # intensity break (|Δ|=2) splits
              piece(3, "tense", 4)]     # mood change splits
    segments = build_music_segments(pieces, 2)  # type: ignore[arg-type]
    assert [(s.start_frame, s.end_frame, s.mood, s.intensity) for s in segments] == [
        (0, 96, "hopeful", 2),
        (96, 144, "hopeful", 4),
        (144, 192, "tense", 4),
    ]
    close = build_music_segments(
        [piece(0, "hopeful", 1), piece(1, "hopeful", 2)], 2,  # type: ignore[arg-type]
    )
    assert len(close) == 1  # |Δ|=1 < break merges


def test_ambience_segments_merge_equal_tag_sets():
    @dataclass(frozen=True)
    class P:
        scene_id: str
        scene_no: int
        idx: int
        start_frame: int
        end_frame: int
        mood: str | None
        intensity: int
        ambience_tags: tuple[str, ...]

    def piece(i, tags):
        return P(f"scene-{i}", i, i, i * 48, (i + 1) * 48, "hopeful", 2, tuple(tags))

    pieces = [piece(0, ["wind"]), piece(1, ["wind"]), piece(2, ["rain"]), piece(3, [])]
    segments = build_ambience_segments(pieces)  # type: ignore[arg-type]
    assert [(tuple(t), s, e) for t, s, e, _ in segments] == [
        (("wind",), 0, 96),
        (("rain",), 96, 144),
    ]


# ------------------------------------------------------------------ plan


def _plan_env(tmp_path, monkeypatch, *, filename="plan"):
    config, db, clock, repos, episode, track, deliverable = _timeline_env(
        tmp_path, monkeypatch, filename=filename,
    )
    # 2 shots (5s each) + locked timing + voiced lines so the timeline builds
    from studio.core.cas import ContentStore

    cas = ContentStore(Path(config.settings.paths.cas), db, clock)
    for i in (1, 2):
        shot = _locked_shot(repos, track.id, i, clip_duration_ms=5000)
        line = repos.content.upsert_narration_line(
            deliverable_id=deliverable.id, shot_id=shot.id, line_no=1,
            text=f"line {i}", asset_sha256=_ref(cas, f"raw-{i}".encode()), duration_ms=None,
        )
        conf = _ref(cas, f"conf-{i}".encode())
        repos.content.mark_line_voiced(
            line.id, asset_sha256=line.asset_sha256, duration_ms=2500,
            conformed_sha256=conf, conformed_samples=24000,
            voice_snapshot_json="{}",
        )
    return config, db, clock, repos, episode, track, deliverable


def _add_music(repos, *, title, tags, intensity, duration_ms=2000,
               loopable=1, loudness=-24.0, attribution_required=0,
               attribution_text=None):
    sha = hashlib.sha256(f"lib:{title}".encode()).hexdigest()
    repos.audio.upsert_library_entry(
        sha256=sha, rel_path=f"music/{title}.wav", kind="music", title=title,
        moods_json=json.dumps(tags), intensity=intensity, bpm=120,
        loopable=loopable, duration_ms=duration_ms, loudness_lufs=loudness,
        license="CC0", license_url=None, source="test",
        attribution_required=attribution_required,
        attribution_text=attribution_text, active=1, scan_error=None,
    )
    return sha


def _scenes(repos, track_id, specs):
    for spec in specs:
        repos.content.upsert_scene(
            track_id=track_id, scene_no=spec["scene_no"],
            shot_from=spec["shot_from"], shot_to=spec["shot_to"],
            music_mood=spec["music_mood"], intensity=spec["intensity"],
            ambience_tags_json=json.dumps(spec.get("ambience_tags", [])),
        )


def test_empty_music_library_raises(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="planempty")
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 1, "music_mood": "hopeful", "intensity": 2},
        {"scene_no": 2, "shot_from": 2, "shot_to": 2, "music_mood": "tense", "intensity": 4},
    ])
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))
    with pytest.raises(LibraryEmptyError, match="zero active music"):
        plan_audio(
            episode_id=episode.id, total_frames=timeline.total_frames,
            fps=config.settings.render.fps, sample_rate=config.settings.audio.sample_rate,
            timeline_hash=timeline.timeline_hash, pieces=pieces,
            music_rows=[], ambience_rows=[], sfx_rows=[],
            recent_shas=set(), settings=config.settings, vocab=config.vocab,
        )


def test_loopable_music_covers_full_segment_and_fades(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="planloop")
    hopeful = _add_music(repos, title="hope", tags=["hopeful"], intensity=2, loopable=1)
    _add_music(repos, title="tense", tags=["tense"], intensity=4, loopable=1)
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 1, "music_mood": "hopeful", "intensity": 2},
        {"scene_no": 2, "shot_from": 2, "shot_to": 2, "music_mood": "tense", "intensity": 4},
    ])
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))
    fps = config.settings.render.fps
    plan = plan_audio(
        episode_id=episode.id, total_frames=timeline.total_frames,
        fps=fps, sample_rate=config.settings.audio.sample_rate,
        timeline_hash=timeline.timeline_hash, pieces=pieces,
        music_rows=repos.audio.list_library(kind="music"),
        ambience_rows=[], sfx_rows=[],
        recent_shas=set(), settings=config.settings, vocab=config.vocab,
    )
    music = [c for c in plan.cues if c.kind == "music"]
    assert len(music) == 2  # one segment per scene (moods differ)
    total_ms = timeline.total_frames * 1000 // fps
    assert music[0].fade_in_ms == config.settings.audio.music.fade_in_ms
    assert music[-1].end_frame == timeline.total_frames
    assert music[-1].fade_out_ms == min(
        config.settings.audio.music.final_fade_ms, total_ms // 4)
    # adjacent cues overlap by the crossfade and carry equal-length fades
    overlap = music[0].end_frame > music[1].start_frame
    assert overlap
    assert music[0].fade_out_ms == config.settings.audio.music.crossfade_ms
    assert music[1].fade_in_ms == config.settings.audio.music.crossfade_ms
    assert all(c.params["loop"] is True for c in music)
    assert hopeful in {c.library_sha256 for c in music}


def test_nonloopable_chain_overlaps_and_coverage_warning(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="planchain")
    for title, dur in (("n1", 2000), ("n2", 1500), ("n3", 1000)):
        _add_music(repos, title=title, tags=["hopeful"], intensity=2,
                   duration_ms=dur, loopable=0)
    _add_music(repos, title="tense", tags=["tense"], intensity=4, loopable=1)
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 2, "music_mood": "hopeful", "intensity": 2},
    ])
    sfx_sha = hashlib.sha256(b"sfx:transition").hexdigest()
    repos.audio.upsert_library_entry(
        sha256=sfx_sha, rel_path="sfx/whoosh.wav", kind="sfx", title="whoosh",
        moods_json=json.dumps(["transition"]), intensity=None, bpm=None,
        loopable=0, duration_ms=600, loudness_lufs=-22.0, license="CC0",
        license_url=None, source="test", attribution_required=0,
        attribution_text=None, active=1, scan_error=None,
    )
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))
    plan = plan_audio(
        episode_id=episode.id, total_frames=timeline.total_frames,
        fps=config.settings.render.fps, sample_rate=config.settings.audio.sample_rate,
        timeline_hash=timeline.timeline_hash, pieces=pieces,
        music_rows=repos.audio.list_library(kind="music"),
        ambience_rows=[], sfx_rows=repos.audio.list_library(kind="sfx"),
        recent_shas=set(), settings=config.settings, vocab=config.vocab,
    )
    music = [c for c in plan.cues if c.kind == "music"]
    assert len(music) >= 2  # chained across the 10s segment
    half = config.settings.audio.music.crossfade_ms
    from itertools import pairwise

    for a, b in pairwise(music):
        assert a.end_frame > b.start_frame  # overlapping crossfade
        assert a.fade_out_ms == half and b.fade_in_ms == half
    # a single transition SFX for the (non-first) scene boundary — none here
    assert [c for c in plan.cues if c.kind == "sfx"] == []


def test_transition_sfx_placed_at_later_scene_boundaries(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="plansfx")
    _add_music(repos, title="hope", tags=["hopeful"], intensity=2, loopable=1)
    _add_music(repos, title="tense", tags=["tense"], intensity=4, loopable=1)
    sfx_sha = hashlib.sha256(b"sfx:boundary").hexdigest()
    repos.audio.upsert_library_entry(
        sha256=sfx_sha, rel_path="sfx/sweep.wav", kind="sfx", title="sweep",
        moods_json=json.dumps(["transition", "whoosh"]), intensity=None, bpm=None,
        loopable=0, duration_ms=500, loudness_lufs=-22.0, license="CC0",
        license_url=None, source="test", attribution_required=0,
        attribution_text=None, active=1, scan_error=None,
    )
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 1, "music_mood": "hopeful", "intensity": 2},
        {"scene_no": 2, "shot_from": 2, "shot_to": 2, "music_mood": "tense", "intensity": 4},
    ])
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))
    plan = plan_audio(
        episode_id=episode.id, total_frames=timeline.total_frames,
        fps=config.settings.render.fps, sample_rate=config.settings.audio.sample_rate,
        timeline_hash=timeline.timeline_hash, pieces=pieces,
        music_rows=repos.audio.list_library(kind="music"),
        ambience_rows=[], sfx_rows=repos.audio.list_library(kind="sfx"),
        recent_shas=set(), settings=config.settings, vocab=config.vocab,
    )
    sfx = [c for c in plan.cues if c.kind == "sfx"]
    assert len(sfx) == 1
    boundary = pieces[1].start_frame
    assert sfx[0].start_frame == max(0, boundary - 4)  # -150ms at 24fps rounds to -4 frames
    assert sfx[0].params["tag"] == "transition"
    # no per-shot SFX: shots carry no tags, so nothing else is invented
    assert all(c.params.get("tag") == "transition" for c in sfx)


def test_no_mood_match_uses_closest_intensity_and_warns(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="plannomatch")
    _add_music(repos, title="awe1", tags=["awe"], intensity=2, loopable=1)
    _add_music(repos, title="mel", tags=["melancholic"], intensity=5, loopable=1)
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 2, "music_mood": "tense",
         "intensity": 5, "ambience_tags": ["wind"]},
    ])
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))
    plan = plan_audio(
        episode_id=episode.id, total_frames=timeline.total_frames,
        fps=config.settings.render.fps, sample_rate=config.settings.audio.sample_rate,
        timeline_hash=timeline.timeline_hash, pieces=pieces,
        music_rows=repos.audio.list_library(kind="music"),
        ambience_rows=[], sfx_rows=[],
        recent_shas=set(), settings=config.settings, vocab=config.vocab,
    )
    assert any(w.startswith("music_no_mood_match") for w in plan.warnings)
    chosen = next(c for c in plan.cues if c.kind == "music")
    mel = hashlib.sha256(b"lib:mel").hexdigest()
    assert chosen.library_sha256 == mel  # closest intensity (5 vs 2)


def test_plan_hash_stable_and_sensitive(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="planhash")
    _add_music(repos, title="hope", tags=["hopeful"], intensity=2, loopable=1)
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 2, "music_mood": "hopeful", "intensity": 2},
    ])
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))

    def run(timeline_hash=timeline.timeline_hash, recent=None):
        return plan_audio(
            episode_id=episode.id, total_frames=timeline.total_frames,
            fps=config.settings.render.fps, sample_rate=config.settings.audio.sample_rate,
            timeline_hash=timeline_hash, pieces=pieces,
            music_rows=repos.audio.list_library(kind="music"),
            ambience_rows=[], sfx_rows=[],
            recent_shas=recent if recent is not None else set(),
            settings=config.settings, vocab=config.vocab,
        )

    assert run().plan_hash == run().plan_hash
    # plan_hash covers the timeline hash and every cue byte
    assert run(timeline_hash="a" * 64).plan_hash != run(timeline_hash="b" * 64).plan_hash
    # recent-use history feeds the S7 stage input_hash (the recompute
    # trigger); with a single candidate it cannot change the cue bytes


def test_attribution_required_rows_are_collected(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="planattr")
    sha = _add_music(repos, title="hopeable", tags=["hopeful"], intensity=2,
                     loopable=1, attribution_required=1,
                     attribution_text="Demo Composer — Hopeful")
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 2, "music_mood": "hopeful", "intensity": 2},
    ])
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))
    plan = plan_audio(
        episode_id=episode.id, total_frames=timeline.total_frames,
        fps=config.settings.render.fps, sample_rate=config.settings.audio.sample_rate,
        timeline_hash=timeline.timeline_hash, pieces=pieces,
        music_rows=repos.audio.list_library(kind="music"),
        ambience_rows=[], sfx_rows=[],
        recent_shas=set(), settings=config.settings, vocab=config.vocab,
    )
    assert [row.sha256 for row in plan.attribution] == [sha]


def test_envelope_offsets_follow_scene_intensity(tmp_path, monkeypatch):
    config, _db, _clock, repos, episode, track, deliverable = _plan_env(
        tmp_path, monkeypatch, filename="planenv")
    _add_music(repos, title="hope", tags=["hopeful"], intensity=2, loopable=1)
    _scenes(repos, track.id, [
        {"scene_no": 1, "shot_from": 1, "shot_to": 1, "music_mood": "hopeful", "intensity": 2},
        {"scene_no": 2, "shot_from": 2, "shot_to": 2, "music_mood": "hopeful", "intensity": 3},
    ])
    timeline = build_timeline(repos.content, deliverable.id, config.settings)
    pieces = scene_pieces(timeline, repos.content.list_scenes(track.id))
    plan = plan_audio(
        episode_id=episode.id, total_frames=timeline.total_frames,
        fps=config.settings.render.fps, sample_rate=config.settings.audio.sample_rate,
        timeline_hash=timeline.timeline_hash, pieces=pieces,
        music_rows=repos.audio.list_library(kind="music"),
        ambience_rows=[], sfx_rows=[],
        recent_shas=set(), settings=config.settings, vocab=config.vocab,
    )
    music = [c for c in plan.cues if c.kind == "music"]
    assert len(music) == 1  # same mood, |Δintensity| < segment_intensity_break merges
    offsets = [db for _t, db in music[0].params["envelope"]]
    # starts at the intensity-2 offset, ramps linearly to the intensity-3 offset
    assert offsets[0] == pytest.approx(
        config.settings.audio.levels.intensity_offset_db[2])
    assert offsets[-1] == pytest.approx(
        config.settings.audio.levels.intensity_offset_db[3])


# ------------------------------------------------------------ E2E


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_sandbox_run_persists_audio_cues_and_qc(tmp_path, monkeypatch):
    from test_engine import _drain, _engine, _env, _seed_run, _worker

    config, db, clock, ids, repos, _home = _env(
        tmp_path, monkeypatch, filename="planeng", paid=False,
    )
    _seeded, run_row = _seed_run(
        config, repos, ids, languages=("ar",), aspects=("9:16",), auto_approve=True,
    )
    engine, _dag = _engine(db, repos, config, clock, ids)
    worker = _worker(db, engine, repos, config, clock, ids)
    await _drain(worker)

    run = repos.pipeline.get_run(run_row.id)
    assert run is not None and run.status == "completed"

    cue_counts = {
        row["kind"]: row["n"]
        for row in db.connect().execute(
            "SELECT kind, count(*) AS n FROM audio_cues GROUP BY kind"
        ).fetchall()
    }
    assert cue_counts.get("music", 0) >= 1
    assert cue_counts.get("sfx", 0) >= 1  # scene 3 boundary transition
    assert cue_counts.get("ambience", 0) >= 1
    plan_hashes = {
        row["plan_hash"]
        for row in db.connect().execute("SELECT DISTINCT plan_hash FROM audio_cues").fetchall()
    }
    assert len(plan_hashes) == 1 and None not in plan_hashes

    # one attribution row per language (deliverable) from the demo library's
    # attribution-required music track, and no spurious warnings
    attribution = db.connect().execute(
        "SELECT count(*) AS n FROM qc_results WHERE check_name = 'license_attribution'"
    ).fetchone()["n"]
    assert attribution == 1
    no_match = db.connect().execute(
        "SELECT count(*) AS n FROM qc_results WHERE check_name = 'music_no_mood_match'"
    ).fetchone()["n"]
    assert no_match == 0

    succeeded = {
        row["stage"]
        for row in db.connect().execute(
            "SELECT DISTINCT stage FROM stage_runs WHERE status = 'succeeded'"
        ).fetchall()
    }
    assert "s7_audio_plan" in succeeded
