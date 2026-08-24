from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
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
    pickup_point_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    storage_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    storage_deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class BitrixChatActionCandidate(Base):
    __tablename__ = "bitrix_chat_action_candidate"
    __table_args__ = (
        UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            "site_order_number",
            name="uq_bitrix_chat_action_candidate_source_order",
        ),
        UniqueConstraint("nonce", name="uq_bitrix_chat_action_candidate_nonce"),
        Index("ix_bitrix_chat_action_candidate_status_expires", "status", "expires_at"),
        Index("ix_bitrix_chat_action_candidate_order", "site_order_number"),
    )

    raw_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("bitrix_chat_message.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_author_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    site_order_number: Mapped[str] = mapped_column(String(32), nullable=False)
    bitrix_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detected_action: Mapped[str] = mapped_column(String(64), nullable=False)
    pickup_point_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    pickup_point_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    active_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bot_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dry_run: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
    )
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    raw_message = relationship("BitrixChatMessage")
    pickup_point = relationship("LogisticsWarehouse")
    actions = relationship(
        "BitrixChatAction",
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class BitrixChatAction(Base):
    __tablename__ = "bitrix_chat_action"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_bitrix_chat_action_idempotency"),
        Index("ix_bitrix_chat_action_candidate_at", "candidate_id", "created_at"),
        Index("ix_bitrix_chat_action_actor_at", "actor_id", "created_at"),
    )

    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("bitrix_chat_action_candidate.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmation_step: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    before_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    candidate = relationship("BitrixChatActionCandidate", back_populates="actions")


class SiteOrderFulfillmentOutbox(Base):
    __tablename__ = "site_order_fulfillment_outbox"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_site_order_fulfillment_outbox_idempotency",
        ),
        Index("ix_site_order_fulfillment_outbox_status_available", "status", "available_at"),
    )

    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("bitrix_chat_action_candidate.id", ondelete="SET NULL"),
        nullable=True,
    )
    action_id: Mapped[int | None] = mapped_column(
        ForeignKey("bitrix_chat_action.id", ondelete="SET NULL"),
        nullable=True,
    )
    depends_on_id: Mapped[int | None] = mapped_column(
        ForeignKey("site_order_fulfillment_outbox.id", ondelete="SET NULL"),
        nullable=True,
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=8,
        server_default="8",
    )
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    candidate = relationship("BitrixChatActionCandidate")
    action = relationship("BitrixChatAction")
    depends_on = relationship(
        "SiteOrderFulfillmentOutbox", remote_side="SiteOrderFulfillmentOutbox.id"
    )
