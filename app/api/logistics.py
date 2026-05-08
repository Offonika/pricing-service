from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_logistics_internal_token
from app.schemas.logistics import (
    LogisticsConfirmResponse,
    LogisticsDraftConfirmRequest,
    LogisticsDraftCreateRequest,
    LogisticsDraftResponse,
    LogisticsDraftScanRequest,
    LogisticsDriverSyncItem,
    LogisticsEventActionRequest,
    LogisticsExpectedDeliveryResponse,
    LogisticsHistoryEventResponse,
    LogisticsMonitorResponse,
    LogisticsSyncResponse,
    LogisticsTelegramAuthRequest,
    LogisticsTransferSyncItem,
    LogisticsUserProfile,
    LogisticsUserSyncItem,
    LogisticsWarehouseSyncItem,
)
from app.services import logistics as logistics_service

router = APIRouter(dependencies=[Depends(require_logistics_internal_token)])


@router.post("/auth/telegram", response_model=LogisticsUserProfile)
def auth_telegram(payload: LogisticsTelegramAuthRequest, db: Session = Depends(get_db)):
    return logistics_service.telegram_auth(
        db, telegram_user_id=payload.telegram_user_id, username=payload.username
    )


@router.post("/sync/warehouses", response_model=LogisticsSyncResponse)
def sync_warehouses(items: list[LogisticsWarehouseSyncItem], db: Session = Depends(get_db)):
    return logistics_service.sync_warehouses(db, [item.model_dump() for item in items])


@router.post("/sync/drivers", response_model=LogisticsSyncResponse)
def sync_drivers(items: list[LogisticsDriverSyncItem], db: Session = Depends(get_db)):
    return logistics_service.sync_drivers(db, [item.model_dump() for item in items])


@router.post("/sync/users", response_model=LogisticsSyncResponse)
def sync_users(items: list[LogisticsUserSyncItem], db: Session = Depends(get_db)):
    return logistics_service.sync_users(db, [item.model_dump() for item in items])


@router.post("/sync/transfers", response_model=LogisticsSyncResponse)
def sync_transfers(items: list[LogisticsTransferSyncItem], db: Session = Depends(get_db)):
    return logistics_service.sync_transfers(db, [item.model_dump() for item in items])


@router.post("/handoffs/draft", response_model=LogisticsDraftResponse)
def create_handoff_draft(payload: LogisticsDraftCreateRequest, db: Session = Depends(get_db)):
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_HANDOFF,
        actor_user_id=payload.actor_user_id,
        warehouse_id=payload.warehouse_id,
        driver_id=payload.driver_id,
        default_dropoff_warehouse_id=payload.default_dropoff_warehouse_id,
        comment=payload.comment,
    )


@router.post("/handoffs/draft/{draft_id}/scan", response_model=LogisticsDraftResponse)
def scan_handoff_item(
    draft_id: int,
    payload: LogisticsDraftScanRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=payload.actor_user_id,
        barcode=payload.barcode,
        dropoff_warehouse_id=payload.dropoff_warehouse_id,
    )


@router.post("/handoffs/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def confirm_handoff(
    draft_id: int,
    payload: LogisticsDraftConfirmRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=payload.actor_user_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[photo.model_dump() for photo in payload.photos],
    )


@router.post("/receipts/draft", response_model=LogisticsDraftResponse)
def create_receipt_draft(payload: LogisticsDraftCreateRequest, db: Session = Depends(get_db)):
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_RECEIPT,
        actor_user_id=payload.actor_user_id,
        warehouse_id=payload.warehouse_id,
        driver_id=payload.driver_id,
        comment=payload.comment,
    )


@router.post("/receipts/draft/{draft_id}/scan", response_model=LogisticsDraftResponse)
def scan_receipt_item(
    draft_id: int,
    payload: LogisticsDraftScanRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=payload.actor_user_id,
        barcode=payload.barcode,
    )


@router.post("/receipts/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def confirm_receipt(
    draft_id: int,
    payload: LogisticsDraftConfirmRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=payload.actor_user_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[photo.model_dump() for photo in payload.photos],
    )


@router.get("/expected-deliveries", response_model=list[LogisticsExpectedDeliveryResponse])
def list_expected_deliveries(
    warehouse_id: int,
    driver_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return logistics_service.list_expected_deliveries(
        db,
        warehouse_id=warehouse_id,
        driver_id=driver_id,
    )


@router.get("/monitor", response_model=list[LogisticsMonitorResponse])
def list_monitor(
    status: str | None = Query(default=None),
    warehouse_id: int | None = Query(default=None),
    driver_id: int | None = Query(default=None),
    final_recipient: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return logistics_service.list_monitor(
        db,
        status=status,
        warehouse_id=warehouse_id,
        driver_id=driver_id,
        final_recipient=final_recipient,
    )


@router.get("/transfers/{transfer_id}/history", response_model=list[LogisticsHistoryEventResponse])
def transfer_history(transfer_id: int, db: Session = Depends(get_db)):
    return logistics_service.get_transfer_history(db, transfer_id=transfer_id)


@router.post("/transfers/{transfer_id}/incident")
def create_incident(
    transfer_id: int,
    payload: LogisticsEventActionRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.create_transfer_event(
        db,
        transfer_id=transfer_id,
        actor_user_id=payload.actor_user_id,
        event_type=logistics_service.EVENT_INCIDENT,
        source="api",
        warehouse_id=payload.warehouse_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[photo.model_dump() for photo in payload.photos],
    )


@router.post("/transfers/{transfer_id}/return")
def mark_returned(
    transfer_id: int,
    payload: LogisticsEventActionRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.create_transfer_event(
        db,
        transfer_id=transfer_id,
        actor_user_id=payload.actor_user_id,
        event_type=logistics_service.EVENT_RETURNED,
        source="api",
        warehouse_id=payload.warehouse_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[photo.model_dump() for photo in payload.photos],
    )
