from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ONEC_REF_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class OrderClosureSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class OrderClosureSessionUser(BaseModel):
    user_id: str
    name: str | None = None
    role: Literal["viewer", "order_closure_operator"]
    can_confirm: bool


class OrderClosureSessionResponse(BaseModel):
    session_token: str
    expires_at: datetime
    expires_in: int
    user: OrderClosureSessionUser


class OrderClosureInputLine(BaseModel):
    number: str = Field(min_length=1, max_length=64)
    period: str | None = Field(default=None, max_length=10)


class OrderClosureFilter(BaseModel):
    year: int = Field(ge=2000, le=2100)
    department_ref: str | None = Field(default=None, pattern=ONEC_REF_PATTERN)
    category: Literal["all", "web", "onec"] = "all"
    state: Literal["all", "eligible", "blocked", "closed"] = "all"


class OrderClosureBatchCreateRequest(BaseModel):
    source_type: Literal["excel", "filter"]
    pasted_text: str | None = Field(default=None, max_length=20000)
    lines: list[OrderClosureInputLine] = Field(default_factory=list, max_length=200)
    filters: OrderClosureFilter | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.source_type == "filter" and self.filters is None:
            raise ValueError("filters are required for filter source")
        if self.source_type == "excel" and not (self.pasted_text or self.lines):
            raise ValueError("pasted_text or lines are required for excel source")
        return self


class OrderClosureReasonAssignment(BaseModel):
    item_id: int
    reason_code: Literal["execution", "cancellation"]
    reason_ref: str = Field(pattern=ONEC_REF_PATTERN)
    reason_name: Literal["Исполнение заказа", "Отмена заказа"]


class OrderClosureConfirmRequest(BaseModel):
    diagnosis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignments: list[OrderClosureReasonAssignment] = Field(min_length=1, max_length=200)


class OrderClosureItemResponse(BaseModel):
    id: int
    position: int
    input_number: str
    input_period: str | None
    onec_order_ref: str | None
    onec_order_number: str | None
    onec_order_date: date | None
    site_order_number: str | None
    department_name: str | None
    status: str
    eligible: bool
    blocker_code: str | None
    blocker_text: str | None
    facts: dict[str, Any]
    state_hash: str | None
    reason_code: str | None
    reason_ref: str | None
    reason_name: str | None
    result_document_ref: str | None
    result_document_number: str | None


class OrderClosureBatchResponse(BaseModel):
    id: str
    status: str
    source_type: str
    actor_id: str
    actor_name: str | None
    confirmed_by: str | None
    diagnosis_hash: str | None
    command_kind: str | None
    attempt_count: int
    last_error_code: str | None
    last_polled_at: datetime | None
    lease_until: datetime | None
    applied_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[OrderClosureItemResponse]


class OrderClosureReasonResponse(BaseModel):
    code: Literal["execution", "cancellation"]
    name: Literal["Исполнение заказа", "Отмена заказа"]
    ref: str | None = Field(default=None, pattern=ONEC_REF_PATTERN)


class OrderClosureCandidateResponse(BaseModel):
    batch_id: str
    diagnosis_hash: str
    items: list[OrderClosureItemResponse]


class OrderClosureAckItem(BaseModel):
    position: int = Field(ge=1, le=200)
    input_number: str = Field(min_length=1, max_length=64)
    input_period: str | None = Field(default=None, max_length=10)
    onec_order_ref: str | None = Field(default=None, pattern=ONEC_REF_PATTERN)
    onec_order_number: str | None = None
    onec_order_date: date | None = None
    site_order_number: str | None = None
    department_ref: str | None = Field(default=None, pattern=ONEC_REF_PATTERN)
    department_name: str | None = None
    eligible: bool = False
    blocker_code: str | None = None
    blocker_text: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    state_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_document_ref: str | None = Field(default=None, pattern=ONEC_REF_PATTERN)
    result_document_number: str | None = None


class OrderClosureCommandAckRequest(BaseModel):
    lease_token: str = Field(min_length=1, max_length=64)
    outcome: Literal["diagnosed", "applied", "stale", "failed"]
    diagnosis_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(default=None, max_length=128)
    items: list[OrderClosureAckItem] = Field(default_factory=list, max_length=200)


class OrderClosureCommandAckResponse(BaseModel):
    batch_id: str
    status: str
    duplicate: bool = False
