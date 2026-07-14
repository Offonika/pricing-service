from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ReturnSchemeIncident(Base):
    __tablename__ = "return_scheme_incident"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_return_scheme_incident_fingerprint"),
        Index("ix_return_scheme_incident_notified_at", "notified_at"),
        Index("ix_return_scheme_incident_store_ref", "store_ref"),
        Index("ix_return_scheme_incident_product_ref", "product_ref"),
        Index("ix_return_scheme_incident_second_sale_doc_datetime", "second_sale_doc_datetime"),
    )

    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    product_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    product_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    store_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    store_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    manager_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manager_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    first_sale_doc_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    first_sale_doc_number: Mapped[str] = mapped_column(String(64), nullable=False)
    first_sale_doc_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    return_doc_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    return_doc_number: Mapped[str] = mapped_column(String(64), nullable=False)
    return_doc_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    second_sale_doc_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    second_sale_doc_number: Mapped[str] = mapped_column(String(64), nullable=False)
    second_sale_doc_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    second_price_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    matched_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    alert_batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("return_scheme_alert_batch.id", ondelete="SET NULL"),
        nullable=True,
    )

    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    alert_batch = relationship("ReturnSchemeAlertBatch", back_populates="incidents")
