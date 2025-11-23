from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, JSON, Numeric, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class PriceRecommendation(Base):
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    strategy_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("pricingstrategyversion.id"), nullable=True
    )
    recommended_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    floor_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    competitor_min_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    min_margin_pct: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    reasons: Mapped[List[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product")
    strategy_version = relationship("PricingStrategyVersion")

    __table_args__ = (
        Index("ix_price_recommendation_product_created", "product_id", "created_at"),
    )
