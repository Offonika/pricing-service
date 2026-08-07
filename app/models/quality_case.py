from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class QualityCase(Base):
    __tablename__ = "quality_case"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_quality_case_external_id"),
        UniqueConstraint("source_return_line_key", name="uq_quality_case_source_return_line_key"),
        Index("ix_quality_case_status_due_at", "current_status", "due_at"),
        Index("ix_quality_case_nomenclature_return_at", "nomenclature_code", "return_at"),
        Index("ix_quality_case_confirmed_product_defect", "counts_as_confirmed_product_defect"),
    )

    external_id: Mapped[str] = mapped_column(String(96), nullable=False)
    source_return_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    source_return_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_return_line_key: Mapped[str] = mapped_column(String(160), nullable=False)
    return_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    nomenclature_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nomenclature_code: Mapped[str] = mapped_column(String(64), nullable=False)
    nomenclature_name: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    store_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preliminary_quality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preliminary_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_status: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    final_decision_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disposition_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    decision_author_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    onec_quality_correction_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    counts_as_confirmed_product_defect: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    events = relationship(
        "QualityCaseEvent",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="QualityCaseEvent.event_at.desc()",
    )


class QualityCaseEvent(Base):
    __tablename__ = "quality_case_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_quality_case_event_idempotency_key"),
        Index("ix_quality_case_event_case_event_at", "quality_case_id", "event_at"),
        Index("ix_quality_case_event_type", "event_type"),
    )

    quality_case_id: Mapped[int] = mapped_column(
        ForeignKey("quality_case.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    actor_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    case = relationship("QualityCase", back_populates="events")
