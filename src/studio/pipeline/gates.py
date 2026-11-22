"""GateService: gate lifecycle for human-approval gates.

Rounds: every time a gated stage completes with a new output snapshot, a new
approval_gates row is created (round = count + 1). The latest row is the
active one. Auto-approve is possible only when the profile is in
gates.allow_auto_approve_in AND the run was created with auto_approve=True;
`final` is always refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from studio.core.canonical import canonical_json
from studio.core.clock import Clock
from studio.core.ids import Ids, UlidIds
from studio.db.connection import Database
from studio.db.repos import StudioRepos
from studio.db.repos.pipeline import subject_from_stage_run
from studio.domain.enums import GateKind, Profile, RunStatus, Scope
from studio.domain.errors import GatePolicyError
from studio.domain.models import GateRow
from studio.domain.refs import SubjectRef, subjects_related
from studio.pipeline.checklist import normalize_checklist
from studio.pipeline.dag import Dag
from studio.pipeline.subjects import shot_subject

_SHEETS_STAGE = "s8a_character_sheets"
_RENDER_QC_STAGE = "s12_render_qc"


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
        presented: dict[str, object] | None = None,
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
            presented_json=(canonical_json(presented).decode() if presented is not None else None),
        )
        if auto_approve:
            # Phase 5 §J: a machine-filled G3 checklist is recorded as such;
            # S13's final blockers later refuse auto-made attestations.
            auto_checklist = (
                canonical_json(normalize_checklist(None, profile=profile, auto=True)).decode()
                if gate is GateKind.G3_FINAL_CUT
                else None
            )
            decided = self.repos.gates.decide(
                created.id,
                status="auto_approved",
                note="auto-approved (profile policy)",
                checklist_json=auto_checklist,
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
            self._after_gate_approved(decided)
            return decided, True
        self.repos.audit.append(
            actor="gates",
            entity=f"gate:{created.id}",
            event="gate_created",
            payload={"run_id": run_id, "stage": stage, "gate": gate.value, "round": round_no},
        )
        return created, False

    # ---- decisions -----------------------------------------------------------

    def approve(
        self,
        gate_id: str,
        note: str | None = None,
        checklist: dict[str, object] | None = None,
    ) -> GateRow:
        gate = self._pending_gate(gate_id)
        checklist_json: str | None = None
        if gate.gate == GateKind.G3_FINAL_CUT.value:
            run = self.repos.pipeline.get_run(gate.run_id)
            profile = run.profile if run is not None else Profile.FINAL.value
            checklist_json = canonical_json(
                normalize_checklist(checklist, profile=profile, auto=False)
            ).decode()
        elif checklist:
            raise GatePolicyError(
                f"gate {gate_id} ({gate.gate}) does not take an approval checklist"
            )
        decided = self.repos.gates.decide(
            gate.id, status="approved", note=note, checklist_json=checklist_json
        )
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
                "checklist": (
                    normalize_checklist(json.loads(checklist_json), profile="draft")
                    if checklist_json
                    else None
                ),
            },
        )
        self._after_gate_approved(decided)
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
            raise GatePolicyError("request_changes requires at least one rejected item")
        gate = self._pending_gate(gate_id)
        if gate.stage == _SHEETS_STAGE:
            return self._request_changes_sheets(gate, note, rejections)
        affected: list[str] = []
        # Phase 5 (§E2): a G2 rejection re-runs the *keyframe* stage, not the
        # animatic: only the named shots are re-paid, and the rebuilt animatic
        # creates the next G2 round.
        spec = self.dag.spec(gate.stage)
        reject_stage = spec.gate_reject_stage or gate.stage
        downstream = sorted(self.dag.downstream(reject_stage))
        stages_to_stale = {reject_stage, *downstream}
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

    def _series_for_run(self, run_id: str) -> str | None:
        run = self.repos.pipeline.get_run(run_id)
        if run is None or not run.episode_id:
            return None
        episode = self.repos.content.get_episode(run.episode_id)
        return episode.series_id if episode is not None else None

    def _after_gate_approved(self, gate: GateRow) -> None:
        """Side effects of approval that live outside the generic gate row.

        G0 (character sheets): flip the newest candidate rows to approved and
        supersede older ones, atomically.
        """
        if gate.stage != _SHEETS_STAGE:
            return
        series_id = self._series_for_run(gate.run_id)
        if series_id is None:
            return
        approved = self.repos.sheets.approve_latest_candidates(series_id)
        self.repos.audit.append(
            actor="gates",
            entity=f"gate:{gate.id}",
            event="sheets_approved",
            payload={
                "run_id": gate.run_id,
                "gate_id": gate.id,
                "series_id": series_id,
                "approved": [f"{row.character_id}:{row.role}" for row in approved],
            },
        )

    def _request_changes_sheets(
        self,
        gate: GateRow,
        note: str | None,
        rejections: list[ShotRejection],
    ) -> GateRow:
        """G0 reject: mark the named sheet rows rejected and stale the S8a runs.

        `rejections[*].shot_id` carries the sheet row id. Only the rejected
        (character, role) pairs regenerate on the next advance, with a new
        nonce and the usual cost check; approved characters are untouched.
        """
        series_id = self._series_for_run(gate.run_id)
        if series_id is None:
            raise GatePolicyError(f"run {gate.run_id} has no resolvable series")
        rejected: list[str] = []
        stale_subjects: list[str] = []
        for rejection in rejections:
            sheet = self.repos.sheets.get(rejection.shot_id)
            if sheet is None:
                raise GatePolicyError(f"unknown character sheet: {rejection.shot_id}")
            if sheet.series_id != series_id:
                raise GatePolicyError(
                    f"sheet {sheet.id} belongs to a different series"
                )
            rejected.append(sheet.id)
            stale_subjects.append(f"{sheet.character_id}::{sheet.role}")
        count = self.repos.sheets.reject(rejected)
        # Stale only the rejected sheets' own stage runs. Downstream (keyframes,
        # clips) stales automatically: regenerated sheet -> new sha -> changed
        # input_hash (rule 4). The new sheet candidate parks the run at G0
        # round 2 before any paid downstream stage can run.
        affected: list[str] = []
        for row in self.repos.pipeline.list_stage_runs(gate.run_id, stage=gate.stage):
            if row.status not in ("succeeded", "pending", "running", "stale"):
                continue
            if row.subject_id in stale_subjects:
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
                "rejected_sheets": rejected,
                "sheets_rejected": count,
                "staled_stage_runs": sorted(set(affected)),
            },
        )
        self._set_run_running(gate.run_id)
        self._enqueue_advance(gate.run_id)
        return decided

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
