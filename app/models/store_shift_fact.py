from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StoreShiftFact(Base):
    __tablename__ = "store_shift_fact"
    __table_args__ = (
        UniqueConstraint("business_key", name="uq_store_shift_fact_business_key"),
        Index("ix_store_shift_fact_shift_date", "shift_date"),
        Index("ix_store_shift_fact_store_shift", "store_ref", "shift_code"),
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    business_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_shift_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_code: Mapped[str] = mapped_column(String(32), nullable=False)
    store_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    store_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    role_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    staff_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    staff_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attendance_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_start_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    actual_end_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
