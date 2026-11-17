"""Audio tables repository (Phase 3): character voices, library, cues,
QC results and masters."""

from __future__ import annotations

from typing import Protocol

from studio.domain.models import (
    AudioCueRow,
    AudioLibraryRow,
    AudioMasterRow,
    CharacterVoiceRow,
    QcResultRow,
)

from .base import SqliteRepo, row_to_dict


class NewAudioCue:
    """Plain carrier for cue rows (avoids N dataclass conversions)."""

    def __init__(
        self,
        *,
        cue_index: int,
        kind: str,
        library_sha256: str,
        scene_id: str | None,
        start_frame: int,
        end_frame: int,
        source_offset_ms: int,
        gain_db: float,
        fade_in_ms: int,
        fade_out_ms: int,
        params_json: str,
    ) -> None:
        self.cue_index = cue_index
        self.kind = kind
        self.library_sha256 = library_sha256
        self.scene_id = scene_id
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.source_offset_ms = source_offset_ms
        self.gain_db = gain_db
        self.fade_in_ms = fade_in_ms
        self.fade_out_ms = fade_out_ms
        self.params_json = params_json


class AudioRepo(Protocol):
    def upsert_character_voice(
        self, *, series_id: str, character_slug: str, language: str, voice_id: str,
        settings_json: str,
    ) -> CharacterVoiceRow: ...
    def get_character_voice(
        self, *, series_id: str, character_slug: str, language: str,
    ) -> CharacterVoiceRow | None: ...
    def list_character_voices(self, *, series_id: str) -> list[CharacterVoiceRow]: ...

    def upsert_library_entry(
        self, *, sha256: str, rel_path: str, kind: str, title: str, moods_json: str,
        intensity: int | None, bpm: int | None, loopable: int, duration_ms: int,
        loudness_lufs: float | None, license: str | None, license_url: str | None,
        source: str | None, attribution_required: int, attribution_text: str | None,
        active: int, scan_error: str | None,
    ) -> AudioLibraryRow: ...
    def get_library_entry(self, sha256: str) -> AudioLibraryRow | None: ...
    def list_library(
        self, *, kind: str | None = None, include_inactive: bool = False
    ) -> list[AudioLibraryRow]: ...
    def set_library_active(self, sha256: str, *, active: bool, scan_error: str | None) -> None: ...

    def replace_cues(
        self, deliverable_id: str, cues: list[NewAudioCue], plan_hash: str
    ) -> None: ...
    def list_cues(self, deliverable_id: str, *, kind: str | None = None) -> list[AudioCueRow]: ...
    def music_uses_in_series(self, series_id: str) -> list[tuple[str, str, str]]: ...

    def add_qc_result(
        self, *, deliverable_id: str | None, subject_scope: str, subject_id: str,
        check_name: str, status: str, detail_json: str,
    ) -> QcResultRow: ...
    def list_qc_results(
        self, *, deliverable_id: str | None = None, subject_id: str | None = None,
        check_name: str | None = None,
    ) -> list[QcResultRow]: ...

    def get_ready_master(
        self, *, deliverable_id: str, input_hash: str
    ) -> AudioMasterRow | None: ...
    def insert_master(
        self, *, deliverable_id: str, input_hash: str, timeline_hash: str,
        master_sha256: str, narration_stem_sha256: str | None,
        total_samples: int, loudness_i: float | None, true_peak: float | None,
        lra: float | None, duck_depth_db: float | None, stats_json: str,
    ) -> AudioMasterRow: ...
    def supersede_masters(self, *, deliverable_id: str, keep_id: str) -> int: ...
    def list_masters(
        self, *, deliverable_id: str
    ) -> list[AudioMasterRow]: ...


