from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class NomenclatureClassificationOperation(Base):
    __tablename__ = "nomenclature_classification_operation"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_nomenclature_classification_operation_id"),
        UniqueConstraint("command_hash", name="uq_nomenclature_classification_command_hash"),
        CheckConstraint(
            "state IN ("
            "'pending_dry_run','dry_run_sent','dry_run_ok','apply_sent','applying',"
            "'applied','failed','cancelled'"
            ")",
            name="ck_nomenclature_classification_state",
        ),
        Index("ix_nomenclature_classification_state_updated", "state", "updated_at"),
    )

    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_dry_run")
    approved_by: Mapped[str] = mapped_column(String(150), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(150), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    target: Mapped[str] = mapped_column(String(80), nullable=False)
    canonical_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    dry_run_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    apply_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    readback_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dry_run_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    apply_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    readback_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    dry_run_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    apply_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    apply_requested_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    apply_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    readback_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_result_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_result_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    items = relationship(
        "NomenclatureClassificationOperationItem",
        back_populates="operation",
        cascade="all, delete-orphan",
        order_by="NomenclatureClassificationOperationItem.id",
    )
    events = relationship(
        "NomenclatureClassificationOperationEvent",
        back_populates="operation",
        cascade="all, delete-orphan",
        order_by="NomenclatureClassificationOperationEvent.id",
    )


class NomenclatureClassificationOperationItem(Base):
    __tablename__ = "nomenclature_classification_operation_item"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_nomenclature_classification_idempotency"),
        UniqueConstraint(
            "active_nomenclature_key",
            name="uq_nomenclature_classification_active_nomenclature",
        ),
        Index(
            "ix_nomenclature_classification_item_operation",
            "operation_pk",
            "nomenclature_code",
        ),
    )

    operation_pk: Mapped[int] = mapped_column(
        ForeignKey("nomenclature_classification_operation.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nomenclature_code: Mapped[str] = mapped_column(String(64), nullable=False)
    nomenclature_guid: Mapped[str] = mapped_column(String(36), nullable=False)
    active_nomenclature_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    canonical_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    last_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    old_category_guids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    projected_category_guids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    readback_category_guids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    operation = relationship("NomenclatureClassificationOperation", back_populates="items")


class NomenclatureClassificationOperationEvent(Base):
    __tablename__ = "nomenclature_classification_operation_event"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_nomenclature_classification_event_key"),
        Index(
            "ix_nomenclature_classification_event_operation_created",
            "operation_pk",
            "created_at",
        ),
        Index("ix_nomenclature_classification_event_message", "message_id"),
    )

    operation_pk: Mapped[int] = mapped_column(
        ForeignKey("nomenclature_classification_operation.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    operation = relationship("NomenclatureClassificationOperation", back_populates="events")
