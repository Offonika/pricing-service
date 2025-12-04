from typing import Optional

from sqlalchemy import ForeignKey, String, UniqueConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProductMatch(Base):
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitor.id"), nullable=False)
    competitor_sku: Mapped[Optional[str]] = mapped_column(String(128))
    confidence: Mapped[float] = mapped_column(default=1.0)
    is_manual: Mapped[bool] = mapped_column(default=False)
    phone_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phone_models.id"), nullable=True, index=True)
    quality: Mapped[Optional[str]] = mapped_column(String(50))

    product = relationship("Product", back_populates="matches")
    competitor = relationship("Competitor", back_populates="matches")
    phone_model = relationship("PhoneModel")

    __table_args__ = (
        UniqueConstraint("product_id", "competitor_id", name="uq_product_match_product_competitor"),
    )
