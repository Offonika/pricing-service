from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProductSkuPlan(Base):
    __tablename__ = "product_sku_plan"
    __table_args__ = (
        Index(
            "uq_product_sku_plan_active_product",
            "product_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
        Index(
            "uq_product_sku_plan_active_planned_sku",
            "planned_sku",
            unique=True,
            sqlite_where=text("is_active = 1 AND planned_sku IS NOT NULL"),
            postgresql_where=text("is_active AND planned_sku IS NOT NULL"),
        ),
        Index("ix_product_sku_plan_product_id", "product_id"),
        Index("ix_product_sku_plan_planned_sku", "planned_sku"),
    )

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), nullable=False)
    planned_sku: Mapped[Optional[str]] = mapped_column(String(35), nullable=True)
    brand_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    category_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    device_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    key_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rev: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_review")
    error_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="rules")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    product = relationship("Product", back_populates="sku_plans")
