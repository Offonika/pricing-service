from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, Date, DateTime, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReceivableCase(Base):
    __tablename__ = "receivable_case"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "segment",
            "counterparty_ref",
            name="uq_receivable_case_date_segment_counterparty",
        ),
        Index("ix_receivable_case_snapshot_date", "snapshot_date"),
        Index("ix_receivable_case_segment", "segment"),
        Index("ix_receivable_case_owner_type", "owner_type"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    segment: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(255), nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    aged_bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    activity_segment: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_document_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_document_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_document_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    origin_manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    department_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    department_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    planned_payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    credit_depth_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    shipment_ban: Mapped[Optional[bool]] = mapped_column(nullable=True)
    payment_term_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    overdue_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    is_overdue: Mapped[bool] = mapped_column(nullable=False, default=False)
    chain_documents: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
