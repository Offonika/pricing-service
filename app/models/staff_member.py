from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StaffMember(Base):
    __tablename__ = "staff_member"
    __table_args__ = (
        UniqueConstraint("source", "external_ref", name="uq_staff_member_source_external_ref"),
        Index("ix_staff_member_store_status", "store_ref", "employment_status"),
        Index("ix_staff_member_department_ref", "department_ref"),
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    role_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    department_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    department_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    store_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    store_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    employment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    hire_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    termination_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    manager_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
