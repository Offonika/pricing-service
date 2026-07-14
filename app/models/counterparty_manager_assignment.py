from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.receivable_ledger_event import ReceivableLedgerEvent


class CounterpartyManagerAssignment(Base):
    __tablename__ = "counterparty_manager_assignment"
    __table_args__ = (
        UniqueConstraint("business_key", name="uq_counterparty_manager_assignment_business_key"),
        Index(
            "ix_counterparty_manager_assignment_counterparty_from",
            "counterparty_ref",
            "effective_from",
        ),
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="onec")
    business_key: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    manager_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    assignment_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("receivable_ledger_event.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    source_event: Mapped[Optional[ReceivableLedgerEvent]] = relationship(
        back_populates="manager_assignments"
    )
