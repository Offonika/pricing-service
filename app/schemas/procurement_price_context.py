"""Explicit price layers. All monetary values are per source unit, never estimates."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class ProcurementPriceDocument(BaseModel):
    kind: str
    ref: str
    number: str | None = None
    at: datetime | None = None


class ProcurementPriceFact(BaseModel):
    value: Decimal | None = None
    currency: str | None = None
    status: Literal["confirmed", "reference", "unconfirmed", "missing", "ambiguous", "not_formed"]
    reason: str | None = None
    source: str | None = None
    confirmed_by: str | None = None
    at: datetime | None = None
    unit_ref: str | None = None
    unit_name: str | None = None
    characteristic_ref: str | None = None
    exchange_rate: Decimal | None = None
    exchange_multiplicity: Decimal | None = None
    exchange_rate_at: datetime | None = None
    documents: list[ProcurementPriceDocument] = Field(default_factory=list)


class ProcurementPriceContext(BaseModel):
    schema_version: Literal[1] = 1
    agreed_purchase: ProcurementPriceFact
    purchase_rub: ProcurementPriceFact
    receipt_purchases_rub: list[ProcurementPriceFact] = Field(default_factory=list)
    reference_cost_rub: ProcurementPriceFact
    actual_cost_status: Literal["confirmed", "partial", "not_formed", "ambiguous"] = "not_formed"
    actual_costs_rub: list[ProcurementPriceFact] = Field(default_factory=list)
    supplier_quotes: list[ProcurementPriceFact] = Field(default_factory=list)
    source_status: Literal["ready", "unavailable", "not_loaded", "ambiguous"] = "not_loaded"
    checked_on: date | None = None
    last_success_on: date | None = None
    stale: bool = False
    error_type: str | None = None
