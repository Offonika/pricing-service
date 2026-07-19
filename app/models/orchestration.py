"""Persistent state for management job and delivery orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrchestrationApiRequest(Base):
    __tablename__ = "orchestration_api_request"

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


class OrchestrationJobRun(Base):
    __tablename__ = "orchestration_job_run"
    __table_args__ = (
        UniqueConstraint("job_id", "run_key", name="uq_orchestration_job_run_key"),
        CheckConstraint(
            "status IN ('claimed','running','succeeded','partial','failed','skipped','blocked')",
            name="ck_orchestration_job_run_status",
        ),
        CheckConstraint(
            "mode IN ('shadow','production','replay')",
            name="ck_orchestration_job_run_mode",
        ),
        Index("ix_orchestration_job_run_status_lease", "status", "lease_expires_at"),
        Index("ix_orchestration_job_run_scheduled_for", "scheduled_for"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_key: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrchestrationDeliveryIntent(Base):
    __tablename__ = "orchestration_delivery_intent"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "channel",
            "dedupe_key",
            name="uq_orchestration_delivery_intent_dedupe",
        ),
        CheckConstraint(
            "status IN ('pending','sending','delivered','failed','unknown','manual_review','cancelled')",
            name="ck_orchestration_delivery_intent_status",
        ),
        Index(
            "ix_orchestration_delivery_intent_status_lease",
            "status",
            "lease_expires_at",
        ),
        Index("ix_orchestration_delivery_intent_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("orchestration_job_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrchestrationDeliveryAttempt(Base):
    __tablename__ = "orchestration_delivery_attempt"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "attempt_number",
            name="uq_orchestration_delivery_attempt_number",
        ),
        Index("ix_orchestration_delivery_attempt_intent_id", "intent_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("orchestration_delivery_intent.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
