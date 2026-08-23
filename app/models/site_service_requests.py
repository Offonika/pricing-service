from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class SiteServiceRequestCase(Base):
    __tablename__ = "site_service_request_case"
    __table_args__ = (
        UniqueConstraint(
            "source_ticket_id",
            name="uq_site_service_request_case_source_ticket",
        ),
        UniqueConstraint(
            "bitrix_item_id",
            name="uq_site_service_request_case_bitrix_item",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_site_service_request_case_version",
        ),
        CheckConstraint(
            "round_robin_seq >= 0",
            name="ck_site_service_request_case_round_robin_seq",
        ),
        CheckConstraint(
            "intake_mode IS NULL OR " "intake_mode IN ('during_open_shift', 'outside_open_shift')",
            name="ck_site_service_request_case_intake_mode",
        ),
        Index(
            "ix_site_service_request_case_assignment",
            "assignment_state",
            "assigned_user_id",
        ),
        Index(
            "ix_site_service_request_case_sync_status",
            "sync_status",
            "updated_at",
        ),
        Index(
            "ix_site_service_request_case_first_response_due",
            "first_response_due_at",
        ),
        Index(
            "ix_site_service_request_case_assignment_checked",
            "assignment_checked_at",
            "id",
        ),
        Index(
            "ix_site_service_request_case_outbound_checked",
            "outbound_checked_at",
            "id",
        ),
    )

    source_ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bitrix_item_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_open_stage_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    crm_contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    crm_company_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    crm_deal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    assigned_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assignment_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="waiting",
        server_default="waiting",
    )
    round_robin_seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    intake_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    first_response_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sla_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalation_timeline_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escalation_notification_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    latest_inbound_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latest_outbound_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_sync_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    base_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sync_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    assignment_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assignment_last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outbound_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    events: Mapped[list[SiteServiceRequestEvent]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )
    files: Mapped[list[SiteServiceRequestFile]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )
    commands: Mapped[list[SiteServiceRequestCommand]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )


class SiteServiceRequestEvent(Base):
    __tablename__ = "site_service_request_event"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_site_service_request_event_id"),
        CheckConstraint(
            "attempts >= 0",
            name="ck_site_service_request_event_attempts",
        ),
        Index(
            "ix_site_service_request_event_case_message",
            "case_id",
            "source_message_id",
        ),
        Index(
            "ix_site_service_request_event_processing",
            "status",
            "next_retry_at",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    case_id: Mapped[int] = mapped_column(
        ForeignKey("site_service_request_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case: Mapped[SiteServiceRequestCase] = relationship(back_populates="events")


class SiteServiceRequestFile(Base):
    __tablename__ = "site_service_request_file"
    __table_args__ = (
        UniqueConstraint(
            "source_message_id",
            "source_file_id",
            name="uq_site_service_request_file_source",
        ),
        CheckConstraint(
            "byte_size >= 0",
            name="ck_site_service_request_file_byte_size",
        ),
        Index(
            "ix_site_service_request_file_case_status",
            "case_id",
            "status",
        ),
    )

    case_id: Mapped[int] = mapped_column(
        ForeignKey("site_service_request_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_file_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    safe_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    bitrix_file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    temporary_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case: Mapped[SiteServiceRequestCase] = relationship(back_populates="files")


class SiteServiceRequestCommand(Base):
    __tablename__ = "site_service_request_command"
    __table_args__ = (
        UniqueConstraint(
            "command_key",
            name="uq_site_service_request_command_key",
        ),
        CheckConstraint(
            "status IN ('pending', 'leased', 'applied', 'failed')",
            name="ck_site_service_request_command_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_site_service_request_command_attempts",
        ),
        Index(
            "ix_site_service_request_command_lease",
            "status",
            "lease_until",
        ),
    )

    case_id: Mapped[int] = mapped_column(
        ForeignKey("site_service_request_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    command_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reply_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    reply_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    card_action_cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case: Mapped[SiteServiceRequestCase] = relationship(back_populates="commands")


class SiteServiceRequestNonce(Base):
    """Durable one-time HMAC nonce shared by all API workers."""

    __tablename__ = "site_service_request_nonce"
    __table_args__ = (
        UniqueConstraint("nonce", name="uq_site_service_request_nonce_nonce"),
        Index("ix_site_service_request_nonce_expires", "expires_at"),
    )

    nonce: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
