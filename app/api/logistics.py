from __future__ import annotations

from datetime import date
from typing import Literal
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_logistics_internal_token
from app.schemas.logistics import (
    LogisticsConfirmResponse,
    LogisticsDraftConfirmRequest,
    LogisticsDraftCreateRequest,
    LogisticsDraftResponse,
    LogisticsDraftScanRequest,
    LogisticsDriverResponse,
    LogisticsDriverSyncItem,
    LogisticsEventActionRequest,
    LogisticsExpectedDeliveryResponse,
    LogisticsExternalCarrierAcceptRequest,
    LogisticsExternalCarrierHandoffRequest,
    LogisticsHistoryEventResponse,
    LogisticsManualReadyOverrideRequest,
    LogisticsManualReviewResponse,
    LogisticsMonitorResponse,
    LogisticsRouteRunCreateRequest,
    LogisticsRouteRunResponse,
    LogisticsRtuReadyForPickupResponse,
    LogisticsSyncResponse,
    LogisticsTelegramAuthRequest,
    LogisticsTransferAssistantCandidateResponse,
    LogisticsTransferSyncItem,
    LogisticsUnitLookupResponse,
    LogisticsUnitSyncItem,
    LogisticsUserProfile,
    LogisticsUserSyncItem,
    LogisticsWarehouseResponse,
    LogisticsWarehouseSyncItem,
)
from app.services import logistics as logistics_service
from app.services import transfer_assistant as transfer_assistant_service

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


@router.post("/sync/units", response_model=LogisticsSyncResponse)
def sync_units(items: list[LogisticsUnitSyncItem], db: Session = Depends(get_db)):
    return logistics_service.sync_units(db, [item.model_dump() for item in items])


@router.get("/units/lookup", response_model=LogisticsUnitLookupResponse)
def lookup_unit(code: str = Query(min_length=1), db: Session = Depends(get_db)):
    return logistics_service.lookup_unit(db, code=code)


@router.get("/warehouses", response_model=list[LogisticsWarehouseResponse])
def list_warehouses(db: Session = Depends(get_db)):
    return logistics_service.list_warehouses(db)


@router.get("/drivers", response_model=list[LogisticsDriverResponse])
def list_drivers(db: Session = Depends(get_db)):
    return logistics_service.list_drivers(db)


@router.post("/handoffs/draft", response_model=LogisticsDraftResponse)
def create_handoff_draft(payload: LogisticsDraftCreateRequest, db: Session = Depends(get_db)):
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_HANDOFF,
        actor_user_id=payload.actor_user_id,
        warehouse_id=payload.warehouse_id,
        driver_id=payload.driver_id,
        route_run_id=payload.route_run_id,
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
        lookup_code=payload.lookup_code,
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
        route_run_id=payload.route_run_id,
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
        lookup_code=payload.lookup_code,
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


@router.get(
    "/rtu/ready-for-pickup",
    response_model=list[LogisticsRtuReadyForPickupResponse],
    responses={
        200: {
            "content": {
                "application/xml": {
                    "schema": {"type": "string"},
                }
            }
        }
    },
)
def list_rtu_ready_for_pickup(
    warehouse_code: str = Query(min_length=1),
    date_from: date | None = Query(default=None),
    response_format: Literal["json", "xml"] = Query(default="json", alias="format"),
    db: Session = Depends(get_db),
):
    rows = logistics_service.list_rtu_ready_for_pickup(
        db,
        warehouse_code=warehouse_code,
        date_from=date_from,
    )
    if response_format == "xml":
        root = ElementTree.Element("rtu_ready_for_pickup")
        for row in rows:
            item = ElementTree.SubElement(root, "rtu")
            for field in ("external_id", "document_number", "document_date", "accepted_at"):
                value = row[field]
                child = ElementTree.SubElement(item, field)
                child.text = value.isoformat() if hasattr(value, "isoformat") else str(value)
        return Response(
            ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
            media_type="application/xml",
        )
    return rows


