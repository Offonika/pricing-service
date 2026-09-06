from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProcurementException(Base):
    __tablename__ = "procurement_exception"
    __table_args__ = (Index("ix_procurement_exception_status_due", "status", "response_due_at"),)

    stable_key: Mapped[str] = mapped_column(String(255), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("procurement_order_formation.id"))
    line_id: Mapped[int | None] = mapped_column(ForeignKey("procurement_order_formation_line.id"))
    reason_code: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="new")
    version: Mapped[int] = mapped_column(default=1)
    facts_hash: Mapped[str] = mapped_column(String(64))
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    response_due_at: Mapped[datetime] = mapped_column(DateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    assigned_user_id: Mapped[str | None] = mapped_column(String(64))
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_facts_hash: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
