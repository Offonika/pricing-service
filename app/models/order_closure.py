from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
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


class OrderClosureBatch(Base):
    __tablename__ = "order_closure_batch"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_order_closure_batch_public_id"),
        CheckConstraint(
            "status IN ('draft','diagnosed','approved','leased','applied','stale','failed','canceled')",
            name="ck_order_closure_batch_status",
        ),
        CheckConstraint(
            "command_kind IS NULL OR command_kind IN ('diagnose','apply')",
            name="ck_order_closure_batch_command_kind",
        ),
        Index("ix_order_closure_batch_status_created", "status", "created_at"),
        Index("ix_order_closure_batch_lease", "lease_until", "lease_token"),
    )

    public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    diagnosis_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    command_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list[OrderClosureItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="OrderClosureItem.position"
    )
    events: Mapped[list[OrderClosureEvent]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class OrderClosureItem(Base):
    __tablename__ = "order_closure_item"
    __table_args__ = (
        UniqueConstraint("batch_id", "position", name="uq_order_closure_item_position"),
        Index("ix_order_closure_item_batch_status", "batch_id", "status"),
        Index("ix_order_closure_item_order_ref", "onec_order_ref"),
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("order_closure_batch.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    input_number: Mapped[str] = mapped_column(String(64), nullable=False)
    input_period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    onec_order_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    onec_order_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onec_order_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    site_order_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    department_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocker_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    blocker_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    state_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result_document_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result_document_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    batch: Mapped[OrderClosureBatch] = relationship(back_populates="items")


class OrderClosureEvent(Base):
    __tablename__ = "order_closure_event"
    __table_args__ = (Index("ix_order_closure_event_batch_created", "batch_id", "created_at"),)

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("order_closure_batch.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_closure_item.id", ondelete="CASCADE"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    batch: Mapped[OrderClosureBatch] = relationship(back_populates="events")
