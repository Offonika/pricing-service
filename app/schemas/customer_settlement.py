from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer, model_validator


class CustomerSettlementSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "available",
        "stale",
        "temporarily_unavailable",
        "not_linked",
        "ambiguous_link",
        "pilot_disabled",
    ]
    state: Literal["debt", "advance", "zero"] | None = None
    amount: Decimal | None = None
    currency: Literal["RUB"] | None = None
    as_of: datetime | None = None
    synced_at: datetime | None = None
    is_stale: bool

    @model_validator(mode="after")
    def validate_status_payload(self) -> CustomerSettlementSummaryResponse:
        financial_fields = (self.state, self.amount, self.currency, self.as_of, self.synced_at)
        if self.status in {"available", "stale"}:
            if any(value is None for value in financial_fields):
                raise ValueError("available and stale responses require complete financial fields")
            if self.is_stale is not (self.status == "stale"):
                raise ValueError("is_stale must match the response status")
        elif any(value is not None for value in financial_fields) or self.is_stale:
            raise ValueError("non-financial statuses must not expose financial fields")
        return self

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal | None) -> str | None:
        return format(value, ".2f") if value is not None else None


class CustomerSettlementEligibilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["eligible", "not_eligible", "temporarily_unavailable"]
