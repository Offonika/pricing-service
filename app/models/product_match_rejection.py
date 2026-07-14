from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProductMatchRejection(Base):
    __tablename__ = "product_match_rejection"

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitor.id"), nullable=False)
    competitor_sku: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "competitor_id",
            name="uq_product_match_rejection_product_competitor",
        ),
    )