class SqliteAudioRepo(SqliteRepo):
    # ---- voices ---------------------------------------------------------

    def upsert_character_voice(
        self, *, series_id: str, character_slug: str, language: str, voice_id: str,
        settings_json: str,
    ) -> CharacterVoiceRow:
        now = self._now()
        voice_id_new = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO character_voices(id, series_id, character_slug, language,
                                         voice_id, settings_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(series_id, character_slug, language) DO UPDATE SET
                voice_id = excluded.voice_id,
                settings_json = excluded.settings_json,
                updated_at = excluded.updated_at
            """,
            (voice_id_new, series_id, character_slug, language, voice_id,
             settings_json, now, now),
        )
        row = conn.execute(
            "SELECT * FROM character_voices "
            "WHERE series_id = ? AND character_slug = ? AND language = ?",
            (series_id, character_slug, language),
        ).fetchone()
        assert row is not None
        return CharacterVoiceRow.model_validate(row_to_dict(row))

    def get_character_voice(
        self, *, series_id: str, character_slug: str, language: str,
    ) -> CharacterVoiceRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM character_voices "
            "WHERE series_id = ? AND character_slug = ? AND language = ?",
                (series_id, character_slug, language),
            )
            .fetchone()
        )
        return CharacterVoiceRow.model_validate(row_to_dict(row)) if row else None

    def list_character_voices(self, *, series_id: str) -> list[CharacterVoiceRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM character_voices "
                "WHERE series_id = ? ORDER BY character_slug, language",
                (series_id,),
            )
            .fetchall()
        )
        return [CharacterVoiceRow.model_validate(row_to_dict(r)) for r in rows]

    # ---- library --------------------------------------------------------

    def upsert_library_entry(
        self, *, sha256: str, rel_path: str, kind: str, title: str, moods_json: str,
        intensity: int | None, bpm: int | None, loopable: int, duration_ms: int,
        loudness_lufs: float | None, license: str | None, license_url: str | None,
        source: str | None, attribution_required: int, attribution_text: str | None,
        active: int, scan_error: str | None,
    ) -> AudioLibraryRow:
        now = self._now()
        self.db.connect().execute(
            """
            INSERT INTO audio_library(sha256, rel_path, kind, title, moods_json,
                                      intensity, bpm, loopable, duration_ms, loudness_lufs,
                                      license, license_url, source, attribution_required,
                                      attribution_text, active, scan_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                rel_path = excluded.rel_path,
                kind = excluded.kind,
                title = excluded.title,
                moods_json = excluded.moods_json,
                intensity = excluded.intensity,
                bpm = excluded.bpm,
                loopable = excluded.loopable,
                duration_ms = excluded.duration_ms,
                loudness_lufs = excluded.loudness_lufs,
                license = excluded.license,
                license_url = excluded.license_url,
                source = excluded.source,
                attribution_required = excluded.attribution_required,
                attribution_text = excluded.attribution_text,
                active = excluded.active,
                scan_error = excluded.scan_error,
                updated_at = excluded.updated_at
            """,
            (sha256, rel_path, kind, title, moods_json, intensity, bpm, loopable,
             duration_ms, loudness_lufs, license, license_url, source,
             attribution_required, attribution_text, active, scan_error, now, now),
        )
        row = self.db.connect().execute(
            "SELECT * FROM audio_library WHERE sha256 = ?", (sha256,)
        ).fetchone()
        assert row is not None
        return AudioLibraryRow.model_validate(row_to_dict(row))

    def get_library_entry(self, sha256: str) -> AudioLibraryRow | None:
        row = (
            self.db.connect().execute(
                "SELECT * FROM audio_library WHERE sha256 = ?", (sha256,)
            ).fetchone()
        )
        return AudioLibraryRow.model_validate(row_to_dict(row)) if row else None

    def list_library(
        self, *, kind: str | None = None, include_inactive: bool = False
    ) -> list[AudioLibraryRow]:
        clauses = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if not include_inactive:
            clauses.append("active = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = (
            self.db.connect()
            .execute(
                f"SELECT * FROM audio_library{where} ORDER BY kind, title, sha256", params  # noqa: S608
            )
            .fetchall()
        )
        return [AudioLibraryRow.model_validate(row_to_dict(r)) for r in rows]

    def set_library_active(self, sha256: str, *, active: bool, scan_error: str | None) -> None:
        self.db.connect().execute(
            "UPDATE audio_library SET active = ?, scan_error = ?, updated_at = ? WHERE sha256 = ?",
            (1 if active else 0, scan_error, self._now(), sha256),
        )

    # ---- cues -----------------------------------------------------------

    def replace_cues(
        self, deliverable_id: str, cues: list[NewAudioCue], plan_hash: str
    ) -> None:
        now = self._now()
        with self.db.tx(immediate=True) as conn:
            conn.execute("DELETE FROM audio_cues WHERE deliverable_id = ?", (deliverable_id,))
            for cue in cues:
                conn.execute(
                    """
                    INSERT INTO audio_cues(id, deliverable_id, cue_index, kind,
                                           library_sha256, scene_id, start_frame, end_frame,
                                           source_offset_ms, gain_db, fade_in_ms, fade_out_ms,
                                           params_json, plan_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._new_id(), deliverable_id, cue.cue_index, cue.kind,
                        cue.library_sha256, cue.scene_id, cue.start_frame, cue.end_frame,
                        cue.source_offset_ms, cue.gain_db, cue.fade_in_ms, cue.fade_out_ms,
                        cue.params_json, plan_hash, now,
                    ),
                )

    def list_cues(self, deliverable_id: str, *, kind: str | None = None) -> list[AudioCueRow]:
        if kind is None:
            rows = (
                self.db.connect()
                .execute(
                    "SELECT * FROM audio_cues WHERE deliverable_id = ? ORDER BY kind, cue_index",
                    (deliverable_id,),
                )
                .fetchall()
            )
        else:
            rows = (
                self.db.connect()
                .execute(
                    "SELECT * FROM audio_cues "
                    "WHERE deliverable_id = ? AND kind = ? ORDER BY cue_index",
                    (deliverable_id, kind),
                )
                .fetchall()
            )
        return [AudioCueRow.model_validate(row_to_dict(r)) for r in rows]

    def music_uses_in_series(self, series_id: str) -> list[tuple[str, str, str]]:
        """(episode_id, episode_created_at, library_sha256) for every music cue in the series."""
        rows = (
            self.db.connect()
            .execute(
                """
            SELECT DISTINCT e.id AS episode_id, e.created_at AS created_at,
                            c.library_sha256 AS library_sha256
            FROM audio_cues c
            JOIN deliverables d ON d.id = c.deliverable_id
            JOIN tracks t ON t.id = d.track_id
            JOIN episodes e ON e.id = t.episode_id
            WHERE e.series_id = ? AND c.kind = 'music'
            ORDER BY e.created_at
            """,
                (series_id,),
            )
            .fetchall()
        )
        return [
            (str(r["episode_id"]), str(r["created_at"]), str(r["library_sha256"])) for r in rows
        ]

    # ---- qc -------------------------------------------------------------

    def add_qc_result(
        self, *, deliverable_id: str | None, subject_scope: str, subject_id: str,
        check_name: str, status: str, detail_json: str,
    ) -> QcResultRow:
        now = self._now()
        qc_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO qc_results(id, deliverable_id, subject_scope, subject_id,
                                   check_name, status, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (qc_id, deliverable_id, subject_scope, subject_id,
             check_name, status, detail_json, now),
        )
        row = conn.execute("SELECT * FROM qc_results WHERE id = ?", (qc_id,)).fetchone()
        assert row is not None
        return QcResultRow.model_validate(row_to_dict(row))

    def list_qc_results(
        self, *, deliverable_id: str | None = None, subject_id: str | None = None,
        check_name: str | None = None,
    ) -> list[QcResultRow]:
        clauses: list[str] = []
        params: list[object] = []
        if deliverable_id is not None:
            clauses.append("deliverable_id = ?")
            params.append(deliverable_id)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if check_name is not None:
            clauses.append("check_name = ?")
            params.append(check_name)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = (
            self.db.connect()
            .execute(
                f"SELECT * FROM qc_results{where} ORDER BY created_at, id", params  # noqa: S608
            )
            .fetchall()
        )
        return [QcResultRow.model_validate(row_to_dict(r)) for r in rows]

    # ---- masters --------------------------------------------------------

    def get_ready_master(
        self, *, deliverable_id: str, input_hash: str
    ) -> AudioMasterRow | None:
        row = (
            self.db.connect()
            .execute(
                "SELECT * FROM audio_masters "
                "WHERE deliverable_id = ? AND input_hash = ? AND status = 'ready'",
                (deliverable_id, input_hash),
            )
            .fetchone()
        )
        return AudioMasterRow.model_validate(row_to_dict(row)) if row else None

    def insert_master(
        self, *, deliverable_id: str, input_hash: str, timeline_hash: str,
        master_sha256: str, narration_stem_sha256: str | None,
        total_samples: int, loudness_i: float | None, true_peak: float | None,
        lra: float | None, duck_depth_db: float | None, stats_json: str,
    ) -> AudioMasterRow:
        now = self._now()
        master_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO audio_masters(id, deliverable_id, input_hash, timeline_hash,
                                      master_sha256, narration_stem_sha256, total_samples,
                                      loudness_i, true_peak, lra, duck_depth_db,
                                      stats_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)
            ON CONFLICT(deliverable_id, input_hash) DO UPDATE SET
                timeline_hash = excluded.timeline_hash,
                master_sha256 = excluded.master_sha256,
                narration_stem_sha256 = excluded.narration_stem_sha256,
                total_samples = excluded.total_samples,
                loudness_i = excluded.loudness_i,
                true_peak = excluded.true_peak,
                lra = excluded.lra,
                duck_depth_db = excluded.duck_depth_db,
                stats_json = excluded.stats_json,
                status = 'ready'
            """,
            (master_id, deliverable_id, input_hash, timeline_hash, master_sha256,
             narration_stem_sha256, total_samples, loudness_i, true_peak, lra,
             duck_depth_db, stats_json, now),
        )
        row = conn.execute(
            "SELECT * FROM audio_masters WHERE deliverable_id = ? AND input_hash = ?",
            (deliverable_id, input_hash),
        ).fetchone()
        assert row is not None
        return AudioMasterRow.model_validate(row_to_dict(row))

    def supersede_masters(self, *, deliverable_id: str, keep_id: str) -> int:
        cur = self.db.connect().execute(
            "UPDATE audio_masters SET status = 'superseded' "
            "WHERE deliverable_id = ? AND id <> ? AND status = 'ready'",
            (deliverable_id, keep_id),
        )
        return cur.rowcount

    def list_masters(
        self, *, deliverable_id: str
    ) -> list[AudioMasterRow]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT * FROM audio_masters "
                "WHERE deliverable_id = ? ORDER BY created_at DESC, id DESC",
                (deliverable_id,),
            )
            .fetchall()
        )
        return [AudioMasterRow.model_validate(row_to_dict(r)) for r in rows]
