from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CompetitorItem(Base):
    competitor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(1024))
    category: Mapped[Optional[str]] = mapped_column(String(255))
    price_opt: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    price_roz: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    availability: Mapped[Optional[bool]] = mapped_column(Boolean)
    url: Mapped[Optional[str]] = mapped_column(String(1024))
    scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    snapshots: Mapped[List["CompetitorItemSnapshot"]] = relationship(
        "CompetitorItemSnapshot",
        back_populates="item",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("competitor", "external_id", name="uq_competitor_item_competitor_ext"),
    )


class CompetitorItemSnapshot(Base):
    competitor_item_id: Mapped[int] = mapped_column(
        ForeignKey("competitoritem.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    price_opt: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    price_roz: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    availability: Mapped[Optional[bool]] = mapped_column(Boolean)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    item: Mapped["CompetitorItem"] = relationship("CompetitorItem", back_populates="snapshots")