@router.get("/monitor", response_model=list[LogisticsMonitorResponse])
def list_monitor(
    status: str | None = Query(default=None),
    warehouse_id: int | None = Query(default=None),
    driver_id: int | None = Query(default=None),
    final_recipient: str | None = Query(default=None),
    source_document_type: str | None = Query(default=None),
    route_run_id: int | None = Query(default=None),
    with_external_carrier: bool | None = Query(default=None),
    manual_review: bool | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return logistics_service.list_monitor(
        db,
        status=status,
        warehouse_id=warehouse_id,
        driver_id=driver_id,
        final_recipient=final_recipient,
        source_document_type=source_document_type,
        route_run_id=route_run_id,
        with_external_carrier=with_external_carrier,
        manual_review=manual_review,
    )


@router.get(
    "/transfer-assistant/candidates",
    response_model=list[LogisticsTransferAssistantCandidateResponse],
)
def list_transfer_assistant_candidates(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    warehouse_id: str | None = Query(default=None, min_length=1),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    try:
        return transfer_assistant_service.list_transfer_assistant_candidates(
            date_from=date_from,
            date_to=date_to,
            warehouse_id=warehouse_id,
            status=status,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/route-runs", response_model=LogisticsRouteRunResponse)
def create_route_run(payload: LogisticsRouteRunCreateRequest, db: Session = Depends(get_db)):
    return logistics_service.create_route_run(
        db,
        route_name=payload.route_name,
        external_id=payload.external_id,
        planned_at=payload.planned_at,
        driver_id=payload.driver_id,
        status=payload.status,
        payload=payload.payload,
        items=[item.model_dump() for item in payload.items],
    )


@router.get("/route-runs", response_model=list[LogisticsRouteRunResponse])
def list_route_runs(
    status: str | None = Query(default=None),
    driver_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return logistics_service.list_route_runs(db, status=status, driver_id=driver_id)


@router.get("/manual-review", response_model=list[LogisticsManualReviewResponse])
def list_manual_review(
    status: str | None = Query(default="open"),
    review_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return logistics_service.list_manual_reviews(
        db,
        status=status,
        review_type=review_type,
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


@router.post("/transfers/{transfer_id}/handoff-cancel")
def cancel_handoff(
    transfer_id: int,
    payload: LogisticsEventActionRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.create_transfer_event(
        db,
        transfer_id=transfer_id,
        actor_user_id=payload.actor_user_id,
        event_type=logistics_service.EVENT_HANDOFF_CANCELLED,
        source="api",
        warehouse_id=payload.warehouse_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[photo.model_dump() for photo in payload.photos],
    )


@router.post("/transfers/{transfer_id}/external-carrier/handoff")
def external_carrier_handoff(
    transfer_id: int,
    payload: LogisticsExternalCarrierHandoffRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.handoff_to_external_carrier(
        db,
        transfer_id=transfer_id,
        actor_user_id=payload.actor_user_id,
        carrier_name=payload.carrier_name,
        tracking_number=payload.tracking_number,
        carrier_terminal=payload.carrier_terminal,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/transfers/{transfer_id}/external-carrier/accept")
def external_carrier_accept(
    transfer_id: int,
    payload: LogisticsExternalCarrierAcceptRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.accept_from_external_carrier(
        db,
        transfer_id=transfer_id,
        actor_user_id=payload.actor_user_id,
        warehouse_id=payload.warehouse_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/manual-ready-overrides")
def manual_ready_override(
    payload: LogisticsManualReadyOverrideRequest,
    db: Session = Depends(get_db),
):
    return logistics_service.manual_ready_override(
        db,
        actor_user_id=payload.actor_user_id,
        source_document_type=payload.source_document_type,
        external_id=payload.external_id,
        warehouse_id=payload.warehouse_id,
        reason=payload.reason,
        lookup_code=payload.lookup_code,
        site_order_number=payload.site_order_number,
    )
