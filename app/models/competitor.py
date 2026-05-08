from __future__ import annotations

from typing import List, Optional

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.competitor_price import CompetitorPrice
from app.models.product_match import ProductMatch


class Competitor(Base):
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    website: Mapped[Optional[str]] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)

    prices: Mapped[List[CompetitorPrice]] = relationship(
        back_populates="competitor", cascade="all, delete-orphan"
    )
    matches: Mapped[List[ProductMatch]] = relationship(
        "ProductMatch", back_populates="competitor", cascade="all, delete-orphan"
    )
