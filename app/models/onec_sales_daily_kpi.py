from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OneCSalesDailyKpi(Base):
    __tablename__ = "onec_sales_daily_kpi"
    __table_args__ = (
        UniqueConstraint(
            "sales_date",
            "manager_ref",
            "store_ref",
            name="uq_onec_sales_daily_kpi_date_manager_store",
        ),
        Index("ix_onec_sales_daily_kpi_sales_date", "sales_date"),
        Index("ix_onec_sales_daily_kpi_manager_ref", "manager_ref"),
        Index("ix_onec_sales_daily_kpi_store_ref", "store_ref"),
    )

    sales_date: Mapped[date] = mapped_column(Date, nullable=False)
    manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    store_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    store_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    revenue: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    cost_of_sales: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    sales_count: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
