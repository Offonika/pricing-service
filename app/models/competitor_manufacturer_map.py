from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CompetitorManufacturerMap(Base):
    __tablename__ = "competitor_manufacturer_map"
    __table_args__ = (
        UniqueConstraint("competitor", "raw_label", name="uq_competitor_manufacturer_map"),
    )

    competitor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    raw_label: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_manufacturer: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
