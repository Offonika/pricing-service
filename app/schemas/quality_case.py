from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

QualityDecisionCode = Literal[
    "factory_defect",
    "supplier_defect",
    "technical_defect",
    "confirmed_ok_after_check",
    "revision_or_catalog_mismatch",
    "transport_damage",
    "internal_handling_damage",
    "customer_reason",
]
QualityDispositionCode = Literal[
    "return_to_stock",
    "repair_then_return_to_stock",
    "keep_as_defect",
    "return_to_supplier",
    "convert_to_nonconforming",
    "write_off",
]


class QualityCaseSyncItem(BaseModel):
    external_id: str
    source_return_ref: str
    source_return_number: str | None = None
    source_return_line_key: str
    return_at: datetime
    nomenclature_ref: str | None = None
    nomenclature_code: str
    nomenclature_name: str | None = None
    quantity: Decimal = Field(gt=0)
    store_external_id: str | None = None
    store_name: str | None = None
    preliminary_quality: str | None = None
    preliminary_reason_code: str | None = None
    owner_external_id: str | None = None
    due_at: datetime | None = None
    payload: dict[str, Any] | None = None
    idempotency_key: str | None = None


class QualityCaseActionRequest(BaseModel):
    actor_external_id: str
    comment: str | None = None
    idempotency_key: str | None = None


class QualityCaseDecisionRequest(QualityCaseActionRequest):
    decision_code: QualityDecisionCode
    disposition_code: QualityDispositionCode
    onec_quality_correction_ref: str | None = None


class QualityCaseResponse(BaseModel):
    id: int
    external_id: str
    source_return_ref: str
    source_return_number: str | None = None
    source_return_line_key: str
    return_at: datetime
    nomenclature_ref: str | None = None
    nomenclature_code: str
    nomenclature_name: str | None = None
    quantity: Decimal
    store_external_id: str | None = None
    store_name: str | None = None
    preliminary_quality: str | None = None
    preliminary_reason_code: str | None = None
    current_status: str
    owner_external_id: str | None = None
    due_at: datetime | None = None
    final_decision_code: str | None = None
    disposition_code: str | None = None
    decision_comment: str | None = None
    decision_author_external_id: str | None = None
    decided_at: datetime | None = None
    onec_quality_correction_ref: str | None = None
    counts_as_confirmed_product_defect: bool
    payload: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class QualityCaseEventResponse(BaseModel):
    id: int
    event_type: str
    event_at: datetime
    actor_external_id: str | None = None
    source: str
    comment: str | None = None
    meta: dict[str, Any] | None = None


class QualityMetricItem(BaseModel):
    nomenclature_code: str
    candidate_qty: Decimal
    pending_qty: Decimal
    confirmed_product_defect_qty: Decimal
    handling_damage_qty: Decimal
    confirmed_not_product_defect_qty: Decimal
