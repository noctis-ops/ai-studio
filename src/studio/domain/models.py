"""Pydantic row models mirroring db/migrations/001_init.sql 1:1.

`*_json` columns stay raw JSON strings here; repositories decode them at the
boundary. Money columns are integer micro-USD, never floats.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SeriesRow(_Row):
    id: str
    name: str
    slug: str
    created_at: str


class CharacterRow(_Row):
    id: str
    series_id: str
    role: str
    name: str
    voices_json: str
    created_at: str


class EpisodeRow(_Row):
    id: str
    series_id: str
    title: str
    concept: str
    status: str
    languages_json: str
    aspects_json: str
    created_at: str


class TrackRow(_Row):
    id: str
    episode_id: str
    aspect: str
    created_at: str


class DeliverableRow(_Row):
    id: str
    track_id: str
    language: str
    channel_id: str
    created_at: str
    metadata_json: str | None = None
    safety_status: str = "pending"


class ShotRow(_Row):
    id: str
    track_id: str
    shot_no: int
    prompt: str
    created_at: str


class ShotAttemptRow(_Row):
    id: str
    shot_id: str
    attempt_no: int
    nonce: str
    prompt_used: str
    rejection_note: str | None
    created_at: str


class NarrationLineRow(_Row):
    id: str
    deliverable_id: str
    shot_id: str
    line_no: int
    text: str
    asset_sha256: str | None
    duration_ms: int | None
    created_at: str


class AssetRow(_Row):
    sha256: str
    rel_path: str
    kind: str
    mime: str
    size_bytes: int
    duration_ms: int | None
    width: int | None
    height: int | None
    retention: str
    created_at: str
    purged_at: str | None


class CacheEntryRow(_Row):
    cache_key: str
    capability: str
    model_key: str
    assets_json: str
    inline_json: str | None
    cost_musd: int
    created_at: str


class BudgetRow(_Row):
    id: str
    scope: str
    scope_ref: str
    cap_musd: int | None
    kill_switch_tripped: int
    tripped_at: str | None
    created_at: str
    updated_at: str


class LedgerEntryRow(_Row):
    id: str
    ts: str
    scope: str
    scope_ref: str
    entry_type: str
    amount_musd: int
    call_id: str | None


class ProviderCallRow(_Row):
    id: str
    episode_id: str | None
    stage_run_id: str | None
    capability: str
    model_key: str
    provider: str
    purpose: str
    cache_key: str
    request_json: str | None
    status: str
    attempt: int
    est_cost_musd: int
    actual_cost_musd: int | None
    remote_request_id: str | None
    submit_started_at: str | None
    error_class: str | None
    error_message: str | None
    created_at: str
    updated_at: str


class JobRow(_Row):
    id: str
    kind: str
    payload_json: str
    dedupe_key: str | None
    status: str
    attempt: int
    max_attempts: int
    run_after: str
    worker_id: str | None
    heartbeat_at: str | None
    stage_run_id: str | None
    created_at: str
    updated_at: str


class PipelineRunRow(_Row):
    id: str
    episode_id: str | None
    profile: str
    status: str
    auto_approve: int
    config_snapshot_json: str | None
    created_at: str
    updated_at: str


class StageRunRow(_Row):
    id: str
    run_id: str
    stage: str
    subject_scope: str
    subject_id: str
    lineage_json: str
    status: str
    input_hash: str | None
    output_json: str | None
    created_at: str
    updated_at: str


class GateRow(_Row):
    id: str
    run_id: str
    gate: str
    stage: str
    scope: str | None
    subject_id: str | None
    round: int
    status: str
    snapshot_hash: str | None
    note: str | None
    created_at: str
    decided_at: str | None


class AuditEventRow(_Row):
    id: str
    ts: str
    actor: str
    entity: str
    event: str
    payload_json: str
