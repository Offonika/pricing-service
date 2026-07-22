"""HTTP contracts for the internal management orchestration API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Sha256 = str


class RunClaimRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    run_key: str = Field(min_length=1, max_length=255)
    scheduled_for: datetime
    input_sha256: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["shadow", "production", "replay"] = "production"
    worker_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class RunHeartbeatRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class RunFinishRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "partial", "failed", "skipped", "blocked"]
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    error_detail: str | None = Field(default=None, max_length=4000)


class RunResponse(BaseModel):
    run_id: UUID
    job_id: str
    run_key: str
    status: str
    should_execute: bool
    resource_replayed: bool
    idempotency_replayed: bool
    lease_expires_at: datetime
    completed_at: datetime | None = None


class DeliveryClaimRequest(BaseModel):
    run_id: UUID
    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    channel: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    dedupe_key: str = Field(min_length=1, max_length=255)
    target_fingerprint: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    payload_sha256: Sha256 = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    worker_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=300, ge=30, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)


class DeliveryFinishRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["delivered", "failed", "unknown", "manual_review", "cancelled"]
    external_ref: str | None = Field(default=None, max_length=255)
    error_code: str | None = Field(default=None, max_length=128)
    error_detail: str | None = Field(default=None, max_length=4000)


class DeliveryResponse(BaseModel):
    intent_id: UUID
    run_id: UUID
    job_id: str
    channel: str
    dedupe_key: str
    status: str
    should_send: bool
    attempts_count: int
    manual_review_required: bool
    external_ref: str | None = None
    idempotency_replayed: bool


class OrchestrationHealthResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checked_at: datetime
    run_status_counts: dict[str, int]
    delivery_status_counts: dict[str, int]
    stale_run_count: int
    manual_review_count: int
