"""Content entities: series, characters, episodes, tracks, deliverables,
shots, shot attempts and narration lines."""

from __future__ import annotations

from typing import Any, Protocol

from studio.domain.models import (
    CharacterRow,
    DeliverableRow,
    EpisodeRow,
    NarrationLineRow,
    SceneRow,
    SeriesRow,
    ShotAttemptRow,
    ShotRow,
    TrackRow,
)

from .base import SqliteRepo, row_to_dict


class ContentRepo(Protocol):
    def create_series(self, *, name: str, slug: str) -> SeriesRow: ...
    def get_series(self, series_id: str) -> SeriesRow | None: ...

    def get_series_by_slug(self, slug: str) -> SeriesRow | None: ...

    def create_character(
        self, *, series_id: str, role: str, name: str, voices_json: str
    ) -> CharacterRow: ...

    def get_character(self, character_id: str) -> CharacterRow | None: ...

    def get_narrator(self, series_id: str) -> CharacterRow | None: ...

    def get_character_by_slug(self, series_id: str, slug: str) -> CharacterRow | None: ...

    def create_episode(
        self, *, series_id: str, title: str, concept: str, languages_json: str, aspects_json: str
    ) -> EpisodeRow: ...

    def get_episode(self, episode_id: str) -> EpisodeRow | None: ...

    def set_episode_status(self, episode_id: str, status: str) -> None: ...

    def create_track(self, *, episode_id: str, aspect: str) -> TrackRow: ...

    def list_tracks(self, episode_id: str) -> list[TrackRow]: ...

    def get_track(self, track_id: str) -> TrackRow | None: ...

    def create_deliverable(
        self, *, track_id: str, language: str, channel_id: str
    ) -> DeliverableRow: ...

    def list_deliverables(self, episode_id: str) -> list[DeliverableRow]: ...
    def set_deliverable_metadata(
        self,
        *,
        deliverable_id: str,
        metadata_json: str,
        safety_status: str,
    ) -> None: ...

    def get_deliverable(self, deliverable_id: str) -> DeliverableRow | None: ...

    def set_deliverable_published(
        self, deliverable_id: str, *, published_at: str, published_url: str
    ) -> None: ...

    def create_shot(
        self,
        *,
        track_id: str,
        shot_no: int,
        prompt: str,
        min_duration_ms: int | None = None,
    ) -> ShotRow: ...

    def list_shots(self, episode_id: str) -> list[ShotRow]: ...

    def get_shot(self, shot_id: str) -> ShotRow | None: ...

    def shots_for_track(self, track_id: str) -> list[ShotRow]: ...

    def current_shot_attempt(
        self, shot_id: str, *, kind: str | None = None
    ) -> ShotAttemptRow | None: ...

    def next_shot_attempt_no(self, shot_id: str) -> int: ...

    def update_shot_attempt(self, attempt_id: str, **fields: Any) -> ShotAttemptRow | None:
        """Update media bookkeeping columns on an attempt (whitelist)."""
        ...

    def get_shot_attempt(self, attempt_id: str) -> ShotAttemptRow | None: ...

    def create_shot_attempt(
        self,
        *,
        shot_id: str,
        attempt_no: int,
        nonce: str,
        prompt_used: str,
        rejection_note: str | None = None,
        kind: str = "keyframe",
    ) -> ShotAttemptRow: ...

    def upsert_narration_line(
        self,
        *,
        deliverable_id: str,
        shot_id: str,
        line_no: int,
        text: str,
        asset_sha256: str | None,
        duration_ms: int | None,
    ) -> NarrationLineRow: ...

    def get_narration_line(self, line_id: str) -> NarrationLineRow | None: ...

    def narration_lines_for_episode(self, episode_id: str) -> list[NarrationLineRow]: ...

    def set_line_tts_nonce(self, line_id: str, tts_nonce: str) -> None: ...

    def set_line_text_final(self, line_id: str, text_final: str | None) -> None: ...

    def mark_line_voiced(
        self,
        line_id: str,
        *,
        asset_sha256: str,
        duration_ms: int | None,
        conformed_sha256: str,
        conformed_samples: int,
        voice_snapshot_json: str,
    ) -> None: ...

    def set_shot_timing(
        self,
        shot_id: str,
        *,
        clip_duration_ms: int,
        speed_factor: float,
        hold_frames: int,
        timing_status: str,
    ) -> None: ...

    def upsert_scene(
        self,
        *,
        track_id: str,
        scene_no: int,
        shot_from: int,
        shot_to: int,
        music_mood: str | None,
        intensity: int,
        ambience_tags_json: str,
    ) -> SceneRow: ...

    def list_scenes(self, track_id: str) -> list[SceneRow]: ...

    def narration_lines(self, deliverable_id: str) -> list[NarrationLineRow]: ...

    def narration_lines_for_shot(self, shot_id: str) -> list[NarrationLineRow]: ...

    def narration_max_duration_ms(self, track_id: str, shot_id: str) -> int | None: ...


