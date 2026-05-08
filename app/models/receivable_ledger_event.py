from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.counterparty_manager_assignment import CounterpartyManagerAssignment
    from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot


class ReceivableLedgerEvent(Base):
    __tablename__ = "receivable_ledger_event"
    __table_args__ = (
        UniqueConstraint("business_key", name="uq_receivable_ledger_event_business_key"),
        Index(
            "ix_receivable_ledger_event_counterparty_date",
            "counterparty_ref",
            "external_document_date",
        ),
        Index("ix_receivable_ledger_event_event_type", "event_type"),
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="onec")
    business_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_document_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    external_document_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    external_document_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contract_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    contract_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contract_kind_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    contract_kind_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    store_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    store_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_layer: Mapped[str] = mapped_column(
        String(32), nullable=False, default="regular_receivables"
    )
    planned_payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    credit_depth_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    shipment_ban: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    line_no: Mapped[Optional[int]] = mapped_column(nullable=True)
    amount_delta: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    manager_assignments: Mapped[list[CounterpartyManagerAssignment]] = relationship(
        back_populates="source_event"
    )
    origin_snapshots: Mapped[list[ReceivableBalanceSnapshot]] = relationship(
        back_populates="origin_event"
    )
