from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CompetitorItemUrlAlias(Base):
    """Дополнительные URL одной позиции конкурента.

    Нужен для случаев, когда в прайсе лежит redirect-ссылка, а оператор ищет
    прямую карточку магазина.
    """

    __tablename__ = "competitor_item_url_alias"
    __table_args__ = (
        UniqueConstraint(
            "competitor",
            "normalized_url",
            name="uq_competitor_item_url_alias_competitor_url",
        ),
    )

    competitor_item_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    competitor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    alias_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    normalized_url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    url_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="stored")
    catalog_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    redirect_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    resolved_from_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    item = relationship("CompetitorItem", back_populates="url_aliases")