class SqliteContentRepo(SqliteRepo):
    # ---- series / characters -----------------------------------------------

    def get_series(self, series_id: str) -> SeriesRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM series WHERE id = ?", (series_id,))
            .fetchone()
        )
        return SeriesRow.model_validate(row_to_dict(row)) if row else None

    def get_series_by_slug(self, slug: str) -> SeriesRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM series WHERE slug = ?", (slug,))
            .fetchone()
        )
        return SeriesRow.model_validate(row_to_dict(row)) if row else None

    def create_series(self, *, name: str, slug: str) -> SeriesRow:
        now = self._now()
        series_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            "INSERT INTO series(id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            (series_id, name, slug, now),
        )
        row = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
        assert row is not None
        return SeriesRow.model_validate(row_to_dict(row))

    def create_character(
        self, *, series_id: str, role: str, name: str, voices_json: str
    ) -> CharacterRow:
        now = self._now()
        character_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            "INSERT INTO characters(id, series_id, role, name, voices_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (character_id, series_id, role, name, voices_json, now),
        )
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
        assert row is not None
        return CharacterRow.model_validate(row_to_dict(row))

    def get_character(self, character_id: str) -> CharacterRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM characters WHERE id = ?", (character_id,))
            .fetchone()
        )
        return CharacterRow.model_validate(row_to_dict(row)) if row else None

    def get_character_by_slug(self, series_id: str, slug: str) -> CharacterRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM characters WHERE series_id = ? AND role = ?",
                (series_id, slug),
            )
            .fetchone()
        )
        return CharacterRow.model_validate(row_to_dict(row)) if row else None

    def get_narrator(self, series_id: str) -> CharacterRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM characters WHERE series_id = ? AND role = 'narrator' "
                "ORDER BY created_at, id LIMIT 1",
                (series_id,),
            )
            .fetchone()
        )
        return CharacterRow.model_validate(row_to_dict(row)) if row else None

    # ---- episodes / tracks / deliverables -----------------------------------

    def create_episode(
        self,
        *,
        series_id: str,
        title: str,
        concept: str,
        languages_json: str,
        aspects_json: str,
    ) -> EpisodeRow:
        now = self._now()
        episode_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO episodes(id, series_id, title, concept, status,
                                 languages_json, aspects_json, created_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (episode_id, series_id, title, concept, languages_json, aspects_json, now),
        )
        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row is not None
        return EpisodeRow.model_validate(row_to_dict(row))

    def get_episode(self, episode_id: str) -> EpisodeRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM episodes WHERE id = ?", (episode_id,))
            .fetchone()
        )
        return EpisodeRow.model_validate(row_to_dict(row)) if row else None

    def set_episode_status(self, episode_id: str, status: str) -> None:
        self.db.connect().execute(
            "UPDATE episodes SET status = ? WHERE id = ?", (status, episode_id)
        )

    def create_track(self, *, episode_id: str, aspect: str) -> TrackRow:
        now = self._now()
        track_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            "INSERT OR IGNORE INTO tracks(id, episode_id, aspect, created_at) VALUES (?, ?, ?, ?)",
            (track_id, episode_id, aspect, now),
        )
        row = conn.execute(
            "SELECT * FROM tracks WHERE episode_id = ? AND aspect = ?", (episode_id, aspect)
        ).fetchone()
        assert row is not None
        return TrackRow.model_validate(row_to_dict(row))

    def list_tracks(self, episode_id: str) -> list[TrackRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM tracks WHERE episode_id = ? ORDER BY created_at, id", (episode_id,)
            )
            .fetchall()
        )
        return [TrackRow.model_validate(row_to_dict(r)) for r in rows]

    def get_track(self, track_id: str) -> TrackRow | None:
        row = self.db.connect().execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return TrackRow.model_validate(row_to_dict(row)) if row else None

    def create_deliverable(
        self, *, track_id: str, language: str, channel_id: str
    ) -> DeliverableRow:
        now = self._now()
        deliverable_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO deliverables(id, track_id, language, channel_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (deliverable_id, track_id, language, channel_id, now),
        )
        row = conn.execute(
            "SELECT * FROM deliverables WHERE track_id = ? AND language = ?",
            (track_id, language),
        ).fetchone()
        assert row is not None
        return DeliverableRow.model_validate(row_to_dict(row))

    def list_deliverables(self, episode_id: str) -> list[DeliverableRow]:
        rows = (
            self.db.connect()
            .execute(
                """
            SELECT d.* FROM deliverables d
            JOIN tracks t ON t.id = d.track_id
            WHERE t.episode_id = ?
            ORDER BY d.created_at, d.id
            """,
                (episode_id,),
            )
            .fetchall()
        )
        return [DeliverableRow.model_validate(row_to_dict(r)) for r in rows]

    def set_deliverable_metadata(
        self,
        *,
        deliverable_id: str,
        metadata_json: str,
        safety_status: str,
    ) -> None:
        import json as _json

        from studio.domain.errors import StudioError

        if safety_status not in ("pending", "clear", "flagged"):
            raise StudioError(f"safety_status must be pending|clear|flagged, got {safety_status!r}")
        try:
            _json.loads(metadata_json)
        except _json.JSONDecodeError as exc:
            raise StudioError(f"metadata_json is not valid JSON: {exc}") from exc
        result = self.db.connect().execute(
            "UPDATE deliverables SET metadata_json = ?, safety_status = ? WHERE id = ?",
            (metadata_json, safety_status, deliverable_id),
        )
        if result.rowcount != 1:
            raise StudioError(f"deliverable not found: {deliverable_id}")

    def set_deliverable_published(
        self, deliverable_id: str, *, published_at: str, published_url: str
    ) -> None:
        from studio.domain.errors import StudioError

        if not published_url.strip():
            raise StudioError("published_url must not be empty")
        result = self.db.connect().execute(
            "UPDATE deliverables SET published_at = ?, published_url = ? WHERE id = ?",
            (published_at, published_url.strip(), deliverable_id),
        )
        if result.rowcount != 1:
            raise StudioError(f"deliverable not found: {deliverable_id}")

    def get_deliverable(self, deliverable_id: str) -> DeliverableRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM deliverables WHERE id = ?", (deliverable_id,))
            .fetchone()
        )
        return DeliverableRow.model_validate(row_to_dict(row)) if row else None

    # ---- shots / attempts ---------------------------------------------------

    def create_shot(
        self,
        *,
        track_id: str,
        shot_no: int,
        prompt: str,
        min_duration_ms: int | None = None,
    ) -> ShotRow:
        now = self._now()
        shot_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO shots(id, track_id, shot_no, prompt, created_at,
                                        min_duration_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (shot_id, track_id, shot_no, prompt, now, min_duration_ms),
        )
        row = conn.execute(
            "SELECT * FROM shots WHERE track_id = ? AND shot_no = ?", (track_id, shot_no)
        ).fetchone()
        assert row is not None
        return ShotRow.model_validate(row_to_dict(row))

    def list_shots(self, episode_id: str) -> list[ShotRow]:
        rows = (
            self.db.connect()
            .execute(
                """
            SELECT s.* FROM shots s
            JOIN tracks t ON t.id = s.track_id
            WHERE t.episode_id = ?
            ORDER BY t.created_at, t.id, s.shot_no
            """,
                (episode_id,),
            )
            .fetchall()
        )
        return [ShotRow.model_validate(row_to_dict(r)) for r in rows]

    def get_shot(self, shot_id: str) -> ShotRow | None:
        row = self.db.connect().execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
        return ShotRow.model_validate(row_to_dict(row)) if row else None

    def shots_for_track(self, track_id: str) -> list[ShotRow]:
        rows = (
            self.db.connect()
            .execute("SELECT * FROM shots WHERE track_id = ? ORDER BY shot_no", (track_id,))
            .fetchall()
        )
        return [ShotRow.model_validate(row_to_dict(r)) for r in rows]

    def current_shot_attempt(
        self, shot_id: str, *, kind: str | None = None
    ) -> ShotAttemptRow | None:
        if kind is None:
            row = (
                self.db.connect()
                .execute(
                    "SELECT * FROM shot_attempts WHERE shot_id = ? "
                    "ORDER BY attempt_no DESC, created_at DESC LIMIT 1",
                    (shot_id,),
                )
                .fetchone()
            )
        else:
            row = (
                self.db.connect()
                .execute(
                    "SELECT * FROM shot_attempts WHERE shot_id = ? AND kind = ? "
                    "ORDER BY attempt_no DESC, created_at DESC LIMIT 1",
                    (shot_id, kind),
                )
                .fetchone()
            )
        return ShotAttemptRow.model_validate(row_to_dict(row)) if row else None

    def next_shot_attempt_no(self, shot_id: str) -> int:
        row = (
            self.db.connect()
            .execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS m FROM shot_attempts WHERE shot_id = ?",
                (shot_id,),
            )
            .fetchone()
        )
        return int(row["m"]) + 1 if row else 1

    # Phase 4 (migration 005) media bookkeeping whitelist.
    _ATTEMPT_UPDATABLE = frozenset(
        {
            "nonce",
            "prompt_spec_json",
            "sanitize_level",
            "refusal_log_json",
            "qc_status",
            "qc_json",
            "asset_id",
            "conformed_asset_id",
            "cost_musd",
            "provider_call_id",
            "policy_blocked",
        }
    )

    def update_shot_attempt(self, attempt_id: str, **fields: Any) -> ShotAttemptRow | None:
        unknown = sorted(set(fields) - self._ATTEMPT_UPDATABLE)
        if unknown:
            raise ValueError(f"shot_attempts columns not updatable: {', '.join(unknown)}")
        if not fields:
            return self.get_shot_attempt(attempt_id)
        conn = self.db.connect()
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = [fields[column] for column in fields]
        conn.execute(
            f"UPDATE shot_attempts SET {assignments} WHERE id = ?",  # noqa: S608
            (*values, attempt_id),
        )
        return self.get_shot_attempt(attempt_id)

    def get_shot_attempt(self, attempt_id: str) -> ShotAttemptRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM shot_attempts WHERE id = ?", (attempt_id,))
            .fetchone()
        )
        return ShotAttemptRow.model_validate(row_to_dict(row)) if row else None

    def create_shot_attempt(
        self,
        *,
        shot_id: str,
        attempt_no: int,
        nonce: str,
        prompt_used: str,
        rejection_note: str | None = None,
        kind: str = "keyframe",
    ) -> ShotAttemptRow:
        now = self._now()
        attempt_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO shot_attempts(id, shot_id, attempt_no, nonce, prompt_used,
                                      rejection_note, created_at, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, shot_id, attempt_no, nonce, prompt_used, rejection_note, now, kind),
        )
        row = conn.execute("SELECT * FROM shot_attempts WHERE id = ?", (attempt_id,)).fetchone()
        assert row is not None
        return ShotAttemptRow.model_validate(row_to_dict(row))

    # ---- narration -----------------------------------------------------------

    def upsert_narration_line(
        self,
        *,
        deliverable_id: str,
        shot_id: str,
        line_no: int,
        text: str,
        asset_sha256: str | None,
        duration_ms: int | None,
    ) -> NarrationLineRow:
        now = self._now()
        line_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO narration_lines(id, deliverable_id, shot_id, line_no, text,
                                        asset_sha256, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deliverable_id, shot_id) DO UPDATE SET
                line_no = excluded.line_no,
                text = excluded.text,
                asset_sha256 = excluded.asset_sha256,
                duration_ms = excluded.duration_ms
            """,
            (line_id, deliverable_id, shot_id, line_no, text, asset_sha256, duration_ms, now),
        )
        row = conn.execute(
            "SELECT * FROM narration_lines WHERE deliverable_id = ? AND shot_id = ?",
            (deliverable_id, shot_id),
        ).fetchone()
        assert row is not None
        return NarrationLineRow.model_validate(row_to_dict(row))

    def narration_lines(self, deliverable_id: str) -> list[NarrationLineRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM narration_lines WHERE deliverable_id = ? ORDER BY line_no",
                (deliverable_id,),
            )
            .fetchall()
        )
        return [NarrationLineRow.model_validate(row_to_dict(r)) for r in rows]

    def narration_lines_for_shot(self, shot_id: str) -> list[NarrationLineRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM narration_lines WHERE shot_id = ? ORDER BY deliverable_id, line_no",
                (shot_id,),
            )
            .fetchall()
        )
        return [NarrationLineRow.model_validate(row_to_dict(r)) for r in rows]

    def narration_max_duration_ms(self, track_id: str, shot_id: str) -> int | None:
        row = (
            self.db.connect()
            .execute(
                """
            SELECT MAX(n.duration_ms) AS max_ms
            FROM narration_lines n
            JOIN deliverables d ON d.id = n.deliverable_id
            WHERE d.track_id = ? AND n.shot_id = ?
            """,
                (track_id, shot_id),
            )
            .fetchone()
        )
        value = row["max_ms"] if row else None
        return int(value) if value is not None else None

    # ---- Phase 3: narration lines (S5 / S6) --------------------------------

    def get_narration_line(self, line_id: str) -> NarrationLineRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM narration_lines WHERE id = ?", (line_id,))
            .fetchone()
        )
        return NarrationLineRow.model_validate(row_to_dict(row)) if row else None

    def narration_lines_for_episode(self, episode_id: str) -> list[NarrationLineRow]:
        rows = (
            self.db.connect()
            .execute(
                """
            SELECT n.* FROM narration_lines n
            JOIN deliverables d ON d.id = n.deliverable_id
            JOIN tracks t ON t.id = d.track_id
            WHERE t.episode_id = ?
            ORDER BY t.created_at, t.id, d.id, n.line_no
            """,
                (episode_id,),
            )
            .fetchall()
        )
        return [NarrationLineRow.model_validate(row_to_dict(r)) for r in rows]

    def set_line_tts_nonce(self, line_id: str, tts_nonce: str) -> None:
        self.db.connect().execute(
            "UPDATE narration_lines SET tts_nonce = ? WHERE id = ?",
            (tts_nonce, line_id),
        )

    def set_line_text_final(self, line_id: str, text_final: str | None) -> None:
        self.db.connect().execute(
            "UPDATE narration_lines SET text_final = ? WHERE id = ?",
            (text_final, line_id),
        )

    def mark_line_voiced(
        self,
        line_id: str,
        *,
        asset_sha256: str,
        duration_ms: int | None,
        conformed_sha256: str,
        conformed_samples: int,
        voice_snapshot_json: str,
    ) -> None:
        self.db.connect().execute(
            """
            UPDATE narration_lines SET
                asset_sha256 = ?,
                duration_ms = ?,
                conformed_sha256 = ?,
                conformed_samples = ?,
                voice_snapshot_json = ?,
                status = 'voiced'
            WHERE id = ?
            """,
            (asset_sha256, duration_ms, conformed_sha256, conformed_samples,
             voice_snapshot_json, line_id),
        )

    def set_shot_timing(
        self,
        shot_id: str,
        *,
        clip_duration_ms: int,
        speed_factor: float,
        hold_frames: int,
        timing_status: str,
    ) -> None:
        self.db.connect().execute(
            """
            UPDATE shots SET
                clip_duration_ms = ?,
                speed_factor = ?,
                hold_frames = ?,
                timing_status = ?
            WHERE id = ?
            """,
            (clip_duration_ms, speed_factor, hold_frames, timing_status, shot_id),
        )

    # ---- Phase 3: scenes ---------------------------------------------------

    def upsert_scene(
        self,
        *,
        track_id: str,
        scene_no: int,
        shot_from: int,
        shot_to: int,
        music_mood: str | None,
        intensity: int,
        ambience_tags_json: str,
    ) -> SceneRow:
        now = self._now()
        scene_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO scenes(id, track_id, scene_no, shot_from, shot_to,
                               music_mood, intensity, ambience_tags_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id, scene_no) DO UPDATE SET
                shot_from = excluded.shot_from,
                shot_to = excluded.shot_to,
                music_mood = excluded.music_mood,
                intensity = excluded.intensity,
                ambience_tags_json = excluded.ambience_tags_json
            """,
            (scene_id, track_id, scene_no, shot_from, shot_to,
             music_mood, intensity, ambience_tags_json, now),
        )
        row = conn.execute(
            "SELECT * FROM scenes WHERE track_id = ? AND scene_no = ?",
            (track_id, scene_no),
        ).fetchone()
        assert row is not None
        return SceneRow.model_validate(row_to_dict(row))

    def list_scenes(self, track_id: str) -> list[SceneRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM scenes WHERE track_id = ? ORDER BY scene_no",
                (track_id,),
            )
            .fetchall()
        )
        return [SceneRow.model_validate(row_to_dict(r)) for r in rows]
