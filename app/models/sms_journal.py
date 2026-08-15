"""Encrypted, idempotent SMS observability journal."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SmsJournalApiRequest(Base):
    __tablename__ = "sms_journal_api_request"

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


class SmsJournalAttempt(Base):
    __tablename__ = "sms_journal_attempt"
    __table_args__ = (
        CheckConstraint(
            "encoding IN ('GSM-7','UCS-2')",
            name="ck_sms_journal_attempt_encoding",
        ),
        CheckConstraint(
            "send_status IN ('pending','accepted','failed','unknown')",
            name="ck_sms_journal_attempt_send_status",
        ),
        CheckConstraint(
            "delivery_status IN ('pending','delivered','undelivered','expired','unknown')",
            name="ck_sms_journal_attempt_delivery_status",
        ),
        Index("ix_sms_journal_attempt_created_at", "created_at"),
        Index("ix_sms_journal_attempt_source", "source_system", "source_entity_id"),
        Index("ix_sms_journal_attempt_provider_message", "provider", "provider_message_id"),
        Index("ix_sms_journal_attempt_phone_hash", "recipient_phone_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    create_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recipient_phone_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_phone_masked: Mapped[str] = mapped_column(String(32), nullable=False)
    message_text_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    message_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    contains_redacted_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    encoding: Mapped[str] = mapped_column(String(8), nullable=False)
    estimated_segments: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="megafon")
    sender_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    send_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    provider_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_error_detail_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    billed_segments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    reconciliation_period: Mapped[str | None] = mapped_column(String(7), nullable=True)
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
