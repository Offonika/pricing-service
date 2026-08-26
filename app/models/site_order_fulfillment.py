from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class SiteOrderExecutionCase(Base):
    __tablename__ = "site_order_execution_case"
    __table_args__ = (
        UniqueConstraint("site_order_number", name="uq_site_order_execution_case_order"),
        Index("ix_site_order_execution_case_status", "current_derived_status"),
        Index("ix_site_order_execution_case_delivery", "delivery_method"),
    )

    site_order_number: Mapped[str] = mapped_column(String(32), nullable=False)
    bitrix_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    onec_order_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rtu_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_delivery_method: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_derived_status: Mapped[str] = mapped_column(String(64), nullable=False)
    current_crm_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_evidence_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    events = relationship(
        "SiteOrderExecutionEvent",
        back_populates="case",
        cascade="all, delete-orphan",
        foreign_keys="SiteOrderExecutionEvent.case_id",
    )


class BitrixChatMessage(Base):
    __tablename__ = "bitrix_chat_message"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_bitrix_chat_message_identity"),
        Index("ix_bitrix_chat_message_chat_code_at", "chat_code", "message_at"),
    )

    chat_code: Mapped[str] = mapped_column(String(64), nullable=False)
    dialog_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_text_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    mentions = relationship(
        "BitrixChatMention",
        back_populates="message",
        cascade="all, delete-orphan",
    )


class BitrixChatMention(Base):
    __tablename__ = "bitrix_chat_mention"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "site_order_number",
            "event_type",
            name="uq_bitrix_chat_mention_order_event",
        ),
        Index("ix_bitrix_chat_mention_order", "site_order_number"),
    )

    message_id: Mapped[int] = mapped_column(
        ForeignKey("bitrix_chat_message.id", ondelete="CASCADE"),
        nullable=False,
    )
    site_order_number: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_text: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    message = relationship("BitrixChatMessage", back_populates="mentions")


class SiteOrderExecutionEvent(Base):
    __tablename__ = "site_order_execution_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_site_order_execution_event_idempotency"),
        Index("ix_site_order_execution_event_case_at", "case_id", "event_at"),
        Index("ix_site_order_execution_event_type", "event_type"),
    )

    case_id: Mapped[int] = mapped_column(
        ForeignKey("site_order_execution_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("bitrix_chat_message.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    case = relationship(
        "SiteOrderExecutionCase",
        back_populates="events",
        foreign_keys=[case_id],
    )
    raw_message = relationship("BitrixChatMessage")


class SiteOrderStageOutbox(Base):
    __tablename__ = "site_order_stage_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_site_order_stage_outbox_event"),
        UniqueConstraint("idempotency_key", name="uq_site_order_stage_outbox_idempotency"),
        Index("ix_site_order_stage_outbox_status_next", "status", "next_attempt_at"),
        Index("ix_site_order_stage_outbox_case_id", "case_id", "id"),
    )

    case_id: Mapped[int] = mapped_column(
        ForeignKey("site_order_execution_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("site_order_execution_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    site_order_number: Mapped[str] = mapped_column(String(32), nullable=False)
    bitrix_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_live_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timeline_written_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    case = relationship("SiteOrderExecutionCase")
    event = relationship("SiteOrderExecutionEvent")
