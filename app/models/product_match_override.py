from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProductMatchOverride(Base):
    competitor_source: Mapped[str] = mapped_column(String(128), nullable=False)
    competitor_sku: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    variant: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product.id"), nullable=True)
    phone_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phone_models.id"), nullable=True)
    quality: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    product = relationship("Product")
    phone_model = relationship("PhoneModel")

    __table_args__ = (
        UniqueConstraint(
            "competitor_source",
            "competitor_sku",
            name="uq_product_match_override_source_sku",
        ),
    )

