"""Framework-independent orchestration entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class ApiRequest:
    idempotency_key: str
    operation: str
    request_sha256: str
    response_payload: dict[str, Any]


@dataclass(slots=True)
class JobRun:
    id: UUID
    job_id: str
    run_key: str
    scheduled_for: datetime
    input_sha256: str
    mode: str
    status: str
    worker_id: str
    claimed_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime
    completed_at: datetime | None = None
    result_payload: dict[str, Any] | None = None
    result_sha256: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(slots=True)
class DeliveryIntent:
    id: UUID
    run_id: UUID
    job_id: str
    channel: str
    dedupe_key: str
    target_fingerprint: str
    payload_sha256: str
    artifact_refs: list[str]
    status: str
    attempts_count: int = 0
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    external_ref: str | None = None
    result_sha256: str | None = None
    last_error_code: str | None = None
    last_error_detail: str | None = None


@dataclass(slots=True)
class DeliveryAttempt:
    id: UUID
    intent_id: UUID
    attempt_number: int
    worker_id: str
    started_at: datetime
    completed_at: datetime | None = None
    outcome: str | None = None
    external_ref: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(slots=True)
class OrchestrationHealth:
    run_status_counts: dict[str, int] = field(default_factory=dict)
    delivery_status_counts: dict[str, int] = field(default_factory=dict)
    stale_run_count: int = 0
