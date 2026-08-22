from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
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


class ReceivableOpenDebtCache(Base):
    __tablename__ = "receivable_open_debt_cache"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_receivable_open_debt_cache_date_counterparty",
        ),
        Index("ix_receivable_open_debt_cache_snapshot_date", "snapshot_date"),
        Index("ix_receivable_open_debt_cache_department_ref", "department_ref"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    department_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    documents: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReceivablePkoShadowResult(Base):
    __tablename__ = "receivable_pko_shadow_result"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "algorithm_version",
            "counterparty_ref",
            name="uq_receivable_pko_shadow_date_version_counterparty",
        ),
        Index(
            "ix_receivable_pko_shadow_date_version",
            "snapshot_date",
            "algorithm_version",
        ),
        Index("ix_receivable_pko_shadow_run_id", "run_id"),
        Index("ix_receivable_pko_shadow_status", "status"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    department_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    department_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    base_payment_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    base_payment_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    base_payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    base_balance_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    current_origin_document_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_origin_document_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_origin_document_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    candidate_origin_document_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    candidate_origin_document_number: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    candidate_origin_document_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    candidate_responsible_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    candidate_responsible_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    candidate_origin_open_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    selected_open_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    delta: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_documents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    candidate_documents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReceivableFolderRecommendationCache(Base):
    __tablename__ = "receivable_folder_recommendation_cache"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "status_scope",
            name="uq_receivable_folder_recommendation_cache_date_status",
        ),
        Index("ix_receivable_folder_recommendation_cache_snapshot_date", "snapshot_date"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    status_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    report_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_status: Mapped[str] = mapped_column(String(32), nullable=False, default="cached")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReceivableBitrixUserAccess(Base):
    __tablename__ = "receivable_bitrix_user_access"
    __table_args__ = (
        UniqueConstraint("bitrix_user_id", name="uq_receivable_bitrix_user_access_user"),
        Index("ix_receivable_bitrix_user_access_active", "is_active"),
    )

    bitrix_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    access_level: Mapped[str] = mapped_column(String(32), nullable=False)
    department_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
