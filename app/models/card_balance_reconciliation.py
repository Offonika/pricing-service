from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
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


class CardBalanceCashbox(Base):
    __tablename__ = "card_balance_cashbox"
    __table_args__ = (
        UniqueConstraint("onec_cashbox_code", name="uq_card_balance_cashbox_onec_code"),
        Index("ix_card_balance_cashbox_card_last4", "card_last4"),
        Index("ix_card_balance_cashbox_employee_last_name", "employee_last_name"),
        Index("ix_card_balance_cashbox_is_active", "is_active"),
    )

    onec_cashbox_ref_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onec_cashbox_code: Mapped[str] = mapped_column(String(64), nullable=False)
    onec_cashbox_name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    currency_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employee_last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    needs_manual_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    review_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    reconciliations = relationship("CardBalanceReconciliation", back_populates="cashbox")


class CardBalanceReconciliation(Base):
    __tablename__ = "card_balance_reconciliation"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_card_balance_reconciliation_external_id"),
        UniqueConstraint("bitrix_item_id", name="uq_card_balance_reconciliation_bitrix_item_id"),
        Index("ix_card_balance_reconciliation_business_date", "business_date"),
        Index("ix_card_balance_reconciliation_status", "status"),
        Index("ix_card_balance_reconciliation_cashbox_date", "cashbox_id", "business_date"),
        Index("ix_card_balance_reconciliation_due_at", "due_at"),
    )

    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    cashbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("card_balance_cashbox.id", ondelete="SET NULL"),
        nullable=True,
    )
    employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    employee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employee_last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    onec_cashbox_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onec_cashbox_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_channel: Mapped[str] = mapped_column(
        String(32), nullable=False, default="bitrix", server_default="bitrix"
    )
    bitrix_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_stage_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    screenshot_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    screenshot_taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manual_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    recognized_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    recognition_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    onec_balance_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    onec_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    diff_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bitrix_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bitrix_last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    cashbox = relationship("CardBalanceCashbox", back_populates="reconciliations")
    events = relationship(
        "CardBalanceReconciliationEvent",
        back_populates="reconciliation",
        cascade="all, delete-orphan",
        order_by="CardBalanceReconciliationEvent.event_at.desc()",
    )


class CardBalanceReconciliationEvent(Base):
    __tablename__ = "card_balance_reconciliation_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_card_balance_event_idempotency_key"),
        Index(
            "ix_card_balance_event_reconciliation_at",
            "reconciliation_id",
            "event_at",
        ),
        Index("ix_card_balance_event_type", "event_type"),
    )

    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("card_balance_reconciliation.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    actor_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    reconciliation = relationship("CardBalanceReconciliation", back_populates="events")
