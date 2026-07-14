from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_card_balance_reconciliation_internal_token
from app.schemas.card_balance_reconciliation import (
    CardBalanceCashboxSyncResponse,
    CardBalanceManualPayload,
    CardBalanceReconciliationDetailResponse,
    CardBalanceReconciliationListItem,
    CardBalanceSyncResponse,
)
from app.services import card_balance_reconciliation as reconciliation_service
from app.workers import card_balance_reconciliation as worker

router = APIRouter(dependencies=[Depends(require_card_balance_reconciliation_internal_token)])


@router.get("/events", response_model=list[CardBalanceReconciliationListItem])
def list_events(
    status: str | None = Query(default=None),
    exception_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return reconciliation_service.list_reconciliations(
        db,
        status=status,
        exception_only=exception_only,
        limit=limit,
    )


@router.get("/exceptions", response_model=list[CardBalanceReconciliationListItem])
def list_exceptions(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return reconciliation_service.list_reconciliations(db, exception_only=True, limit=limit)


@router.get("/events/{event_id}", response_model=CardBalanceReconciliationDetailResponse)
def get_event(event_id: int, db: Session = Depends(get_db)):
    return reconciliation_service.get_reconciliation(db, reconciliation_id=event_id)


@router.post("/events", response_model=CardBalanceReconciliationDetailResponse)
def upsert_event(payload: CardBalanceManualPayload, db: Session = Depends(get_db)):
    row = reconciliation_service.upsert_reconciliation_from_payload(
        db,
        payload=payload.model_dump(exclude_none=True),
        onec_balance=payload.onec_balance,
    )
    return reconciliation_service.get_reconciliation(db, reconciliation_id=row.id)


@router.post(
    "/events/{event_id}/recalculate", response_model=CardBalanceReconciliationDetailResponse
)
def recalculate_event(event_id: int, db: Session = Depends(get_db)):
    row = worker.recalculate_reconciliation(db, reconciliation_id=event_id)
    return reconciliation_service.get_reconciliation(db, reconciliation_id=row.id)


@router.post("/sync/bitrix", response_model=CardBalanceSyncResponse)
def sync_bitrix(
    limit: int = Query(default=50, ge=1, le=2000),
    business_date: date | None = Query(default=None),
    auto_create_daily: bool | None = Query(default=None),
    dry_run_auto_create: bool = Query(default=False),
    require_workday: bool | None = Query(default=None),
    pilot_cashbox_code: list[str] | None = Query(default=None),
    ocr_enabled: bool | None = Query(default=None),
    max_create_count: int | None = Query(default=None, ge=1),
):
    return worker.run_card_balance_bitrix_sync(
        limit=limit,
        business_date=business_date,
        auto_create_daily=auto_create_daily,
        dry_run_auto_create=dry_run_auto_create,
        require_workday=require_workday,
        pilot_cashbox_codes=pilot_cashbox_code,
        ocr_enabled=ocr_enabled,
        max_create_count=max_create_count,
    )


@router.post("/sync/onec-cashboxes", response_model=CardBalanceCashboxSyncResponse)
def sync_onec_cashboxes():
    return worker.run_card_balance_onec_cashbox_sync()
