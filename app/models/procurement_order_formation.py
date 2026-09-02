from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProcurementOrderFormation(Base):
    __tablename__ = "procurement_order_formation"
    __table_args__ = (
        UniqueConstraint("stable_key", name="uq_proc_order_formation_stable_key"),
        UniqueConstraint(
            "bitrix_entity_type_id",
            "bitrix_item_id",
            name="uq_proc_order_formation_bitrix_item",
        ),
        Index("ix_proc_order_formation_status", "status"),
        Index("ix_proc_order_formation_lifecycle", "lifecycle_status", "order_date"),
        Index("ix_proc_order_formation_origin", "origin", "order_date"),
        Index("ix_proc_order_formation_onec_ref", "onec_document_ref"),
        Index("ix_proc_order_formation_supplier", "supplier_ref", "supplier_code"),
        Index("ix_proc_order_formation_onec_status", "onec_status"),
    )

    stable_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="generated")
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    bitrix_entity_type_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    bitrix_item_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bitrix_category_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    bitrix_stage_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    bitrix_stage_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    bitrix_item_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    bitrix_link_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    bitrix_link_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    supplier_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    supplier_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(500), nullable=False)
    contract_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    contract_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    contract_name: Mapped[str] = mapped_column(String(500), nullable=False)
    warehouse_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    warehouse_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    warehouse_name: Mapped[str] = mapped_column(String(500), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    procurement_contour: Mapped[str] = mapped_column(String(64), nullable=False, default="ordinary")
    route: Mapped[str] = mapped_column(String(128), nullable=False, default="ordinary")
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)

    responsible_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    responsible_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    calculation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    source_run_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)

    approved_version: Mapped[Optional[int]] = mapped_column(nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by_actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    approved_by_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    onec_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_sent")
    onec_message_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    onec_document_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    onec_document_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    onec_document_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    onec_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    onec_posted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    onec_marked: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    supplier_dispatch_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cargo_dropoff_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expected_receipt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    onec_ordered_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 3), nullable=True)
    onec_open_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 3), nullable=True)
    onec_received_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 3), nullable=True)
    onec_snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_onec_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_onec_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sync_conflict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    label_onec_document_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    label_onec_document_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    label_source_linked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    lines: Mapped[list[ProcurementOrderFormationLine]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="ProcurementOrderFormationLine.line_number",
    )
    events: Mapped[list[ProcurementOrderFormationEvent]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="ProcurementOrderFormationEvent.created_at.desc()",
    )


class ProcurementOrderFormationLine(Base):
    __tablename__ = "procurement_order_formation_line"
    __table_args__ = (
        UniqueConstraint("stable_key", name="uq_proc_order_line_stable_key"),
        UniqueConstraint("order_id", "line_number", name="uq_proc_order_line_order_number"),
        Index("ix_proc_order_line_order", "order_id", "removed"),
        Index("ix_proc_order_line_onec_ref", "nomenclature_ref"),
        Index("ix_proc_order_line_bitrix_product", "bitrix_product_id"),
    )

    order_id: Mapped[int] = mapped_column(
        ForeignKey("procurement_order_formation.id", ondelete="CASCADE"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(String(255), nullable=False)
    line_number: Mapped[int] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    bitrix_product_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bitrix_product_xml_id: Mapped[str] = mapped_column(String(64), nullable=False)
    nomenclature_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    nomenclature_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    nomenclature_name: Mapped[str] = mapped_column(String(1000), nullable=False)

    recommended_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 3), nullable=False, default=Decimal("0")
    )
    final_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 3), nullable=False, default=Decimal("0")
    )
    purchase_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    onec_open_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 3), nullable=True)
    onec_received_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 3), nullable=True)

    source_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="automatic")
    explicit_demand: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_level: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    risk_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    recommendation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    blockers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    assortment_status: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lifecycle_status: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    quality: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    procurement_profile: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manual_minimum: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 3), nullable=True)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    order: Mapped[ProcurementOrderFormation] = relationship(back_populates="lines")
    classification_proposals: Mapped[list[ProcurementClassificationProposal]] = relationship(
        back_populates="line",
        cascade="all, delete-orphan",
        order_by="ProcurementClassificationProposal.created_at.desc()",
    )


