from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReceivableReconciliationSnapshot(Base):
    __tablename__ = "receivable_reconciliation_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_receivable_reconciliation_snapshot_date_counterparty",
        ),
        Index(
            "ix_receivable_reconciliation_snapshot_snapshot_date",
            "snapshot_date",
        ),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    signed_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    absolute_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    current_manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
