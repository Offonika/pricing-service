from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.api.procurement_labels import _bitrix_launch_payload, _inject_launch_payload, _read_index
from app.core.config import get_settings
from app.schemas.procurement_order_formation import (
    ProcurementClassificationApprovalResponse,
    ProcurementClassificationCreateRequest,
    ProcurementClassificationQueueResponse,
    ProcurementDashboardResponse,
    ProcurementLifecycleTransitionApprovalRequest,
    ProcurementLifecycleTransitionApprovalResponse,
    ProcurementLifecycleTransitionList,
    ProcurementOrderConditionsUpdateRequest,
    ProcurementOrderFormationEventList,
    ProcurementOrderFormationRead,
    ProcurementOrderFormationSessionRequest,
    ProcurementOrderFormationSessionResponse,
    ProcurementOrderFormationUser,
    ProcurementOrderLineUpdateRequest,
    ProcurementOrderListResponse,
    ProcurementOrderTransmissionResponse,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
    create_procurement_order_formation_session_token,
    ensure_bitrix_launch_allowed,
    ensure_bitrix_user_allowed,
    load_bitrix_current_user,
    verify_procurement_order_formation_session,
)
from app.services.procurement_order_formation import (
    VersionConflictError,
    approve_classification_proposal,
    approve_order,
    create_classification_proposal,
    get_order,
    get_order_by_bitrix_item,
    serialize_order,
    serialize_proposal,
    transmit_order,
    update_order_conditions,
    update_order_line,
)
from app.services.procurement_order_formation_workspace import (
    approve_lifecycle_transitions,
    build_dashboard,
    list_classification_proposals,
    list_events,
    list_lifecycle_transitions,
    list_orders,
    record_event,
)

router = APIRouter(prefix="/procurement-order-formation")
page_router = APIRouter()


