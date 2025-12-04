from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CompetitorFtpFile(Base):
    __tablename__ = "competitor_ftp_file"

    source: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_date: Mapped[date] = mapped_column(Date, nullable=False)
    mtime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_valid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_invalid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    date_mismatch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    rows = relationship(
        "CompetitorFtpRawRow",
        back_populates="file",
        cascade="all, delete-orphan",
    )
    records = relationship(
        "CompetitorFtpRecord",
        back_populates="file",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("source", "file_date", name="uq_competitor_ftp_file_source_date"),
    )


class CompetitorFtpRawRow(Base):
    __tablename__ = "competitor_ftp_raw_row"

    file_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_ftp_file.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(255))
    sku: Mapped[Optional[str]] = mapped_column(String(255))
    name: Mapped[Optional[str]] = mapped_column(String(1024))
    price_opt: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    price_roz: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    link: Mapped[Optional[str]] = mapped_column(String(1024))
    stock: Mapped[Optional[bool]] = mapped_column(Boolean)
    amount: Mapped[Optional[int]] = mapped_column(Integer)
    observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    file = relationship("CompetitorFtpFile", back_populates="rows")
    record = relationship("CompetitorFtpRecord", back_populates="raw_row", uselist=False)


class CompetitorFtpRecord(Base):
    __tablename__ = "competitor_ftp_record"

    raw_row_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_ftp_raw_row.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_ftp_file.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(255))
    sku: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(1024))
    price_opt: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    price_roz: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    link: Mapped[Optional[str]] = mapped_column(String(1024))
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    amount: Mapped[Optional[int]] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    raw_row = relationship("CompetitorFtpRawRow", back_populates="record")
    file = relationship("CompetitorFtpFile", back_populates="records")

