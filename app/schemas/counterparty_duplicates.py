from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class CounterpartyDuplicateCandidateRecord(BaseModel):
    counterparty_ref: str
    counterparty_name: str | None = None
    phone: str | None = None
    email: str | None = None
    tax_id: str | None = None
    responsible_code: str | None = None
    updated_at: datetime | None = None


class CounterpartyDuplicateCasePayload(BaseModel):
    case_id: int
    dedupe_key: str
    detected_at: datetime
    risk_level: str
    reason_codes: list[str]
    records: list[CounterpartyDuplicateCandidateRecord]
    responsible_code: str | None = None
    status: str
    sla_deadline_at: datetime
    summary_text: str
    source_hash: str
    delivery_state: str
    external_case_id: str | None = None
    external_status: str | None = None
    external_url: str | None = None


class CounterpartyDuplicatePendingResponse(BaseModel):
    items: list[CounterpartyDuplicateCasePayload]


class CounterpartyDuplicateAckRequest(BaseModel):
    external_case_id: str | None = None
    external_status: str | None = None
    external_url: str | None = None
    status: str | None = None
    delivered_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class CounterpartyDuplicateAckResponse(BaseModel):
    case_id: int
    delivery_state: str
    delivered_at: datetime | None = None
    external_case_id: str | None = None
    external_status: str | None = None
    status: str


class CounterpartyDuplicateHealthComponent(BaseModel):
    component: str
    freshness_status: str
    source_status: str
    latest_detected_at: datetime | None = None
    metrics: dict[str, Any]


class CounterpartyDuplicateHealthResponse(BaseModel):
    status: str
    freshness_status: str
    source_status: str
    components: list[CounterpartyDuplicateHealthComponent]
