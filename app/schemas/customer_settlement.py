from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer


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

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal | None) -> str | None:
        return format(value, ".2f") if value is not None else None
