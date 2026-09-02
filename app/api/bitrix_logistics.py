from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.api.dependencies import get_db
from app.api.logistics_web import COOKIE_NAME, _create_session_token, _profile
from app.api.procurement_labels import (
    _bitrix_launch_payload,
    _inject_launch_payload,
    _read_index,
    _rewrite_index_asset_paths,
)
from app.core.config import get_settings
from app.models import (
    LogisticsDraft,
    LogisticsTransfer,
    LogisticsTransferState,
    LogisticsUser,
    LogisticsWebLaunchToken,
)
from app.schemas.bitrix_logistics import (
    BitrixCustomerReturnCreateRequest,
    BitrixCustomerReturnDealLinkRequest,
    BitrixCustomerReturnDealSearchItem,
    BitrixCustomerReturnPickupRequest,
    BitrixLogisticsBootstrapResponse,
    BitrixLogisticsDraftCancelRequest,
    BitrixLogisticsDraftConfirmRequest,
    BitrixLogisticsDraftCreateRequest,
    BitrixLogisticsDraftScanRequest,
    BitrixLogisticsFallbackLinkResponse,
    BitrixLogisticsFallbackSessionRequest,
    BitrixLogisticsManualReviewPage,
    BitrixLogisticsSessionRequest,
    BitrixLogisticsSessionResponse,
)
from app.schemas.customer_returns import (
    CustomerReturnCarrier,
    CustomerReturnDetailResponse,
    CustomerReturnRegistrationResponse,
    CustomerReturnShipmentResponse,
    CustomerReturnStatus,
)
from app.schemas.logistics import (
    LogisticsConfirmResponse,
    LogisticsDraftResponse,
    LogisticsExpectedDeliveryResponse,
    LogisticsHistoryEventResponse,
    LogisticsMonitorResponse,
)
from app.services import customer_return_deals as customer_return_deal_service
from app.services import customer_returns as customer_return_service
from app.services import logistics as logistics_service
from app.services.bitrix_logistics_auth import (
    LogisticsBitrixSession,
    create_logistics_bitrix_session_token,
    ensure_logistics_bitrix_launch_allowed,
    load_bitrix_current_user,
    verify_logistics_bitrix_session,
    verify_logistics_bitrix_session_token,
)
from app.services.customer_return_carriers import CustomerReturnCarrierError

router = APIRouter(prefix="/bitrix/logistics")
page_router = APIRouter()
ALLOWED_LOGISTICS_ROLES = {"sender", "receiver", "logist", "returns", "admin"}
CUSTOMER_RETURN_ROLES = {"returns", "admin"}
BITRIX_SESSION_COOKIE_NAME = "mm_logistics_bitrix_session"
_LOGISTICS_INDEX_PATHS = (
    Path(__file__).resolve().parents[2] / "ui" / "dist" / "logistics.html",
    Path("/var/www/pricing-service/logistics.html"),
)


def _read_logistics_index() -> str:
    for path in _LOGISTICS_INDEX_PATHS:
        if path.exists():
            return _rewrite_index_asset_paths(path.read_text(encoding="utf-8"))
    return _read_index()


