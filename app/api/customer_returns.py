from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_logistics_internal_token
from app.schemas.customer_returns import (
    CustomerReturnActionCompleteRequest,
    CustomerReturnActionResponse,
    CustomerReturnCarrierEventRequest,
    CustomerReturnCreateRequest,
    CustomerReturnDetailResponse,
    CustomerReturnEventIngestResponse,
    CustomerReturnOneCConfirmationRequest,
    CustomerReturnPickupRequest,
    CustomerReturnRegistrationResponse,
    CustomerReturnShipmentResponse,
)
from app.services import customer_returns as customer_return_service
from app.services.customer_return_carriers import CustomerReturnCarrierError

router = APIRouter(dependencies=[Depends(require_logistics_internal_token)])


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, customer_return_service.CustomerReturnNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, customer_return_service.CustomerReturnConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, CustomerReturnCarrierError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.post("", response_model=CustomerReturnRegistrationResponse)
def register_customer_return(
    payload: CustomerReturnCreateRequest,
    db: Session = Depends(get_db),
):
    try:
        shipment, created = customer_return_service.register_return(
            db,
            carrier=payload.carrier,
            tracking_number=payload.tracking_number,
            source=payload.source,
            source_ref=payload.source_ref,
            bitrix_case_id=payload.bitrix_case_id,
            site_ticket_id=payload.site_ticket_id,
            onec_order_ref=payload.onec_order_ref,
            created_by_bitrix_user_id=payload.created_by_bitrix_user_id,
            payload=payload.payload,
        )
    except (
        customer_return_service.CustomerReturnConflict,
        CustomerReturnCarrierError,
    ) as exc:
        _raise_http_error(exc)
    return {"created": created, "shipment": shipment}


@router.get("", response_model=list[CustomerReturnShipmentResponse])
def list_customer_returns(
    carrier: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return customer_return_service.list_returns(
        db,
        carrier=carrier,
        status=status,
        limit=limit,
    )


@router.get("/actions/due", response_model=list[CustomerReturnActionResponse])
def list_due_customer_return_actions(
    as_of: datetime = Query(default_factory=lambda: datetime.now(timezone.utc)),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return customer_return_service.list_due_actions(db, as_of=as_of, limit=limit)


@router.post(
    "/actions/{action_id}/complete",
    response_model=CustomerReturnActionResponse,
)
def complete_customer_return_action(
    action_id: int,
    payload: CustomerReturnActionCompleteRequest,
    db: Session = Depends(get_db),
):
    try:
        return customer_return_service.complete_action(
            db,
            action_id,
            external_reference=payload.external_reference,
            completed_at=payload.completed_at,
        )
    except (
        customer_return_service.CustomerReturnNotFound,
        customer_return_service.CustomerReturnConflict,
    ) as exc:
        _raise_http_error(exc)


@router.get("/{shipment_id}", response_model=CustomerReturnDetailResponse)
def get_customer_return(shipment_id: int, db: Session = Depends(get_db)):
    try:
        return customer_return_service.get_return(db, shipment_id)
    except customer_return_service.CustomerReturnNotFound as exc:
        _raise_http_error(exc)


@router.post(
    "/{shipment_id}/carrier-events",
    response_model=CustomerReturnEventIngestResponse,
)
def ingest_customer_return_carrier_event(
    shipment_id: int,
    payload: CustomerReturnCarrierEventRequest,
    db: Session = Depends(get_db),
):
    try:
        shipment, event_created = customer_return_service.record_carrier_event(
            db,
            shipment_id,
            status_code=payload.status_code,
            status_text=payload.status_text,
            occurred_at=payload.occurred_at,
            external_event_id=payload.external_event_id,
            idempotency_key=payload.idempotency_key,
            storage_deadline_at=payload.storage_deadline_at,
            payload=payload.payload,
        )
    except (
        customer_return_service.CustomerReturnNotFound,
        CustomerReturnCarrierError,
    ) as exc:
        _raise_http_error(exc)
    return {"event_created": event_created, "shipment": shipment}


@router.post("/{shipment_id}/pickup", response_model=CustomerReturnDetailResponse)
def confirm_customer_return_pickup(
    shipment_id: int,
    payload: CustomerReturnPickupRequest,
    db: Session = Depends(get_db),
):
    try:
        return customer_return_service.confirm_pickup(
            db,
            shipment_id,
            actor_bitrix_user_id=payload.actor_bitrix_user_id,
            occurred_at=payload.occurred_at,
            idempotency_key=payload.idempotency_key,
            comment=payload.comment,
        )
    except customer_return_service.CustomerReturnNotFound as exc:
        _raise_http_error(exc)


@router.post(
    "/{shipment_id}/onec-confirmation",
    response_model=CustomerReturnDetailResponse,
)
def confirm_customer_return_in_onec(
    shipment_id: int,
    payload: CustomerReturnOneCConfirmationRequest,
    db: Session = Depends(get_db),
):
    try:
        return customer_return_service.confirm_onec_return(
            db,
            shipment_id,
            onec_return_ref=payload.onec_return_ref,
            occurred_at=payload.occurred_at,
            idempotency_key=payload.idempotency_key,
        )
    except (
        customer_return_service.CustomerReturnNotFound,
        customer_return_service.CustomerReturnConflict,
    ) as exc:
        _raise_http_error(exc)
