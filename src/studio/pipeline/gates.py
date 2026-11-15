"""GateService: gate lifecycle for human-approval gates.

Rounds: every time a gated stage completes with a new output snapshot, a new
approval_gates row is created (round = count + 1). The latest row is the
active one. Auto-approve is possible only when the profile is in
gates.allow_auto_approve_in AND the run was created with auto_approve=True;
`final` is always refused.
"""

from __future__ import annotations

from dataclasses import dataclass

from studio.core.clock import Clock
from studio.core.ids import Ids, UlidIds
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.db.repos.pipeline import subject_from_stage_run
from studio.domain.enums import GateKind, Profile, RunStatus, Scope
from studio.domain.errors import GatePolicyError
from studio.domain.models import GateRow
from studio.domain.refs import SubjectRef, subjects_related
from studio.pipeline.dag import Dag
from studio.pipeline.subjects import shot_subject


@dataclass(frozen=True)
class ShotRejection:
    shot_id: str
    edited_prompt: str | None = None


class GateService:
    def __init__(
        self,
        db: Database,
        repos: StudioRepos,
        clock: Clock,
        dag: Dag,
        ids: Ids | None = None,
    ) -> None:
        self.db = db
        self.repos = repos
        self.clock = clock
        self.dag = dag
        self.ids: Ids = ids if ids is not None else UlidIds()

    # ---- creation (called by the engine) -----------------------------------

    def create_gate_round(
        self,
        *,
        run_id: str,
        stage: str,
        gate: GateKind,
        snapshot_hash: str,
        profile: str,
        auto_approve: bool,
        allow_auto_approve_in: list[Profile],
    ) -> tuple[GateRow, bool]:
        """Returns (gate, auto_approved). Raises GatePolicyError on policy violations."""
        if auto_approve:
            if profile == Profile.FINAL.value:
                raise GatePolicyError(
                    "the final profile never auto-approves; human review is mandatory"
                )
            allowed = {p.value for p in allow_auto_approve_in}
            if profile not in allowed:
                raise GatePolicyError(
                    f"profile {profile!r} is not in gates.allow_auto_approve_in; no auto-approve"
                )
        round_no = self.repos.gates.count_rounds(run_id, stage) + 1
        scope: str | None = None
        subject_id: str | None = None
        created = self.repos.gates.create(
            run_id=run_id,
            gate=gate.value,
            stage=stage,
            scope=scope,
            subject_id=subject_id,
            round=round_no,
            snapshot_hash=snapshot_hash,
        )
        if auto_approve:
            decided = self.repos.gates.decide(
                created.id, status="auto_approved", note="auto-approved (profile policy)"
            )
            assert decided is not None
            self.repos.audit.append(
                actor="gates",
                entity=f"gate:{created.id}",
                event="gate_auto_approved",
                payload={
                    "run_id": run_id,
                    "stage": stage,
                    "gate": gate.value,
                    "round": round_no,
                    "profile": profile,
                },
            )
            return decided, True
        self.repos.audit.append(
            actor="gates",
            entity=f"gate:{created.id}",
            event="gate_created",
            payload={"run_id": run_id, "stage": stage, "gate": gate.value, "round": round_no},
        )
        return created, False

    # ---- decisions -----------------------------------------------------------

    def approve(self, gate_id: str, note: str | None = None) -> GateRow:
        gate = self._pending_gate(gate_id)
        decided = self.repos.gates.decide(gate.id, status="approved", note=note)
        assert decided is not None
        self.repos.audit.append(
            actor="operator",
            entity=f"gate:{gate.id}",
            event="gate_approved",
            payload={
                "run_id": gate.run_id,
                "stage": gate.stage,
                "gate": gate.gate,
                "round": gate.round,
                "note": note,
            },
        )
        self._set_run_running(gate.run_id)
        self._enqueue_advance(gate.run_id)
        return decided

    def request_changes(
        self,
        gate_id: str,
        note: str | None,
        rejections: list[ShotRejection],
    ) -> GateRow:
        if not rejections:
            raise GatePolicyError("request_changes requires at least one rejected shot")
        gate = self._pending_gate(gate_id)
        affected: list[str] = []
        downstream = sorted(self.dag.downstream(gate.stage))
        stages_to_stale = {gate.stage, *downstream}
        for rejection in rejections:
            shot = self.repos.content.get_shot(rejection.shot_id)
            if shot is None:
                raise GatePolicyError(f"unknown shot: {rejection.shot_id}")
            attempt = self.repos.content.current_shot_attempt(shot.id)
            next_no = (attempt.attempt_no if attempt else 0) + 1
            prompt_used = (
                rejection.edited_prompt
                if rejection.edited_prompt is not None
                else (attempt.prompt_used if attempt else shot.prompt)
            )
            self.repos.content.create_shot_attempt(
                shot_id=shot.id,
                attempt_no=next_no,
                nonce=self.ids.new(),
                prompt_used=prompt_used,
                rejection_note=note,
            )
            anchor = self._subject_for_shot(shot.id)
            for row in self.repos.pipeline.list_stage_runs(gate.run_id):
                if row.stage not in stages_to_stale:
                    continue
                if row.status not in ("succeeded", "pending", "running", "stale"):
                    continue
                other = subject_from_stage_run(row)
                if subjects_related(anchor, other):
                    affected.append(row.id)
        if affected:
            self.repos.pipeline.mark_stale(affected)
        decided = self.repos.gates.decide(gate.id, status="changes_requested", note=note)
        assert decided is not None
        self.repos.audit.append(
            actor="operator",
            entity=f"gate:{gate.id}",
            event="gate_changes_requested",
            payload={
                "run_id": gate.run_id,
                "stage": gate.stage,
                "gate": gate.gate,
                "round": gate.round,
                "note": note,
                "rejections": [
                    {"shot_id": r.shot_id, "edited_prompt": r.edited_prompt} for r in rejections
                ],
                "staled_stage_runs": sorted(set(affected)),
            },
        )
        self._set_run_running(gate.run_id)
        self._enqueue_advance(gate.run_id)
        return decided

    def list_pending(self, run_id: str | None = None) -> list[GateRow]:
        return self.repos.gates.list_pending(run_id)

    def rounds(self, run_id: str, stage: str) -> list[GateRow]:
        return [g for g in self.repos.gates.list_for_run(run_id) if g.stage == stage]

    # ---- internals -----------------------------------------------------------

    def _pending_gate(self, gate_id: str) -> GateRow:
        gate = self.repos.gates.get(gate_id)
        if gate is None:
            raise GatePolicyError(f"unknown gate: {gate_id}")
        if gate.status != "pending":
            raise GatePolicyError(f"gate {gate_id} is {gate.status}, expected pending")
        return gate

    def _set_run_running(self, run_id: str) -> None:
        # If the run halted on a gate earlier, approval releases it.
        self.repos.pipeline.set_run_status(run_id, RunStatus.RUNNING.value)
        run = self.repos.pipeline.get_run(run_id)
        if run is not None and run.episode_id:
            self.repos.content.set_episode_status(run.episode_id, RunStatus.RUNNING.value)

    def _enqueue_advance(self, run_id: str) -> None:
        self.repos.jobs.enqueue(
            kind="advance_run", payload={"run_id": run_id}, dedupe_key=f"advance:{run_id}"
        )

    def _subject_for_shot(self, shot_id: str) -> SubjectRef:
        shot = self.repos.content.get_shot(shot_id)
        if shot is None:
            raise GatePolicyError(f"unknown shot: {shot_id}")
        track = self.repos.content.get_track(shot.track_id)
        if track is None:
            raise GatePolicyError(f"shot {shot_id} references missing track {shot.track_id}")
        episode = self.repos.content.get_episode(track.episode_id)
        if episode is None:
            raise GatePolicyError(f"track {track.id} references missing episode {track.episode_id}")
        return shot_subject(episode.series_id, episode.id, track.id, shot.id)


def build_shot_anchor(repos: StudioRepos, shot_id: str) -> SubjectRef:
    """Helper for the CLI: build a shot SubjectRef with full lineage."""
    shot = repos.content.get_shot(shot_id)
    if shot is None:
        raise GatePolicyError(f"unknown shot: {shot_id}")
    track = repos.content.get_track(shot.track_id)
    assert track is not None
    episode = repos.content.get_episode(track.episode_id)
    assert episode is not None
    return shot_subject(episode.series_id, episode.id, track.id, shot.id)


def scope_value(scope: Scope | None) -> str | None:
    return scope.value if scope is not None else None
