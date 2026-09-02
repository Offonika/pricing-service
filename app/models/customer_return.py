from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

SHIPMENT_STATUSES = (
    "registered",
    "in_transit",
    "arrived_at_pickup_point",
    "picked_up",
    "onec_return_confirmed",
    "cancelled",
    "exception",
)
ACTION_STATUSES = ("pending", "leased", "completed", "skipped", "failed")


class CustomerReturnShipment(Base):
    __tablename__ = "customer_return_shipment"
    __table_args__ = (
        UniqueConstraint(
            "carrier",
            "tracking_number",
            name="uq_customer_return_shipment_carrier_tracking",
        ),
        UniqueConstraint("source_ref", name="uq_customer_return_shipment_source_ref"),
        CheckConstraint(
            f"status IN {SHIPMENT_STATUSES!r}",
            name="ck_customer_return_shipment_status",
        ),
        Index("ix_customer_return_shipment_status_updated", "status", "updated_at"),
        Index("ix_customer_return_shipment_bitrix_case", "bitrix_case_id"),
        Index("ix_customer_return_shipment_bitrix_deal", "bitrix_deal_id"),
    )

    carrier: Mapped[str] = mapped_column(String(32), nullable=False)
    tracking_number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="registered", server_default="registered"
    )
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual", server_default="manual"
    )
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bitrix_case_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site_ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onec_order_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrix_deal_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bitrix_order_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_deal_stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_deal_stage_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bitrix_deal_closed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    bitrix_contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrix_contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bitrix_company_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrix_company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bitrix_responsible_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrix_responsible_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bitrix_deal_linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bitrix_deal_linked_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onec_return_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_bitrix_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    picked_up_by_bitrix_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    carrier_last_status_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    carrier_last_status_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    carrier_last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    storage_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onec_return_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    events: Mapped[list[CustomerReturnEvent]] = relationship(
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="CustomerReturnEvent.occurred_at, CustomerReturnEvent.id",
    )
    actions: Mapped[list[CustomerReturnAction]] = relationship(
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="CustomerReturnAction.due_at, CustomerReturnAction.id",
    )


class CustomerReturnEvent(Base):
    __tablename__ = "customer_return_event"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_customer_return_event_dedupe_key"),
        Index(
            "ix_customer_return_event_shipment_occurred",
            "shipment_id",
            "occurred_at",
            "id",
        ),
    )

    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("customer_return_shipment.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    carrier_status_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    carrier_status_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_bitrix_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    shipment: Mapped[CustomerReturnShipment] = relationship(back_populates="events")


class CustomerReturnAction(Base):
    __tablename__ = "customer_return_action"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_customer_return_action_dedupe_key"),
        CheckConstraint(
            f"status IN {ACTION_STATUSES!r}",
            name="ck_customer_return_action_status",
        ),
        Index(
            "ix_customer_return_action_due",
            "status",
            "next_attempt_at",
            "due_at",
            "id",
        ),
        Index("ix_customer_return_action_shipment", "shipment_id", "id"),
    )

    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("customer_return_shipment.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    shipment: Mapped[CustomerReturnShipment] = relationship(back_populates="actions")
