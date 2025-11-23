from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CompetitorPrice(Base):
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitor.id"), index=True)
    price: Mapped[float] = mapped_column(Numeric(12, 2))
    in_stock: Mapped[bool] = mapped_column(default=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product", back_populates="competitor_prices")
    competitor = relationship("Competitor", back_populates="prices")

    __table_args__ = (
        Index(
            "ix_competitor_price_product_competitor_date",
            "product_id",
            "competitor_id",
            "collected_at",
        ),
    )
