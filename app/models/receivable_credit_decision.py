from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReceivableCreditDecisionOperation(Base):
    __tablename__ = "receivable_credit_decision_operation"
    __table_args__ = (
        UniqueConstraint(
            "bitrix_entity_type_id",
            "bitrix_item_id",
            "decision_hash",
            name="uq_receivable_credit_decision_item_hash",
        ),
        UniqueConstraint(
            "active_counterparty_key",
            name="uq_receivable_credit_decision_active_counterparty",
        ),
        CheckConstraint(
            "state IN ("
            "'pending_dry_run','dry_run_sent','dry_run_ok','apply_sent','applying',"
            "'applied','failed','cancelled'"
            ")",
            name="ck_receivable_credit_decision_state",
        ),
        CheckConstraint(
            "expected_current_limit >= 0 AND proposed_limit >= 0",
            name="ck_receivable_credit_decision_nonnegative_limits",
        ),
        CheckConstraint(
            "expected_current_depth >= 0 AND proposed_depth >= 0",
            name="ck_receivable_credit_decision_nonnegative_depths",
        ),
        Index(
            "ix_receivable_credit_decision_state_updated",
            "state",
            "updated_at",
        ),
        Index(
            "ix_receivable_credit_decision_counterparty",
            "counterparty_key",
            "created_at",
        ),
    )

    bitrix_entity_type_id: Mapped[int] = mapped_column(nullable=False)
    bitrix_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bitrix_category_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    bitrix_stage_id: Mapped[str] = mapped_column(String(96), nullable=False)
    bitrix_revision: Mapped[str] = mapped_column(String(96), nullable=False)
    moved_by_user_id: Mapped[str] = mapped_column(String(32), nullable=False)

    decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_key: Mapped[str] = mapped_column(String(96), nullable=False)
    active_counterparty_key: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_guid: Mapped[str] = mapped_column(String(36), nullable=False)
    counterparty_code: Mapped[str] = mapped_column(String(32), nullable=False)
    counterparty_name: Mapped[str] = mapped_column(String(255), nullable=False)

    expected_current_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    expected_current_depth: Mapped[int] = mapped_column(nullable=False)
    proposed_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    proposed_depth: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_dry_run")
    dry_run_message_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    apply_message_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    readback_message_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    dry_run_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    apply_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    readback_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_result_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_result_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dry_run_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    apply_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    readback_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    readback_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    readback_depth: Mapped[Optional[int]] = mapped_column(nullable=True)
    bitrix_sync_pending: Mapped[bool] = mapped_column(nullable=False, default=False)
    source_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
