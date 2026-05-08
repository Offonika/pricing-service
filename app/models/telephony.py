from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TelephonyUserLineSnapshot(Base):
    __tablename__ = "telephony_user_line_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "user_ref_hex",
            name="uq_telephony_user_line_snapshot_date_user",
        ),
        Index("ix_telephony_user_line_snapshot_snapshot_date", "snapshot_date"),
        Index("ix_telephony_user_line_snapshot_extension", "extension"),
        Index(
            "ix_telephony_user_line_snapshot_snapshot_extension",
            "snapshot_date",
            "extension",
        ),
        Index(
            "ix_telephony_user_line_snapshot_snapshot_status",
            "snapshot_date",
            "employment_status",
        ),
        Index("ix_telephony_user_line_snapshot_bitrix_user_id", "bitrix_user_id"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    mapping_source: Mapped[str] = mapped_column(String(64), nullable=False)
    user_ref_hex: Mapped[str] = mapped_column(String(64), nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    physical_person_ref_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    physical_person_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    computer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    store_ref_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department_ref_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employment_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    staff_store_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    staff_store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    staff_department_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    staff_department_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bitrix_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mdm_employee_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_marked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_extension: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_bitrix: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