class ProcurementClassificationProposal(Base):
    __tablename__ = "procurement_classification_proposal"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_proc_class_proposal_idempotency"),
        Index("ix_proc_class_proposal_line_status", "line_id", "status"),
        Index("ix_proc_class_proposal_message", "onec_message_id"),
    )

    line_id: Mapped[int] = mapped_column(
        ForeignKey("procurement_order_formation_line.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    previous_status: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    proposed_status: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    manual_minimum: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 3), nullable=True)
    review_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # Код и наименование карточки-победителя семьи: решение 2026-08-18 требует
    # указывать, что ведут вместо снятой позиции.
    replacement_sku_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    replacement_sku_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    blocks_order_line: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    requested_by_actor: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_by_bitrix_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by_actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    approved_by_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejected_by_actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rejected_by_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rejected_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    onec_message_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    onec_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_sent")
    onec_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bitrix_readback_value: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reflected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    line: Mapped[ProcurementOrderFormationLine] = relationship(
        back_populates="classification_proposals"
    )


class ProcurementSupplierProfile(Base):
    __tablename__ = "procurement_supplier_profile"
    __table_args__ = (
        UniqueConstraint("supplier_ref", name="uq_proc_supplier_profile_ref"),
        Index("ix_proc_supplier_profile_code", "supplier_code"),
        Index("ix_proc_supplier_profile_class", "qualification_class"),
    )

    supplier_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    supplier_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    qualification_class: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    qualification_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    advantages: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    internal_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    payment_terms: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    credit_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    credit_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    terms_source: Mapped[str] = mapped_column(String(64), nullable=False, default="onec_contract")
    terms_status: Mapped[str] = mapped_column(String(32), nullable=False, default="missing")

    history_order_count: Mapped[Optional[int]] = mapped_column(nullable=True)
    supplier_prepare_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    logistics_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    lead_time_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    lead_time_confidence: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    price_history_count: Mapped[Optional[int]] = mapped_column(nullable=True)
    supplier_defect_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 3), nullable=True)
    supplier_defect_history_units: Mapped[Optional[int]] = mapped_column(nullable=True)
    supplier_defect_confidence: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    facts_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    facts_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    manual_updated_by_actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    manual_updated_by_bitrix_user_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    manual_updated_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    manual_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProcurementLifecycleTransitionProposal(Base):
    __tablename__ = "procurement_lifecycle_transition_proposal"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_proc_lifecycle_transition_idempotency"),
        Index("ix_proc_lifecycle_transition_queue", "folder", "current_status", "status"),
        Index("ix_proc_lifecycle_transition_run", "run_id", "status"),
        Index("ix_proc_lifecycle_transition_product", "nomenclature_code", "status"),
        Index("ix_proc_lifecycle_transition_message", "onec_message_id"),
    )

    nomenclature_code: Mapped[str] = mapped_column(String(64), nullable=False)
    nomenclature_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    product_guid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    product_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    folder: Mapped[str] = mapped_column(String(1000), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="transition")
    current_status: Mapped[str] = mapped_column(String(64), nullable=False)
    target_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    blockers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risk_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    run_id: Mapped[int] = mapped_column(nullable=False)
    run_key: Mapped[str] = mapped_column(String(160), nullable=False)
    facts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    responsible_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    responsible_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by_actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    approved_by_bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_by_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    onec_message_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    onec_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_sent")
    onec_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bitrix_readback_value: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reflected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProcurementOrderFormationEvent(Base):
    __tablename__ = "procurement_order_formation_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_proc_order_event_idempotency"),
        Index("ix_proc_order_event_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_proc_order_event_order", "order_id", "created_at"),
        Index("ix_proc_order_event_type", "event_type", "created_at"),
    )

    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("procurement_order_formation.id", ondelete="CASCADE"), nullable=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    bitrix_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    order: Mapped[Optional[ProcurementOrderFormation]] = relationship(back_populates="events")


Index(
    "uq_proc_order_formation_onec_ref_normalized",
    func.lower(func.trim(ProcurementOrderFormation.onec_document_ref)),
    unique=True,
    sqlite_where=ProcurementOrderFormation.onec_document_ref.is_not(None)
    & (func.trim(ProcurementOrderFormation.onec_document_ref) != ""),
    postgresql_where=ProcurementOrderFormation.onec_document_ref.is_not(None)
    & (func.trim(ProcurementOrderFormation.onec_document_ref) != ""),
)
