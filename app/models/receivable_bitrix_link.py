from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.receivable_work import ReceivableWorkItem


class ReceivableBitrixLink(Base):
    __tablename__ = "receivable_bitrix_link"
    __table_args__ = (
        UniqueConstraint(
            "work_item_id", "contour_code", name="uq_receivable_bitrix_link_work_contour"
        ),
        UniqueConstraint(
            "contour_code",
            "entity_type_id",
            "item_id",
            name="uq_receivable_bitrix_link_contour_item",
        ),
        Index("ix_receivable_bitrix_link_work_item_id", "work_item_id"),
        Index("ix_receivable_bitrix_link_contour_code", "contour_code"),
    )

    work_item_id: Mapped[int] = mapped_column(
        ForeignKey("receivable_work_item.id", ondelete="CASCADE"), nullable=False
    )
    contour_code: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type_id: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    detail_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    stage_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    work_item: Mapped[ReceivableWorkItem] = relationship(back_populates="bitrix_links")
