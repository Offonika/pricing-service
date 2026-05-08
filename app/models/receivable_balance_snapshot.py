from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.receivable_ledger_event import ReceivableLedgerEvent


class ReceivableBalanceSnapshot(Base):
    __tablename__ = "receivable_balance_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_receivable_balance_snapshot_date_counterparty",
        ),
        Index("ix_receivable_balance_snapshot_snapshot_date", "snapshot_date"),
        Index("ix_receivable_balance_snapshot_activity_segment", "activity_segment"),
        Index("ix_receivable_balance_snapshot_aged_bucket", "aged_bucket"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    origin_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("receivable_ledger_event.id", ondelete="SET NULL"),
        nullable=True,
    )
    origin_document_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_document_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_document_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    origin_manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_sale_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_payment_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    planned_payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    credit_depth_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    shipment_ban: Mapped[Optional[bool]] = mapped_column(nullable=True)
    payment_term_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    overdue_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    is_overdue: Mapped[bool] = mapped_column(nullable=False, default=False)
    aged_bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    activity_segment: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    origin_event: Mapped[Optional[ReceivableLedgerEvent]] = relationship(
        back_populates="origin_snapshots"
    )
