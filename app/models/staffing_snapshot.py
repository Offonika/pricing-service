from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, Date, DateTime, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StaffingSnapshot(Base):
    __tablename__ = "staffing_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "store_ref",
            "shift_code",
            name="uq_staffing_snapshot_date_store_shift",
        ),
        Index("ix_staffing_snapshot_snapshot_date", "snapshot_date"),
        Index("ix_staffing_snapshot_store_ref", "store_ref"),
        Index("ix_staffing_snapshot_criticality", "criticality"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    store_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    store_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    shift_code: Mapped[str] = mapped_column(String(32), nullable=False)
    planned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    no_show_count: Mapped[int] = mapped_column(Integer, nullable=False)
    deficit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    criticality: Mapped[str] = mapped_column(String(16), nullable=False)
    deficit_role_counts: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
