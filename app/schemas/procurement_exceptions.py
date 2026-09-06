from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProcurementExceptionRead(BaseModel):
    id: int
    order_id: int
    line_id: int | None = None
    reason_code: str
    title: str
    status: str
    version: int
    facts_hash: str
    facts: dict[str, Any]
    first_seen_at: datetime
    last_seen_at: datetime
    response_due_at: datetime
    acknowledged_at: datetime | None = None
    assigned_user_id: str | None = None
    next_action: str | None = None
    next_action_due_at: datetime | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    overdue: bool


class ProcurementExceptionList(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[ProcurementExceptionRead]
    by_reason: dict[str, int]
    overdue_count: int
    oversight: str = "Омар"


class ProcurementExceptionDecision(BaseModel):
    expected_version: int = Field(ge=1)
    facts_hash: str = Field(pattern="^[0-9a-f]{64}$")
    status: Literal["in_progress", "waiting", "resolved"]
    next_action: str | None = Field(default=None, max_length=2000)
    next_action_due_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=2000)
    evidence: str | None = Field(default=None, max_length=4000)
    expected_order_version: int | None = Field(default=None, ge=1)
    expected_line_version: int | None = Field(default=None, ge=1)
    final_quantity: Decimal | None = Field(default=None, gt=0)


class ProcurementControlSummary(BaseModel):
    generated_at: datetime
    open_orders: int
    without_eta: int
    past_eta: int
    unconfirmed_incoming_quantity: Decimal
    unknown_incoming_order_count: int
    synchronization_errors: int
    last_onec_sync_at: datetime | None = None
    oldest_onec_sync_at: datetime | None = None
    stale_receipt_sources: int = 0
    unknown_freshness_count: int
    exceptions_open: int
    exceptions_overdue: int
    stockout_risks: int
    recommendation_decisions: dict[str, int]
    recommendation_change_reasons: dict[str, int] = Field(default_factory=dict)
    confirmed_amount_by_currency: dict[str, Decimal]
    confirmed_amount_scope: str = "active_generated_drafts"
    unpriced_lines: int
