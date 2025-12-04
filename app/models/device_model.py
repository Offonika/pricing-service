from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class PhoneModel(Base):
    __tablename__ = "phone_models"
    __table_args__ = (UniqueConstraint("brand", "model_name", "variant", name="uq_phone_model_identity"),)

    brand: Mapped[str] = mapped_column(String(100), index=True)
    model_name: Mapped[str] = mapped_column(String(150), index=True)
    variant: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    announce_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    release_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    screen_size_inch: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    screen_technology: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    screen_refresh_rate_hz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    keywords: Mapped[List["Keyword"]] = relationship("Keyword", back_populates="phone_model", cascade="all, delete-orphan")


class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = (UniqueConstraint("phone_model_id", "phrase", name="uq_keyword_phrase_per_model"),)

    phrase: Mapped[str] = mapped_column(String(255), index=True)
    language: Mapped[str] = mapped_column(String(10), default="ru")
    category: Mapped[str] = mapped_column(String(50), default="display")
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    phone_model_id: Mapped[int] = mapped_column(ForeignKey("phone_models.id"), index=True)
    phone_model: Mapped[PhoneModel] = relationship("PhoneModel", back_populates="keywords")

    demand_stats: Mapped[List["KeywordDemand"]] = relationship(
        "KeywordDemand", back_populates="keyword", cascade="all, delete-orphan"
    )


class KeywordDemand(Base):
    __tablename__ = "keyword_demands"
    __table_args__ = (UniqueConstraint("keyword_id", "date", "region", name="uq_keyword_demand_date_region"),)

    keyword_id: Mapped[int] = mapped_column(ForeignKey("keywords.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    region: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    impressions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    clicks: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ctr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_metrics: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    source: Mapped[str] = mapped_column(String(50), default="yandex_direct")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    keyword: Mapped[Keyword] = relationship("Keyword", back_populates="demand_stats")
