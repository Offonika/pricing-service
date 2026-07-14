from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProductPhoneModel(Base):
    __tablename__ = "product_phone_model"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "phone_model_id",
            "source",
            name="uq_product_phone_model_product_model_source",
        ),
    )

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    phone_model_id: Mapped[int] = mapped_column(
        ForeignKey("phone_models.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    raw_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    product = relationship("Product", back_populates="phone_model_links")
    phone_model = relationship("PhoneModel", back_populates="product_links")
