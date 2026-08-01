from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReceivableFolderChangeOperation(Base):
    __tablename__ = "receivable_folder_change_operation"
    __table_args__ = (
        UniqueConstraint(
            "active_counterparty_key",
            name="uq_receivable_folder_change_active_counterparty",
        ),
        CheckConstraint(
            "state IN ('draft','dry_run_sent','dry_run_ok','apply_sent','applied',"
            "'failed','needs_review')",
            name="ck_receivable_folder_change_state",
        ),
        Index("ix_receivable_folder_change_state_updated", "state", "updated_at"),
        Index("ix_receivable_folder_change_signal_key", "signal_key"),
    )

    signal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active_counterparty_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expected_old_folder_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_old_folder_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    proposed_new_folder_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_new_folder_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    signal_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    data_version: Mapped[str] = mapped_column(String(96), nullable=False)
    decision_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_by_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    dry_run_message_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    apply_message_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    readback_folder_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    readback_folder_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
