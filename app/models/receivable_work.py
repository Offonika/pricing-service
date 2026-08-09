from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ReceivableWorkItem(Base):
    __tablename__ = "receivable_work_item"
    __table_args__ = (
        UniqueConstraint("stable_key", name="uq_receivable_work_item_stable_key"),
        Index("ix_receivable_work_item_counterparty_ref", "counterparty_ref"),
        Index("ix_receivable_work_item_current_debt_key", "current_debt_key"),
        Index("ix_receivable_work_item_status", "status"),
        Index("ix_receivable_work_item_bitrix_item_id", "bitrix_item_id"),
    )

    stable_key: Mapped[str] = mapped_column(String(160), nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new_debt")

    current_debt_key: Mapped[Optional[str]] = mapped_column(String(220), nullable=True)
    current_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    origin_document_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_document_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_document_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    overdue_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    age_days: Mapped[Optional[int]] = mapped_column(nullable=True)

    origin_manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    origin_manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    department_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    department_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    phone_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")

    bitrix_item_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    bitrix_stage_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    bitrix_last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    bitrix_last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bitrix_detail_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    assigned_bitrix_user_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    assigned_source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    needs_call_today: Mapped[bool] = mapped_column(nullable=False, default=False)

    last_sms_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_sms_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_sms_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    last_contact_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    promised_payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_action_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_manager_update_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    escalated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    escalation_level: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    chain_documents: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list[ReceivableWorkEvent]] = relationship(
        back_populates="work_item",
        cascade="all, delete-orphan",
    )
    sms_logs: Mapped[list[ReceivableSmsLog]] = relationship(back_populates="work_item")
    supervisor_notes: Mapped[list[ReceivableSupervisorNote]] = relationship(
        back_populates="work_item",
        cascade="all, delete-orphan",
    )


class ReceivableWorkEvent(Base):
    __tablename__ = "receivable_work_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_receivable_work_event_idempotency_key"),
        Index("ix_receivable_work_event_work_item_id", "work_item_id"),
        Index("ix_receivable_work_event_event_type", "event_type"),
        Index("ix_receivable_work_event_event_at", "event_at"),
    )

    work_item_id: Mapped[int] = mapped_column(
        ForeignKey("receivable_work_item.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="automation")
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    work_item: Mapped[ReceivableWorkItem] = relationship(back_populates="events")


class ReceivableSupervisorNote(Base):
    __tablename__ = "receivable_supervisor_note"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('personal', 'shared')",
            name="ck_receivable_supervisor_note_visibility",
        ),
        UniqueConstraint(
            "work_item_id",
            "author_bitrix_user_id",
            "visibility",
            name="uq_receivable_supervisor_note_author_visibility",
        ),
        Index("ix_receivable_supervisor_note_work_item_id", "work_item_id"),
        Index("ix_receivable_supervisor_note_author", "author_bitrix_user_id"),
        Index("ix_receivable_supervisor_note_visibility", "visibility"),
    )

    work_item_id: Mapped[int] = mapped_column(
        ForeignKey("receivable_work_item.id", ondelete="CASCADE"), nullable=False
    )
    author_bitrix_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    author_name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    work_item: Mapped[ReceivableWorkItem] = relationship(back_populates="supervisor_notes")


class ReceivableSmsLog(Base):
    __tablename__ = "receivable_sms_log"
    __table_args__ = (
        UniqueConstraint("debt_key", "business_date", name="uq_receivable_sms_log_debt_date"),
        Index("ix_receivable_sms_log_work_item_id", "work_item_id"),
        Index("ix_receivable_sms_log_stable_key", "stable_key"),
        Index("ix_receivable_sms_log_counterparty_ref", "counterparty_ref"),
        Index("ix_receivable_sms_log_debt_key", "debt_key"),
        Index("ix_receivable_sms_log_status", "status"),
    )

    work_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("receivable_work_item.id", ondelete="SET NULL"), nullable=True
    )
    stable_key: Mapped[str] = mapped_column(String(160), nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    debt_key: Mapped[str] = mapped_column(String(220), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    work_item: Mapped[Optional[ReceivableWorkItem]] = relationship(back_populates="sms_logs")
