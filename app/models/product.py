from typing import List, Optional

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Product(Base):
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    brand: Mapped[Optional[str]] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(100))
    abc_class: Mapped[Optional[str]] = mapped_column(String(5))
    xyz_class: Mapped[Optional[str]] = mapped_column(String(5))
    is_active: Mapped[bool] = mapped_column(default=True)

    competitor_prices: Mapped[List["CompetitorPrice"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    stock: Mapped[Optional["ProductStock"]] = relationship(
        "ProductStock", back_populates="product", cascade="all, delete-orphan", uselist=False
    )
    matches: Mapped[List["ProductMatch"]] = relationship(
        "ProductMatch", back_populates="product", cascade="all, delete-orphan"
    )
