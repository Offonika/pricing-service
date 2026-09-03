from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrderAssemblyQueueItem(Base):
    __tablename__ = "order_assembly_queue_item"
    __table_args__ = (
        Index("ix_order_assembly_queue_item_order_number", "order_number"),
        Index(
            "ix_order_assembly_queue_item_priority",
            "urgent",
            "assembly_due_at",
            "stage_entered_at",
        ),
    )

    deal_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    order_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    crm_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivery_method: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assembly_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    urgent_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgent_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OrderAssemblyQueueSyncState(Base):
    __tablename__ = "order_assembly_queue_sync_state"

    source: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OrderAssemblyCrmOutbox(Base):
    __tablename__ = "order_assembly_crm_outbox"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_order_assembly_crm_outbox_event_key"),
        Index("ix_order_assembly_crm_outbox_status_next", "status", "next_attempt_at"),
        Index("ix_order_assembly_crm_outbox_order", "site_order_number", "event_at"),
    )

    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    crm_status: Mapped[str] = mapped_column(String(32), nullable=False, default="assembled")
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assembly_source: Mapped[str] = mapped_column(String(32), nullable=False)
    assembly_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    onec_order_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site_order_number: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(2), nullable=False)
    delivery_code: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    crm_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
