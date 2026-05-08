from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CounterpartyDuplicateCase(Base):
    __tablename__ = "counterparty_duplicate_case"
    __table_args__ = (
        Index("ix_counterparty_duplicate_case_dedupe_key", "dedupe_key", unique=True),
        Index(
            "ix_counterparty_duplicate_case_delivery_state_detected_at",
            "delivery_state",
            "detected_at",
        ),
        Index("ix_counterparty_duplicate_case_last_seen_at", "last_seen_at"),
    )

    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    candidate_records: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    responsible_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    sla_deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    external_case_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    external_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    external_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