@page_router.api_route(
    "/bitrix/logistics",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/logistics/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/logistics/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_logistics_page(request: Request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    response = HTMLResponse(_inject_launch_payload(_read_logistics_index(), payload))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["X-MM-Logistics-Entry"] = "fast-launch-v1"
    return response


def _load_actor_from_session(
    db: Session,
    bitrix_session: LogisticsBitrixSession,
) -> LogisticsUser:
    actor = db.scalar(
        select(LogisticsUser)
        .where(
            LogisticsUser.id == bitrix_session.actor_user_id,
            LogisticsUser.bitrix_user_id == bitrix_session.bitrix_user_id,
            LogisticsUser.is_active.is_(True),
        )
        .options(joinedload(LogisticsUser.default_warehouse))
    )
    if actor is None:
        raise HTTPException(status_code=401, detail="logistics profile is not active")
    if actor.role not in ALLOWED_LOGISTICS_ROLES:
        raise HTTPException(status_code=403, detail="logistics profile role is not supported")
    return actor


def _actor_from_session(
    bitrix_session: LogisticsBitrixSession = Depends(verify_logistics_bitrix_session),
    db: Session = Depends(get_db),
) -> LogisticsUser:
    return _load_actor_from_session(db, bitrix_session)


def _session_response(
    *,
    token: str,
    expires_at: datetime,
    actor: LogisticsUser,
) -> BitrixLogisticsSessionResponse:
    expires_in = max(1, int((expires_at - datetime.now(UTC)).total_seconds()))
    return BitrixLogisticsSessionResponse(
        session_token=token,
        expires_at=expires_at,
        expires_in=expires_in,
        profile=_profile(actor),
    )


def _set_bitrix_session_cookie(
    response: Response,
    *,
    token: str,
    max_age: int,
) -> None:
    settings = get_settings()
    response.set_cookie(
        BITRIX_SESSION_COOKIE_NAME,
        token,
        max_age=max_age,
        path="/api/bitrix/logistics",
        httponly=True,
        secure=not settings.debug,
        samesite="lax" if settings.debug else "none",
    )


def _effective_warehouse_id(
    db: Session,
    actor: LogisticsUser,
    requested: int | None,
) -> int:
    if actor.role in {"logist", "admin"} and requested is not None:
        warehouse_id = requested
    else:
        if actor.default_warehouse_id is None:
            raise HTTPException(
                status_code=422,
                detail="default logistics warehouse is not configured",
            )
        if requested not in (None, actor.default_warehouse_id):
            raise HTTPException(status_code=403, detail="warehouse is not allowed for user")
        warehouse_id = actor.default_warehouse_id
    return logistics_service.require_warehouse_in_scope(
        db,
        warehouse_id=warehouse_id,
        allowed_external_ids=get_settings().logistics_stage_pilot_warehouse_external_ids,
    )


def _monitor_warehouse_scope(
    db: Session,
    actor: LogisticsUser,
    requested: int | None,
) -> tuple[int | None, list[int] | None]:
    if actor.role in {"logist", "admin"}:
        if requested is None:
            return None, logistics_service.warehouse_ids_in_scope(
                db,
                allowed_external_ids=get_settings().logistics_stage_pilot_warehouse_external_ids,
            )
        return (
            logistics_service.require_warehouse_in_scope(
                db,
                warehouse_id=requested,
                allowed_external_ids=get_settings().logistics_stage_pilot_warehouse_external_ids,
            ),
            None,
        )
    return _effective_warehouse_id(db, actor, requested), None


def _require_role(actor: LogisticsUser, allowed: set[str]) -> None:
    if actor.role not in allowed:
        raise HTTPException(status_code=403, detail="operation is not allowed for logistics role")


def _raise_customer_return_http_error(exc: Exception) -> None:
    if isinstance(exc, customer_return_service.CustomerReturnNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, customer_return_service.CustomerReturnConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, CustomerReturnCarrierError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, customer_return_deal_service.CustomerReturnDealNotFound):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, customer_return_deal_service.CustomerReturnDealUnavailable):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise exc


def _require_draft_type(db: Session, draft_id: int, expected_type: str) -> None:
    actual_type = db.scalar(select(LogisticsDraft.draft_type).where(LogisticsDraft.id == draft_id))
    if actual_type is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if actual_type != expected_type:
        raise HTTPException(status_code=409, detail="draft type does not match endpoint")


def _require_draft_in_pilot(db: Session, draft_id: int) -> None:
    warehouse_id = db.scalar(
        select(LogisticsDraft.warehouse_id).where(LogisticsDraft.id == draft_id)
    )
    if warehouse_id is None:
        raise HTTPException(status_code=404, detail="draft not found")
    logistics_service.require_warehouse_in_scope(
        db,
        warehouse_id=warehouse_id,
        allowed_external_ids=get_settings().logistics_stage_pilot_warehouse_external_ids,
    )


def _require_transfer_visible(db: Session, actor: LogisticsUser, transfer_id: int) -> None:
    if actor.role in {"logist", "admin"}:
        logistics_service.require_transfer_in_warehouse_scope(
            db,
            transfer_id=transfer_id,
            allowed_external_ids=get_settings().logistics_stage_pilot_warehouse_external_ids,
        )
        return
    if actor.default_warehouse_id is None:
        raise HTTPException(status_code=403, detail="default logistics warehouse is not configured")
    transfer = db.scalar(
        select(LogisticsTransfer)
        .where(LogisticsTransfer.id == transfer_id)
        .options(joinedload(LogisticsTransfer.state))
    )
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    state: LogisticsTransferState | None = transfer.state
    if actor.role == "sender":
        visible = transfer.source_warehouse_id == actor.default_warehouse_id or (
            state is not None and state.current_warehouse_id == actor.default_warehouse_id
        )
    else:
        visible = transfer.target_warehouse_id == actor.default_warehouse_id or (
            state is not None
            and (
                state.current_warehouse_id == actor.default_warehouse_id
                or state.dropoff_warehouse_id == actor.default_warehouse_id
            )
        )
    if not visible:
        raise HTTPException(status_code=403, detail="transfer is outside assigned warehouse")


@router.post("/session", response_model=BitrixLogisticsSessionResponse)
def create_bitrix_logistics_session(
    payload: BitrixLogisticsSessionRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> BitrixLogisticsSessionResponse:
    settings = get_settings()
    domain, member_id = ensure_logistics_bitrix_launch_allowed(
        domain=payload.domain,
        member_id=payload.member_id,
        settings=settings,
    )
    bitrix_user = load_bitrix_current_user(
        domain=domain,
        access_token=payload.access_token,
        settings=settings,
    )
    actor = db.scalar(
        select(LogisticsUser)
        .where(
            LogisticsUser.bitrix_user_id == bitrix_user.user_id,
            LogisticsUser.is_active.is_(True),
        )
        .options(joinedload(LogisticsUser.default_warehouse))
    )
    if actor is None:
        raise HTTPException(
            status_code=403, detail="Bitrix user is not mapped to logistics profile"
        )
    if actor.role not in ALLOWED_LOGISTICS_ROLES:
        raise HTTPException(status_code=403, detail="logistics profile role is not supported")
    token, expires_at = create_logistics_bitrix_session_token(
        actor_user_id=actor.id,
        domain=domain,
        member_id=member_id,
        bitrix_user_id=bitrix_user.user_id,
        settings=settings,
    )
    _set_bitrix_session_cookie(
        response,
        token=token,
        max_age=settings.logistics_bitrix_session_ttl_seconds,
    )
    return _session_response(token=token, expires_at=expires_at, actor=actor)


@router.get("/session/resume", response_model=BitrixLogisticsSessionResponse)
def resume_bitrix_logistics_session(
    token: str | None = Cookie(default=None, alias=BITRIX_SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> BitrixLogisticsSessionResponse:
    if not token:
        raise HTTPException(status_code=401, detail="logistics session cookie is missing")
    bitrix_session = verify_logistics_bitrix_session_token(token)
    actor = _load_actor_from_session(db, bitrix_session)
    return _session_response(
        token=token,
        expires_at=bitrix_session.expires_at,
        actor=actor,
    )


@router.get("/bootstrap", response_model=BitrixLogisticsBootstrapResponse)
def bootstrap(
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
) -> BitrixLogisticsBootstrapResponse:
    capabilities = {
        "sender": ["handoff", "monitor", "history"],
        "receiver": ["receipt", "expected", "monitor", "history"],
        "logist": ["expected", "monitor", "history", "errors"],
        "returns": ["customer_returns"],
        "admin": [
            "handoff",
            "receipt",
            "expected",
            "monitor",
            "history",
            "errors",
            "customer_returns",
        ],
    }.get(actor.role, [])
    settings = get_settings()
    warehouses = logistics_service.list_warehouses(
        db,
        allowed_external_ids=settings.logistics_stage_pilot_warehouse_external_ids,
    )
    if actor.role not in {"logist", "admin"}:
        warehouses = [
            warehouse for warehouse in warehouses if warehouse["id"] == actor.default_warehouse_id
        ]
    return BitrixLogisticsBootstrapResponse(
        profile=_profile(actor),
        warehouses=warehouses,
        drivers=logistics_service.list_drivers(db),
        capabilities=capabilities,
        open_draft=logistics_service.get_open_draft_for_actor(
            db,
            actor_user_id=actor.id,
        ),
    )


@router.post(
    "/customer-returns",
    response_model=CustomerReturnRegistrationResponse,
)
def register_bitrix_customer_return(
    payload: BitrixCustomerReturnCreateRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, CUSTOMER_RETURN_ROLES)
    try:
        deal_link = (
            customer_return_deal_service.get_customer_return_deal(
                webhook_url=get_settings().customer_return_bitrix_webhook_url,
                deal_id=payload.bitrix_deal_id,
            )
            if payload.bitrix_deal_id is not None
            else None
        )
        shipment, created = customer_return_service.register_return(
            db,
            carrier=payload.carrier,
            tracking_number=payload.tracking_number,
            source="bitrix_ui",
            source_ref=payload.source_ref,
            bitrix_case_id=payload.bitrix_case_id,
            site_ticket_id=payload.site_ticket_id,
            onec_order_ref=payload.onec_order_ref,
            created_by_bitrix_user_id=str(actor.bitrix_user_id),
            deal_link=deal_link,
        )
    except (
        customer_return_service.CustomerReturnConflict,
        CustomerReturnCarrierError,
        customer_return_deal_service.CustomerReturnDealNotFound,
        customer_return_deal_service.CustomerReturnDealUnavailable,
    ) as exc:
        _raise_customer_return_http_error(exc)
    return {"created": created, "shipment": shipment}


@router.get(
    "/customer-return-deals",
    response_model=list[BitrixCustomerReturnDealSearchItem],
)
def search_bitrix_customer_return_deals(
    search: str = Query(min_length=2, max_length=100),
    limit: int = Query(default=20, ge=1, le=20),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, CUSTOMER_RETURN_ROLES)
    try:
        deals = customer_return_deal_service.search_customer_return_deals(
            webhook_url=get_settings().customer_return_bitrix_webhook_url,
            search=search,
            limit=limit,
        )
    except customer_return_deal_service.CustomerReturnDealUnavailable as exc:
        _raise_customer_return_http_error(exc)
    return [
        {
            "deal_id": deal.deal_id,
            "title": deal.title,
            "order_ref": deal.order_ref,
            "stage_id": deal.stage_id,
            "stage_name": deal.stage_name,
            "closed": deal.closed,
            "created_at": deal.created_at,
            "contact_id": deal.contact_id,
            "contact_name": deal.contact_name,
            "company_id": deal.company_id,
            "company_name": deal.company_name,
            "responsible_user_id": deal.responsible_user_id,
            "responsible_name": deal.responsible_name,
        }
        for deal in deals
    ]


@router.get(
    "/customer-returns",
    response_model=list[CustomerReturnShipmentResponse],
)
def list_bitrix_customer_returns(
    carrier: CustomerReturnCarrier | None = Query(default=None),
    status: CustomerReturnStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, CUSTOMER_RETURN_ROLES)
    return customer_return_service.list_returns(
        db,
        carrier=carrier,
        status=status,
        limit=limit,
    )


@router.get(
    "/customer-returns/{shipment_id}",
    response_model=CustomerReturnDetailResponse,
)
def get_bitrix_customer_return(
    shipment_id: int,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, CUSTOMER_RETURN_ROLES)
    try:
        return customer_return_service.get_return(db, shipment_id)
    except customer_return_service.CustomerReturnNotFound as exc:
        _raise_customer_return_http_error(exc)


@router.put(
    "/customer-returns/{shipment_id}/deal-link",
    response_model=CustomerReturnDetailResponse,
)
def update_bitrix_customer_return_deal_link(
    shipment_id: int,
    payload: BitrixCustomerReturnDealLinkRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, CUSTOMER_RETURN_ROLES)
    try:
        deal_link = (
            customer_return_deal_service.get_customer_return_deal(
                webhook_url=get_settings().customer_return_bitrix_webhook_url,
                deal_id=payload.bitrix_deal_id,
            )
            if payload.bitrix_deal_id is not None
            else None
        )
        return customer_return_service.update_return_deal_link(
            db,
            shipment_id,
            deal_link=deal_link,
            actor_bitrix_user_id=str(actor.bitrix_user_id),
        )
    except (
        customer_return_service.CustomerReturnNotFound,
        customer_return_deal_service.CustomerReturnDealNotFound,
        customer_return_deal_service.CustomerReturnDealUnavailable,
    ) as exc:
        _raise_customer_return_http_error(exc)


@router.post(
    "/customer-returns/{shipment_id}/pickup",
    response_model=CustomerReturnDetailResponse,
)
def confirm_bitrix_customer_return_pickup(
    shipment_id: int,
    payload: BitrixCustomerReturnPickupRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, CUSTOMER_RETURN_ROLES)
    try:
        return customer_return_service.confirm_pickup(
            db,
            shipment_id,
            actor_bitrix_user_id=str(actor.bitrix_user_id),
            occurred_at=datetime.now(UTC),
            idempotency_key=payload.idempotency_key,
            comment=payload.comment,
        )
    except customer_return_service.CustomerReturnNotFound as exc:
        _raise_customer_return_http_error(exc)


@router.post("/handoffs/draft", response_model=LogisticsDraftResponse)
def create_handoff_draft(
    payload: BitrixLogisticsDraftCreateRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"sender", "admin"})
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_HANDOFF,
        actor_user_id=actor.id,
        warehouse_id=_effective_warehouse_id(db, actor, payload.warehouse_id),
        driver_id=payload.driver_id,
        route_run_id=payload.route_run_id,
        default_dropoff_warehouse_id=None,
        comment=payload.comment,
    )


@router.post("/handoffs/draft/{draft_id}/scan", response_model=LogisticsDraftResponse)
def scan_handoff_draft(
    draft_id: int,
    payload: BitrixLogisticsDraftScanRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"sender", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    _require_draft_in_pilot(db, draft_id)
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        barcode=payload.barcode,
        lookup_code=payload.lookup_code,
        dropoff_warehouse_id=None,
    )


@router.post(
    "/handoffs/draft/{draft_id}/items/{item_id}/remove",
    response_model=LogisticsDraftResponse,
)
def remove_handoff_draft_item(
    draft_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"sender", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    _require_draft_in_pilot(db, draft_id)
    return logistics_service.remove_scan_from_draft(
        db,
        draft_id=draft_id,
        item_id=item_id,
        actor_user_id=actor.id,
    )


@router.post("/handoffs/draft/{draft_id}/cancel", response_model=LogisticsDraftResponse)
def cancel_handoff_draft(
    draft_id: int,
    payload: BitrixLogisticsDraftCancelRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"sender", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    return logistics_service.cancel_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        reason=payload.reason,
    )


@router.post("/handoffs/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def confirm_handoff_draft(
    draft_id: int,
    payload: BitrixLogisticsDraftConfirmRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"sender", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    _require_draft_in_pilot(db, draft_id)
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[],
        source_channel="bitrix",
    )


@router.post("/receipts/draft", response_model=LogisticsDraftResponse)
def create_receipt_draft(
    payload: BitrixLogisticsDraftCreateRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"receiver", "admin"})
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_RECEIPT,
        actor_user_id=actor.id,
        warehouse_id=_effective_warehouse_id(db, actor, payload.warehouse_id),
        route_run_id=payload.route_run_id,
        comment=payload.comment,
    )


@router.post("/receipts/draft/{draft_id}/scan", response_model=LogisticsDraftResponse)
def scan_receipt_draft(
    draft_id: int,
    payload: BitrixLogisticsDraftScanRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"receiver", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    _require_draft_in_pilot(db, draft_id)
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        barcode=payload.barcode,
        lookup_code=payload.lookup_code,
    )


@router.post(
    "/receipts/draft/{draft_id}/items/{item_id}/remove",
    response_model=LogisticsDraftResponse,
)
def remove_receipt_draft_item(
    draft_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"receiver", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    _require_draft_in_pilot(db, draft_id)
    return logistics_service.remove_scan_from_draft(
        db,
        draft_id=draft_id,
        item_id=item_id,
        actor_user_id=actor.id,
    )


@router.post("/receipts/draft/{draft_id}/cancel", response_model=LogisticsDraftResponse)
def cancel_receipt_draft(
    draft_id: int,
    payload: BitrixLogisticsDraftCancelRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"receiver", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    return logistics_service.cancel_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        reason=payload.reason,
    )


@router.post("/receipts/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def confirm_receipt_draft(
    draft_id: int,
    payload: BitrixLogisticsDraftConfirmRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"receiver", "admin"})
    _require_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    _require_draft_in_pilot(db, draft_id)
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[],
        source_channel="bitrix",
    )


