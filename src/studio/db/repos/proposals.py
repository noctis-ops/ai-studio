"""`bible_proposals` repository (migration 007).

A proposal is a *suggestion*: creating one never touches `series_bible`.
`decide` is the only status transition and it is one statement, so a crash can
never leave a proposal half-decided.
"""

from __future__ import annotations

from typing import Protocol

from studio.domain.models import BibleProposalRow

from .base import SqliteRepo, row_to_dict

_STATUSES = frozenset({"pending", "accepted", "rejected", "stale"})


class ProposalsRepo(Protocol):
    def create(
        self,
        *,
        episode_id: str,
        series_id: str,
        base_bible_version: int,
        proposal_json: str,
        provider_call_id: str | None = None,
    ) -> BibleProposalRow: ...

    def get(self, proposal_id: str) -> BibleProposalRow | None: ...

    def list_for_episode(
        self, episode_id: str, *, status: str | None = None
    ) -> list[BibleProposalRow]: ...

    def latest_for_episode(
        self, episode_id: str, *, status: str | None = None
    ) -> BibleProposalRow | None: ...

    def count_for_episode(self, episode_id: str) -> int: ...

    def decide(
        self, proposal_id: str, *, status: str, decided_at: str | None = None
    ) -> BibleProposalRow: ...


class SqliteProposalsRepo(SqliteRepo):
    def create(
        self,
        *,
        episode_id: str,
        series_id: str,
        base_bible_version: int,
        proposal_json: str,
        provider_call_id: str | None = None,
    ) -> BibleProposalRow:
        proposal_id = self._new_id()
        conn = self.db.connect()
        conn.execute(
            """
            INSERT INTO bible_proposals(id, episode_id, series_id, base_bible_version,
                                        status, proposal_json, provider_call_id, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                proposal_id,
                episode_id,
                series_id,
                base_bible_version,
                proposal_json,
                provider_call_id,
                self._now(),
            ),
        )
        row = self.get(proposal_id)
        assert row is not None
        return row

    def get(self, proposal_id: str) -> BibleProposalRow | None:
        row = (
            self.db.connect()
            .execute("SELECT * FROM bible_proposals WHERE id = ?", (proposal_id,))
            .fetchone()
        )
        return BibleProposalRow.model_validate(row_to_dict(row)) if row else None

    def list_for_episode(
        self, episode_id: str, *, status: str | None = None
    ) -> list[BibleProposalRow]:
        if status is not None and status not in _STATUSES:
            raise ValueError(f"unknown proposal status {status!r}")
        sql = "SELECT * FROM bible_proposals WHERE episode_id = ?"
        params: list[object] = [episode_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at, rowid"
        rows = self.db.connect().execute(sql, params).fetchall()
        return [BibleProposalRow.model_validate(row_to_dict(row)) for row in rows]

    def latest_for_episode(
        self, episode_id: str, *, status: str | None = None
    ) -> BibleProposalRow | None:
        rows = self.list_for_episode(episode_id, status=status)
        return rows[-1] if rows else None

    def count_for_episode(self, episode_id: str) -> int:
        row = (
            self.db.connect()
            .execute(
                "SELECT count(*) AS n FROM bible_proposals WHERE episode_id = ?",
                (episode_id,),
            )
            .fetchone()
        )
        return int(row["n"]) if row else 0

    def decide(
        self, proposal_id: str, *, status: str, decided_at: str | None = None
    ) -> BibleProposalRow:
        if status not in _STATUSES - {"pending"}:
            raise ValueError(f"cannot decide a proposal as {status!r}")
        conn = self.db.connect()
        conn.execute(
            "UPDATE bible_proposals SET status = ?, decided_at = ? WHERE id = ?",
            (status, decided_at or self._now(), proposal_id),
        )
        row = self.get(proposal_id)
        if row is None:
            raise ValueError(f"unknown proposal {proposal_id}")
        return row
