from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExecutiveDashboardSnapshot(Base):
    __tablename__ = "executive_dashboard_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "revision",
            name="uq_executive_dashboard_snapshot_date_revision",
        ),
        Index("ix_executive_dashboard_snapshot_date_status", "snapshot_date", "status"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_freshness: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutiveActionItem(Base):
    __tablename__ = "executive_action_item"
    __table_args__ = (
        UniqueConstraint("stable_key", name="uq_executive_action_item_stable_key"),
        UniqueConstraint("dedupe_key", name="uq_executive_action_item_dedupe_key"),
        Index("ix_executive_action_item_business_date_status", "business_date", "status"),
        Index("ix_executive_action_item_domain_severity", "domain", "severity"),
        Index(
            "ix_executive_action_item_responsible",
            "responsible_bitrix_user_id",
            "status",
        ),
    )

    stable_key: Mapped[str] = mapped_column(String(160), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="medium")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    responsible_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    drilldown_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutiveSourceFreshness(Base):
    __tablename__ = "executive_source_freshness"
    __table_args__ = (
        UniqueConstraint(
            "source_key",
            "business_date",
            name="uq_executive_source_freshness_key_date",
        ),
        Index("ix_executive_source_freshness_status", "source_status"),
    )

    source_key: Mapped[str] = mapped_column(String(120), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_as_of: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    max_lag_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutiveManagementBalanceSnapshot(Base):
    __tablename__ = "executive_management_balance_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "period_month",
            "view_mode",
            "version",
            name="uq_executive_management_balance_period_view_version",
        ),
        Index(
            "ix_executive_management_balance_period_status",
            "period_month",
            "status",
        ),
    )

    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    balance_date: Mapped[date] = mapped_column(Date, nullable=False)
    view_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    source_status: Mapped[str] = mapped_column(String(32), nullable=False, default="partial")
    freshness_status: Mapped[str] = mapped_column(String(32), nullable=False, default="partial")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    assets_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    liabilities_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    equity_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    imbalance_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    source_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_by: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    close_note: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutiveManagementBalanceLine(Base):
    __tablename__ = "executive_management_balance_line"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "section", "line_key", name="uq_executive_management_balance_line"
        ),
        Index("ix_executive_management_balance_line_snapshot", "snapshot_id", "display_order"),
    )

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("executive_management_balance_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    )
    section: Mapped[str] = mapped_column(String(24), nullable=False)
    line_key: Mapped[str] = mapped_column(String(96), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source_status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_as_of: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class ExecutiveManagementBalanceAudit(Base):
    __tablename__ = "executive_management_balance_audit"
    __table_args__ = (
        Index("ix_executive_management_balance_audit_snapshot", "snapshot_id", "created_at"),
    )

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("executive_management_balance_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
