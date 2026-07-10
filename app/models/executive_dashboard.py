from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, Date, DateTime, Index, Numeric, String, UniqueConstraint, func
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
    source_freshness: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
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
    responsible_bitrix_user_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
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
