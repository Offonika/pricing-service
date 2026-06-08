from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class CardBalanceCashboxResponse(BaseModel):
    id: int
    onec_cashbox_ref_hex: str | None = None
    onec_cashbox_code: str
    onec_cashbox_name: str
    currency_code: str | None = None
    currency_name: str | None = None
    card_last4: str | None = None
    store_name: str | None = None
    employee_last_name: str | None = None
    employee_id: str | None = None
    is_active: bool
    needs_manual_review: bool
    review_reason: str | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CardBalanceReconciliationEventResponse(BaseModel):
    id: int
    reconciliation_id: int
    event_type: str
    event_at: datetime
    actor_external_id: str | None = None
    source: str
    comment: str | None = None
    meta: dict[str, Any] | None = None
    created_at: datetime


class CardBalanceReconciliationListItem(BaseModel):
    id: int
    external_id: str
    business_date: date
    cashbox_id: int | None = None
    employee_id: str | None = None
    employee_name: str | None = None
    employee_last_name: str | None = None
    card_last4: str | None = None
    onec_cashbox_code: str | None = None
    onec_cashbox_name: str | None = None
    source_channel: str
    bitrix_item_id: str | None = None
    bitrix_stage_id: str | None = None
    screenshot_file_id: str | None = None
    submitted_at: datetime | None = None
    screenshot_taken_at: datetime | None = None
    manual_balance: Decimal | None = None
    recognized_balance: Decimal | None = None
    recognition_confidence: Decimal | None = None
    onec_balance_at: datetime | None = None
    onec_balance: Decimal | None = None
    diff_amount: Decimal | None = None
    status: str
    reviewer_id: str | None = None
    resolution_comment: str | None = None
    resolved_at: datetime | None = None
    due_at: datetime | None = None
    bitrix_last_sync_at: datetime | None = None
    bitrix_last_error: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class CardBalanceReconciliationDetailResponse(CardBalanceReconciliationListItem):
    events: list[CardBalanceReconciliationEventResponse] = Field(default_factory=list)


class CardBalanceManualPayload(BaseModel):
    external_id: str | None = None
    business_date: date
    employee_id: str | None = None
    employee_name: str | None = None
    employee_last_name: str | None = None
    card_last4: str | None = None
    onec_cashbox_code: str | None = None
    onec_cashbox_name: str | None = None
    bitrix_item_id: str | None = None
    screenshot_file_id: str | None = None
    manual_balance: Decimal | None = None
    recognized_balance: Decimal | None = None
    recognition_confidence: Decimal | None = None
    onec_balance: Decimal | None = None
    due_at: datetime | None = None
    resolution_comment: str | None = None


class CardBalanceSyncResponse(BaseModel):
    processed: int
    matched: int = 0
    exceptions: int = 0
    errors: int = 0
    business_date: date | None = None
    daily_created: int = 0
    daily_skipped_existing: int = 0
    daily_skipped_manual_review: int = 0
    daily_skipped_missing_data: int = 0
    daily_skipped_not_in_pilot: int = 0
    daily_skipped_no_workday_data: int = 0
    skipped_not_in_pilot: int = 0
    skipped_no_workday_data: int = 0
    skipped_unmapped_bitrix_item: int = 0
    ocr_errors: int = 0
    daily_create_errors: int = 0


class CardBalanceCashboxSyncResponse(BaseModel):
    created: int
    updated: int
    total: int
