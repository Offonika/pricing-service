from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

PaymentCheckStage = Literal["checkout", "cloudpayments_check", "cloudpayments_pay"]
PaymentDecisionReason = Literal[
    "amount_match",
    "site_payment_mismatch",
    "onec_order_not_found",
    "onec_order_deleted",
    "onec_order_ambiguous",
    "onec_order_unposted",
    "onec_order_closed",
    "onec_amount_invalid",
    "onec_amount_mismatch",
]


class OrderPaymentCheckRequest(BaseModel):
    site_order_number: str = Field(min_length=1, max_length=40)
    site_amount: Decimal = Field(ge=0, max_digits=15, decimal_places=2)
    payment_amount: Decimal = Field(ge=0, max_digits=15, decimal_places=2)
    stage: PaymentCheckStage
    payment_id: str | None = Field(default=None, max_length=128)

    @field_validator("site_order_number", "payment_id")
    @classmethod
    def strip_identifiers(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be blank")
        return normalized


class OrderPaymentCheckResponse(BaseModel):
    check_id: str
    allowed: bool
    reason: PaymentDecisionReason
    site_order_number: str
    site_amount: Decimal
    payment_amount: Decimal
    onec_amount: Decimal | None = None
    onec_document_number: str | None = None
    onec_revision: str | None = None
    onec_posted: bool | None = None
    onec_closure_document: str | None = None
    onec_closure_reason: str | None = None
    checked_at: datetime
