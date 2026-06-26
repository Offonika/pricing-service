from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CounterpartyFolderSnapshot(Base):
    __tablename__ = "counterparty_folder_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_counterparty_folder_snapshot_date_ref",
        ),
        Index("ix_counterparty_folder_snapshot_date", "snapshot_date"),
        Index("ix_counterparty_folder_snapshot_counterparty_ref", "counterparty_ref"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_folder_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_folder_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