@router.get("/expected-deliveries", response_model=list[LogisticsExpectedDeliveryResponse])
def expected_deliveries(
    warehouse_id: int | None = Query(default=None),
    driver_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"receiver", "logist", "admin"})
    selected_warehouse_id, pilot_warehouse_ids = _monitor_warehouse_scope(db, actor, warehouse_id)
    return logistics_service.list_expected_deliveries(
        db,
        warehouse_id=selected_warehouse_id,
        warehouse_ids=pilot_warehouse_ids,
        driver_id=driver_id,
    )


@router.get("/monitor", response_model=list[LogisticsMonitorResponse])
def monitor(
    status: str | None = Query(default=None),
    warehouse_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    selected_warehouse_id, pilot_warehouse_ids = _monitor_warehouse_scope(db, actor, warehouse_id)
    return logistics_service.list_monitor(
        db,
        status=status,
        warehouse_id=selected_warehouse_id,
        warehouse_ids=pilot_warehouse_ids,
    )


@router.get("/transfers/{transfer_id}/history", response_model=list[LogisticsHistoryEventResponse])
def history(
    transfer_id: int,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_transfer_visible(db, actor, transfer_id)
    return logistics_service.get_transfer_history(db, transfer_id=transfer_id)


@router.get(
    "/errors",
    response_model=BitrixLogisticsManualReviewPage,
    response_model_exclude_none=True,
)
def errors(
    review_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
):
    _require_role(actor, {"logist", "admin"})
    settings = get_settings()
    return logistics_service.list_bitrix_manual_reviews(
        db,
        review_type=review_type,
        pilot_warehouse_external_ids=settings.logistics_stage_pilot_warehouse_external_ids,
        limit=limit,
        offset=offset,
    )


@router.post("/fallback-link", response_model=BitrixLogisticsFallbackLinkResponse)
def create_fallback_link(
    request: Request,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(_actor_from_session),
) -> BitrixLogisticsFallbackLinkResponse:
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)
    created_at = datetime.now(UTC).replace(tzinfo=None)
    expires_at = created_at + timedelta(seconds=settings.logistics_web_fallback_token_ttl_seconds)
    db.add(
        LogisticsWebLaunchToken(
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            actor_user_id=actor.id,
            expires_at=expires_at,
            created_at=created_at,
        )
    )
    db.commit()
    base_url = str(request.base_url).rstrip("/")
    return BitrixLogisticsFallbackLinkResponse(
        url=f"{base_url}/logistics/fallback?launch={quote(raw_token)}",
        expires_at=expires_at.replace(tzinfo=UTC),
    )


@router.post("/fallback-session")
def exchange_fallback_token(
    payload: BitrixLogisticsFallbackSessionRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    now = datetime.now(UTC).replace(tzinfo=None)
    launch = db.scalar(
        select(LogisticsWebLaunchToken)
        .where(
            LogisticsWebLaunchToken.token_hash == token_hash,
            LogisticsWebLaunchToken.consumed_at.is_(None),
            LogisticsWebLaunchToken.expires_at > now,
        )
        .with_for_update()
    )
    if launch is None:
        raise HTTPException(status_code=401, detail="fallback link is invalid or expired")
    actor = db.scalar(
        select(LogisticsUser)
        .where(LogisticsUser.id == launch.actor_user_id, LogisticsUser.is_active.is_(True))
        .options(joinedload(LogisticsUser.default_warehouse))
    )
    if actor is None:
        raise HTTPException(status_code=401, detail="logistics profile is not active")
    consumed = db.execute(
        update(LogisticsWebLaunchToken)
        .where(
            LogisticsWebLaunchToken.id == launch.id,
            LogisticsWebLaunchToken.consumed_at.is_(None),
            LogisticsWebLaunchToken.expires_at > now,
        )
        .values(consumed_at=now)
    )
    if consumed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="fallback link is invalid or expired")
    session_token, _expires_at = _create_session_token(actor.id)
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        session_token,
        max_age=settings.logistics_web_session_ttl_seconds,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
    )
    db.commit()
    return _profile(actor)
