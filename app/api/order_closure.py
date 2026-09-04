from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, Security
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import _require_bearer_token, get_db, security
from app.api.procurement_labels import _bitrix_launch_payload, _inject_launch_payload, _read_index
from app.core.config import get_settings
from app.schemas.order_closure import (
    OrderClosureBatchCreateRequest,
    OrderClosureBatchResponse,
    OrderClosureCandidateResponse,
    OrderClosureCommandAckRequest,
    OrderClosureCommandAckResponse,
    OrderClosureConfirmRequest,
    OrderClosureItemResponse,
    OrderClosureReasonResponse,
    OrderClosureSessionRequest,
    OrderClosureSessionResponse,
    OrderClosureSessionUser,
)
from app.services import order_closure as service
from app.services.bitrix_order_closure_auth import (
    OrderClosureSession,
    create_order_closure_session_token,
    ensure_order_closure_launch_allowed,
    ensure_order_closure_user_allowed,
    load_bitrix_current_user,
    verify_order_closure_session,
)

router = APIRouter(prefix="/api/order-closures", tags=["order-closures"])
page_router = APIRouter()


@page_router.api_route(
    "/bitrix/order-closures",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/order-closures/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/order-closures/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_order_closures_page(request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    return HTMLResponse(_inject_launch_payload(_read_index(), payload))


def _actor(session: OrderClosureSession) -> service.Actor:
    return service.Actor(session.actor, session.user_name or None, session.can_confirm)


def _item_response(item) -> OrderClosureItemResponse:
    return OrderClosureItemResponse(
        id=item.id,
        position=item.position,
        input_number=item.input_number,
        input_period=item.input_period,
        onec_order_ref=item.onec_order_ref,
        onec_order_number=item.onec_order_number,
        onec_order_date=item.onec_order_date,
        site_order_number=item.site_order_number,
        department_name=item.department_name,
        status=item.status,
        eligible=item.eligible,
        blocker_code=item.blocker_code,
        blocker_text=item.blocker_text,
        facts=item.facts or {},
        state_hash=item.state_hash,
        reason_code=item.reason_code,
        reason_ref=item.reason_ref,
        reason_name=item.reason_name,
        result_document_ref=item.result_document_ref,
        result_document_number=item.result_document_number,
    )


def _batch_response(batch) -> OrderClosureBatchResponse:
    return OrderClosureBatchResponse(
        id=batch.public_id,
        status=batch.status,
        source_type=batch.source_type,
        actor_id=batch.actor_id,
        actor_name=batch.actor_name,
        confirmed_by=batch.confirmed_by,
        diagnosis_hash=batch.diagnosis_hash,
        command_kind=batch.command_kind,
        attempt_count=batch.attempt_count,
        last_error_code=batch.last_error_code,
        last_polled_at=batch.last_polled_at,
        lease_until=batch.lease_until,
        applied_at=batch.applied_at,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        items=[_item_response(item) for item in batch.items],
    )


def _handle_service_error(exc: service.OrderClosureError) -> None:
    if isinstance(exc, service.OrderClosureNotFound):
        raise HTTPException(status_code=404, detail=exc.code) from exc
    if isinstance(exc, service.OrderClosureForbidden):
        raise HTTPException(status_code=403, detail=exc.code) from exc
    raise HTTPException(status_code=409, detail=exc.code) from exc


@router.post("/session", response_model=OrderClosureSessionResponse)
def create_session(payload: OrderClosureSessionRequest) -> OrderClosureSessionResponse:
    settings = get_settings()
    domain, member_id = ensure_order_closure_launch_allowed(
        domain=payload.domain, member_id=payload.member_id, settings=settings
    )
    user = load_bitrix_current_user(
        domain=domain, access_token=payload.access_token, settings=settings
    )
    user_id, is_operator = ensure_order_closure_user_allowed(user.user_id, settings=settings)
    can_confirm = is_operator and settings.order_closure_apply_enabled
    token, expires_at = create_order_closure_session_token(
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=user.name,
        can_confirm=can_confirm,
        settings=settings,
    )
    return OrderClosureSessionResponse(
        session_token=token,
        expires_at=expires_at,
        expires_in=settings.order_closure_bitrix_session_ttl_seconds,
        user=OrderClosureSessionUser(
            user_id=user_id,
            name=user.name,
            role="order_closure_operator" if is_operator else "viewer",
            can_confirm=can_confirm,
        ),
    )


Access = Annotated[OrderClosureSession, Depends(verify_order_closure_session)]


@router.post("/batches", response_model=OrderClosureBatchResponse)
def create_batch(
    payload: OrderClosureBatchCreateRequest, access: Access, db: Session = Depends(get_db)
):
    try:
        batch = service.create_batch(db, payload=payload, actor=_actor(access))
        db.commit()
        db.refresh(batch)
        return _batch_response(batch)
    except service.OrderClosureError as exc:
        db.rollback()
        _handle_service_error(exc)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="order_closure_storage_unavailable") from exc


@router.post("/batches/{batch_id}/diagnose", response_model=OrderClosureBatchResponse)
def diagnose_batch(batch_id: str, access: Access, db: Session = Depends(get_db)):
    try:
        batch = service.request_diagnosis(
            db, batch=service.get_batch(db, batch_id), actor=_actor(access)
        )
        db.commit()
        return _batch_response(batch)
    except service.OrderClosureError as exc:
        db.rollback()
        _handle_service_error(exc)


@router.post("/batches/{batch_id}/confirm", response_model=OrderClosureBatchResponse)
def confirm_batch(
    batch_id: str,
    payload: OrderClosureConfirmRequest,
    access: Access,
    db: Session = Depends(get_db),
):
    try:
        batch = service.confirm_batch(
            db, batch=service.get_batch(db, batch_id), payload=payload, actor=_actor(access)
        )
        db.commit()
        return _batch_response(batch)
    except service.OrderClosureError as exc:
        db.rollback()
        _handle_service_error(exc)


@router.get("/batches/{batch_id}", response_model=OrderClosureBatchResponse)
def read_batch(batch_id: str, access: Access, db: Session = Depends(get_db)):
    try:
        batch = service.get_batch(db, batch_id)
        if batch.actor_id != access.actor and not access.can_confirm:
            raise service.OrderClosureForbidden("batch is not visible")
        return _batch_response(batch)
    except service.OrderClosureError as exc:
        _handle_service_error(exc)


@router.get("/candidates", response_model=OrderClosureCandidateResponse)
def candidates(access: Access, batch_id: str = Query(...), db: Session = Depends(get_db)):
    try:
        batch = service.get_batch(db, batch_id)
        if batch.actor_id != access.actor and not access.can_confirm:
            raise service.OrderClosureForbidden("batch is not visible")
        if batch.status != "diagnosed" or not batch.diagnosis_hash:
            raise service.OrderClosureConflict("diagnosis is not ready")
        return OrderClosureCandidateResponse(
            batch_id=batch.public_id,
            diagnosis_hash=batch.diagnosis_hash,
            items=[_item_response(item) for item in batch.items],
        )
    except service.OrderClosureError as exc:
        _handle_service_error(exc)


@router.get("/reasons", response_model=list[OrderClosureReasonResponse])
def reasons(access: Access, batch_id: str = Query(...), db: Session = Depends(get_db)):
    try:
        batch = service.get_batch(db, batch_id)
        if batch.actor_id != access.actor and not access.can_confirm:
            raise service.OrderClosureForbidden("batch is not visible")
        refs: dict[str, str | None] = {"execution": None, "cancellation": None}
        for item in batch.items:
            allowed = (item.facts or {}).get("allowed_reasons")
            if isinstance(allowed, dict):
                for code in refs:
                    value = allowed.get(code)
                    if isinstance(value, dict) and value.get("ref"):
                        refs[code] = str(value["ref"])
        return [
            OrderClosureReasonResponse(code=code, name=name, ref=refs[code])
            for code, name in service.ALLOWED_REASONS.items()
        ]
    except service.OrderClosureError as exc:
        _handle_service_error(exc)


def require_internal_token(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> str:
    return _require_bearer_token(
        credentials,
        get_settings().order_closure_internal_api_token,
        missing_detail="order closure internal token not configured",
    )


@router.get("/internal/commands", response_class=Response)
def commands(
    limit: int = Query(default=1, ge=1, le=10),
    allow_apply: bool = Query(default=False),
    _token: str = Depends(require_internal_token),
    db: Session = Depends(get_db),
) -> Response:
    try:
        rows = service.lease_commands(db, limit=limit, allow_apply=allow_apply)
        body = service.render_commands_xml(rows)
        db.commit()
        return Response(content=body, media_type="application/xml")
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="order_closure_storage_unavailable") from exc


@router.post("/internal/commands/{batch_id}/ack", response_model=OrderClosureCommandAckResponse)
def ack_command(
    batch_id: str,
    payload: OrderClosureCommandAckRequest,
    _token: str = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    try:
        batch, duplicate = service.acknowledge_command(
            db, batch=service.get_batch(db, batch_id), payload=payload
        )
        db.commit()
        return OrderClosureCommandAckResponse(
            batch_id=batch.public_id, status=batch.status, duplicate=duplicate
        )
    except service.OrderClosureError as exc:
        db.rollback()
        _handle_service_error(exc)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="order_closure_storage_unavailable") from exc


@router.post(
    "/internal/commands/{batch_id}/ack.xml",
    response_model=OrderClosureCommandAckResponse,
    include_in_schema=False,
)
async def ack_command_xml(
    batch_id: str,
    request: Request,
    _token: str = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    try:
        payload = service.ack_payload_from_xml(await request.body())
        batch, duplicate = service.acknowledge_command(
            db, batch=service.get_batch(db, batch_id), payload=payload
        )
        db.commit()
        return OrderClosureCommandAckResponse(
            batch_id=batch.public_id, status=batch.status, duplicate=duplicate
        )
    except service.OrderClosureError as exc:
        db.rollback()
        _handle_service_error(exc)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="order_closure_storage_unavailable") from exc
