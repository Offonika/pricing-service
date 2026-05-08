from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProductCompetitorItemDecision(Base):
    """Append-only audit log for manual competitor item matching decisions."""

    __tablename__ = "product_competitor_item_decision"

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    competitor_item_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    previous_product_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    previous_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    product = relationship("Product")
    competitor_item = relationship("CompetitorItem")