@page_router.api_route(
    "/bitrix/procurement-order-formation",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/procurement-order-formation/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/procurement-order-formation/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_procurement_order_formation_page(request: Request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    return HTMLResponse(_inject_launch_payload(_read_index(), payload))


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VersionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail="order formation service failed")


@router.post("/session", response_model=ProcurementOrderFormationSessionResponse)
def create_bitrix_procurement_order_formation_session(
    payload: ProcurementOrderFormationSessionRequest,
) -> ProcurementOrderFormationSessionResponse:
    settings = get_settings()
    domain, member_id = ensure_bitrix_launch_allowed(
        domain=payload.domain,
        member_id=payload.member_id,
        settings=settings,
    )
    user = load_bitrix_current_user(
        domain=domain,
        access_token=payload.access_token,
        settings=settings,
    )
    user_id = ensure_bitrix_user_allowed(user.user_id, settings=settings)
    token, expires_at = create_procurement_order_formation_session_token(
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=user.name,
        settings=settings,
    )
    return ProcurementOrderFormationSessionResponse(
        session_token=token,
        expires_at=expires_at,
        expires_in=settings.procurement_order_formation_bitrix_session_ttl_seconds,
        user=ProcurementOrderFormationUser(user_id=user_id, name=user.name),
    )


@router.get("/dashboard", response_model=ProcurementDashboardResponse)
def read_dashboard(
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementDashboardResponse:
    try:
        return ProcurementDashboardResponse.model_validate(build_dashboard(db))
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/lifecycle/transitions", response_model=ProcurementLifecycleTransitionList)
def read_lifecycle_transitions(
    status: str,
    scope: str = "action",
    readiness: str = "all",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementLifecycleTransitionList:
    try:
        return ProcurementLifecycleTransitionList.model_validate(
            list_lifecycle_transitions(
                db,
                status=status,
                scope=scope,
                readiness=readiness,
                search=search,
                page=page,
                page_size=page_size,
            )
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post(
    "/lifecycle/transitions/approve",
    response_model=ProcurementLifecycleTransitionApprovalResponse,
)
def approve_lifecycle_transition_batch(
    payload: ProcurementLifecycleTransitionApprovalRequest,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementLifecycleTransitionApprovalResponse:
    try:
        return ProcurementLifecycleTransitionApprovalResponse.model_validate(
            approve_lifecycle_transitions(
                db,
                items=[item.model_dump() for item in payload.items],
                idempotency_key=payload.idempotency_key,
                session=session,
            )
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/orders", response_model=ProcurementOrderListResponse)
def read_orders(
    search: str = "",
    status: str = "",
    supplier: str = "",
    blockers: str = "all",
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderListResponse:
    try:
        return ProcurementOrderListResponse.model_validate(
            list_orders(
                db,
                search=search,
                status=status,
                supplier=supplier,
                blockers=blockers,
                page=page,
                page_size=page_size,
            )
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get(
    "/classification-proposals",
    response_model=ProcurementClassificationQueueResponse,
)
def read_classification_proposals(
    status: str = "",
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementClassificationQueueResponse:
    try:
        return ProcurementClassificationQueueResponse.model_validate(
            list_classification_proposals(
                db,
                status=status,
                page=page,
                page_size=page_size,
            )
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/events", response_model=ProcurementOrderFormationEventList)
def read_events(
    order_id: int | None = None,
    event_type: str = "",
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderFormationEventList:
    try:
        return ProcurementOrderFormationEventList.model_validate(
            list_events(
                db,
                order_id=order_id,
                event_type=event_type,
                page=page,
                page_size=page_size,
            )
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get(
    "/orders/by-bitrix/{item_id}",
    response_model=ProcurementOrderFormationRead,
    include_in_schema=False,
)
def read_order_by_bitrix_item(
    item_id: str,
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderFormationRead:
    try:
        return ProcurementOrderFormationRead.model_validate(
            serialize_order(get_order_by_bitrix_item(db, item_id))
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/orders/{order_id}", response_model=ProcurementOrderFormationRead)
def read_order(
    order_id: int,
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderFormationRead:
    try:
        return ProcurementOrderFormationRead.model_validate(
            serialize_order(get_order(db, order_id))
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.patch("/orders/{order_id}", response_model=ProcurementOrderFormationRead)
def change_order_conditions(
    order_id: int,
    payload: ProcurementOrderConditionsUpdateRequest,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderFormationRead:
    try:
        before = serialize_order(get_order(db, order_id))
        order = update_order_conditions(db, order_id, payload.model_dump(exclude_unset=True))
        after = serialize_order(order)
        record_event(
            db,
            order_id=order_id,
            entity_type="order",
            entity_id=order_id,
            event_type="order_conditions_changed",
            session=session,
            before=before,
            after=after,
        )
        db.commit()
        return ProcurementOrderFormationRead.model_validate(after)
    except Exception as exc:
        raise _service_error(exc) from exc


@router.patch(
    "/orders/{order_id}/lines/{line_id}",
    response_model=ProcurementOrderFormationRead,
)
def change_order_line(
    order_id: int,
    line_id: int,
    payload: ProcurementOrderLineUpdateRequest,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderFormationRead:
    try:
        before = serialize_order(get_order(db, order_id))
        order = update_order_line(
            db,
            order_id,
            line_id,
            payload.model_dump(exclude_unset=True),
        )
        after = serialize_order(order)
        record_event(
            db,
            order_id=order_id,
            entity_type="order_line",
            entity_id=line_id,
            event_type="order_line_changed",
            session=session,
            before=before,
            after=after,
        )
        db.commit()
        return ProcurementOrderFormationRead.model_validate(after)
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post(
    "/orders/{order_id}/lines/{line_id}/classification",
    response_model=ProcurementOrderFormationRead,
)
def propose_line_classification(
    order_id: int,
    line_id: int,
    payload: ProcurementClassificationCreateRequest,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderFormationRead:
    try:
        order = create_classification_proposal(
            db,
            order_id,
            line_id,
            payload.model_dump(),
            session,
        )
        record_event(
            db,
            order_id=order_id,
            entity_type="order_line",
            entity_id=line_id,
            event_type="classification_proposed",
            session=session,
            after=serialize_order(order),
        )
        db.commit()
        return ProcurementOrderFormationRead.model_validate(serialize_order(order))
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post(
    "/orders/{order_id}/lines/{line_id}/classification/{proposal_id}/approve",
    response_model=ProcurementClassificationApprovalResponse,
)
def approve_line_classification(
    order_id: int,
    line_id: int,
    proposal_id: int,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementClassificationApprovalResponse:
    try:
        order, proposal, mode, xml_preview, written_path = approve_classification_proposal(
            db,
            order_id,
            line_id,
            proposal_id,
            session,
        )
        record_event(
            db,
            order_id=order_id,
            entity_type="classification",
            entity_id=proposal_id,
            event_type="classification_approved",
            session=session,
            after=serialize_proposal(proposal),
            payload={"mode": mode, "message_id": proposal.onec_message_id},
        )
        db.commit()
        return ProcurementClassificationApprovalResponse.model_validate(
            {
                "order": serialize_order(order),
                "proposal": serialize_proposal(proposal),
                "mode": mode,
                "message_id": proposal.onec_message_id,
                "xml_preview": xml_preview,
                "written_path": str(written_path) if written_path else None,
            }
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post(
    "/orders/{order_id}/approve",
    response_model=ProcurementOrderFormationRead,
    include_in_schema=False,
)
def approve_current_order_version(
    order_id: int,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderFormationRead:
    try:
        return ProcurementOrderFormationRead.model_validate(
            serialize_order(approve_order(db, order_id, session))
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post(
    "/orders/{order_id}/send-to-1c",
    response_model=ProcurementOrderTransmissionResponse,
)
def send_order_to_onec(
    order_id: int,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
) -> ProcurementOrderTransmissionResponse:
    try:
        order, mode, message_id, xml_preview, written_path = transmit_order(
            db,
            order_id,
            session,
        )
        record_event(
            db,
            order_id=order_id,
            entity_type="order",
            entity_id=order_id,
            event_type="order_checked_and_sent",
            session=session,
            after=serialize_order(order),
            payload={"mode": mode, "message_id": message_id},
            idempotency_key=f"order-send:{order_id}:v{order.version}",
        )
        db.commit()
        return ProcurementOrderTransmissionResponse.model_validate(
            {
                "order": serialize_order(order),
                "mode": mode,
                "message_id": message_id,
                "xml_preview": xml_preview,
                "written_path": str(written_path) if written_path else None,
            }
        )
    except Exception as exc:
        raise _service_error(exc) from exc
