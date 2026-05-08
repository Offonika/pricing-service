from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ExpertiseCase(Base):
    __tablename__ = "expertise_case"
    __table_args__ = (
        Index("ix_expertise_case_current_status", "current_status"),
        Index("ix_expertise_case_store_external_id", "store_external_id"),
        Index("ix_expertise_case_owner_user_external_id", "owner_user_external_id"),
        Index("ix_expertise_case_due_at", "due_at"),
        Index("ix_expertise_case_client_notified", "client_notified"),
        UniqueConstraint("external_id", name="uq_expertise_case_external_id"),
        UniqueConstraint("onec_expertise_ref", name="uq_expertise_case_onec_expertise_ref"),
    )

    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    onec_expertise_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onec_expertise_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    organization_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_sale_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_sale_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    problem_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    current_status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    linked_customer_order_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_customer_order_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    owner_user_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_disk_folder_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_disk_folder_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    bitrix_notify_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bitrix_last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    events = relationship(
        "ExpertiseCaseEvent",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="ExpertiseCaseEvent.event_at.desc()",
    )
    attachments = relationship(
        "ExpertiseCaseAttachment",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="ExpertiseCaseAttachment.created_at.desc()",
    )


class ExpertiseCaseEvent(Base):
    __tablename__ = "expertise_case_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_expertise_case_event_idempotency_key"),
        Index("ix_expertise_case_event_case_event_at", "expertise_case_id", "event_at"),
        Index("ix_expertise_case_event_event_type", "event_type"),
    )

    expertise_case_id: Mapped[int] = mapped_column(
        ForeignKey("expertise_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    actor_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    case = relationship("ExpertiseCase", back_populates="events")


class ExpertiseCaseAttachment(Base):
    __tablename__ = "expertise_case_attachment"
    __table_args__ = (
        Index("ix_expertise_case_attachment_case_id", "expertise_case_id"),
        Index("ix_expertise_case_attachment_kind", "attachment_kind"),
    )

    expertise_case_id: Mapped[int] = mapped_column(
        ForeignKey("expertise_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    attachment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    case = relationship("ExpertiseCase", back_populates="attachments")
