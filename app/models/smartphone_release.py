from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReleaseStatus(str, enum.Enum):
    RUMOR = "rumor"
    ANNOUNCED = "announced"
    RELEASED = "released"


class SourceType(str, enum.Enum):
    NEWS_API = "news_api"
    CATALOG = "catalog"
    MANUAL = "manual"


class SmartphoneRelease(Base):
    __tablename__ = "smartphone_releases"
    __table_args__ = (
        UniqueConstraint("source_name", "source_url", name="uq_smartphone_release_source"),
        UniqueConstraint("brand", "model", "announcement_date", name="uq_smartphone_release_identity"),
        Index("ix_smartphone_releases_brand", "brand"),
        Index("ix_smartphone_releases_model", "model"),
        Index("ix_smartphone_releases_announcement_date", "announcement_date"),
    )

    brand: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(150), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    announcement_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    release_status: Mapped[Optional[ReleaseStatus]] = mapped_column(
        Enum(
            ReleaseStatus,
            name="smartphone_release_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=False,
        ),
        nullable=True,
    )
    source_type: Mapped[SourceType] = mapped_column(
        Enum(
            SourceType,
            name="smartphone_release_source",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=False,
        ),
        default=SourceType.NEWS_API.value,
        nullable=False,
    )
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_ru: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    market_release_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    market_release_date_ru: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    raw_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
