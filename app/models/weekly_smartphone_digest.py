from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WeeklySmartphoneDigest(Base):
    __tablename__ = "weekly_smartphone_digest"
    __table_args__ = (
        UniqueConstraint("week_start", "week_end", name="uq_weekly_smartphone_digest_period"),
    )

    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    week_end: Mapped[date] = mapped_column(Date, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_chars: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    release_ids: Mapped[Optional[List[int]]] = mapped_column(JSON, nullable=True)
    stats: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
