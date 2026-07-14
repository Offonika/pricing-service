from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CompetitorItemCompatibility(Base):
    """
    Совместимость товара конкурента с моделями телефонов (может быть несколько моделей на одну позицию).
    """

    __tablename__ = "competitor_item_compatibility"
    __table_args__ = (
        UniqueConstraint(
            "competitor_item_id",
            "device_brand",
            "device_model",
            "device_variant",
            name="uq_comp_item_compat_item_device_brand_model_variant",
        ),
    )

    competitor_item_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_item.id", ondelete="CASCADE"), index=True
    )
    phone_model_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("phone_models.id"), index=True, nullable=True
    )
    device_brand_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("device_brands.id"), index=True, nullable=True
    )
    device_brand: Mapped[str] = mapped_column(String(128), nullable=False)
    device_model: Mapped[str] = mapped_column(String(255), nullable=False)
    device_variant: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # parser/llm/manual
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    item = relationship("CompetitorItem", back_populates="compatibilities")
    phone_model = relationship("PhoneModel", back_populates="competitor_compatibilities")
    device_brand_ref = relationship("DeviceBrand", back_populates="competitor_compatibilities")
