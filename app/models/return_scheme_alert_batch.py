from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.return_scheme_incident import ReturnSchemeIncident


class ReturnSchemeAlertBatch(Base):
    __tablename__ = "return_scheme_alert_batch"
    __table_args__ = (
        Index("ix_return_scheme_alert_batch_status_generated_at", "status", "generated_at"),
    )

    generated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    new_incidents_count: Mapped[int] = mapped_column(nullable=False, default=0)
    notification_incidents_count: Mapped[int] = mapped_column(nullable=False, default=0)
    report_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    incidents: Mapped[list[ReturnSchemeIncident]] = relationship(back_populates="alert_batch")
