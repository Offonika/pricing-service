from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
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
