"""Scoped read-only API for durable customer price-type state."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_management_internal_token, security
from app.api.procurement_labels import (
    _bitrix_launch_payload,
    _inject_launch_payload,
    _read_index,
)
from app.core.config import get_settings
from app.domains.customer_price_types import CustomerPriceTypeAccessScope
from app.infrastructure.customer_price_types import SqlAlchemyCustomerPriceTypeRepository
from app.schemas.customer_price_types import (
    CustomerPriceTypeCaseDetailResponse,
    CustomerPriceTypeCaseEventResponse,
    CustomerPriceTypeCaseItem,
    CustomerPriceTypeCaseListResponse,
    CustomerPriceTypeProfileResponse,
    CustomerPriceTypeRunResponse,
    CustomerPriceTypeSessionRequest,
    CustomerPriceTypeSessionResponse,
    CustomerPriceTypeSessionUser,
    CustomerPriceTypeSnapshotResponse,
    CustomerPriceTypeSummaryResponse,
    CustomerPriceTypeWorklistsResponse,
)
from app.services.bitrix_customer_price_types_auth import (
    create_customer_price_type_session_token,
    ensure_bitrix_launch_allowed,
    load_bitrix_current_user,
    load_bitrix_headed_department_ids,
    resolve_customer_price_type_access,
    verify_customer_price_type_session,
)
from app.services.customer_price_types import (
    CustomerPriceTypeReadService,
    internal_customer_price_type_scope,
)

router = APIRouter(prefix="/api/customer-price-types", tags=["customer-price-types"])
page_router = APIRouter()


@page_router.api_route(
    "/bitrix/customer-price-types",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/customer-price-types/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/customer-price-types/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_customer_price_types_page(request: Request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    return HTMLResponse(_inject_launch_payload(_read_index(), payload))


def require_customer_price_type_access(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> CustomerPriceTypeAccessScope:
    # Management internal token keeps full internal scope; otherwise fall back to a
    # Bitrix embedded-app session token mapped to a read-only role scope.
    try:
        require_management_internal_token(credentials)
    except HTTPException:
        return verify_customer_price_type_session(credentials)
    return internal_customer_price_type_scope(actor="internal")


Access = Annotated[CustomerPriceTypeAccessScope, Depends(require_customer_price_type_access)]


@router.post("/session", response_model=CustomerPriceTypeSessionResponse)
def create_customer_price_type_session(
    payload: CustomerPriceTypeSessionRequest,
) -> CustomerPriceTypeSessionResponse:
    settings = get_settings()
    domain, member_id = ensure_bitrix_launch_allowed(
        domain=payload.domain, member_id=payload.member_id, settings=settings
    )
    user = load_bitrix_current_user(
        domain=domain, access_token=payload.access_token, settings=settings
    )
    headed = load_bitrix_headed_department_ids(
        domain=domain, access_token=payload.access_token, user_id=user.user_id, settings=settings
    )
    access = resolve_customer_price_type_access(
        bitrix_user_id=user.user_id,
        department_ids=user.department_ids,
        headed_department_ids=headed,
        settings=settings,
    )
    token, expires_at_ts = create_customer_price_type_session_token(
        domain=domain,
        member_id=member_id,
        user_id=user.user_id,
        user_name=user.name,
        access=access,
        settings=settings,
    )
    return CustomerPriceTypeSessionResponse(
        session_token=token,
        expires_at=datetime.fromtimestamp(expires_at_ts, UTC),
        expires_in=settings.customer_price_type_bitrix_session_ttl_seconds,
        user=CustomerPriceTypeSessionUser(
            user_id=user.user_id,
            name=user.name,
            role=access.role,
            can_view_money=access.can_view_money,
        ),
    )


def _month(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="snapshot_month must be a valid YYYY-MM"
        ) from exc


def _snapshot_payload(row, access: CustomerPriceTypeAccessScope) -> dict:
    money = access.can_view_money
    return {
        "id": row.id,
        "run_id": row.run_id,
        "counterparty_ref": row.counterparty_ref,
        "snapshot_month": row.snapshot_month,
        "ruleset_version": row.ruleset_version,
        "current_price_type": row.current_price_type,
        "current_level": row.current_level,
        "price_type_variant": row.price_type_variant,
        "contract_candidates": row.contract_candidates,
        "monthly_sales": row.monthly_sales if money else None,
        "total_3m": row.total_3m if money else None,
        "last_month": row.last_month if money else None,
        "economics": row.economics if money else None,
        "payments": row.payments if money else None,
        "returns": row.returns if money else _redact_monetary(row.returns),
        "history": row.history,
        "source_status": row.source_status,
        "source_statuses": row.source_statuses,
        "conflicts": row.conflicts,
        "stop_factors": row.stop_factors,
        "system_recommendation": row.system_recommendation,
        "recommended_price_type": row.recommended_price_type,
        "recommendation_reason": row.recommendation_reason,
        "action_required": row.action_required,
        "case_type": row.case_type,
        "review_type": row.review_type,
        "reasons": row.reasons,
        "snapshot_hash": row.snapshot_hash,
        "money_visible": money,
    }


def _redact_monetary(value: Any) -> Any:
    if isinstance(value, dict):
        hidden_fragments = (
            "amount",
            "revenue",
            "sales",
            "profit",
            "gross",
            "margin",
            "payment",
            "debt",
            "cost",
            "руб",
        )
        return {
            key: _redact_monetary(item)
            for key, item in value.items()
            if not any(fragment in str(key).casefold() for fragment in hidden_fragments)
        }
    if isinstance(value, list):
        return [_redact_monetary(item) for item in value]
    return value


def _case_payload(case, profile, snapshot) -> dict:
    return {
        "id": case.id,
        "case_key": case.case_key,
        "counterparty_ref": profile.counterparty_ref,
        "counterparty_code": profile.counterparty_code,
        "counterparty_name": profile.counterparty_name,
        "snapshot_month": case.snapshot_month,
        "stage": case.stage,
        "case_type": case.case_type,
        "review_type": case.review_type,
        "reasons": case.reasons,
        "owner_ref": case.owner_ref,
        "owner_name": case.owner_name,
        "department_ref": case.department_ref,
        "department_name": case.department_name,
        "due_at": case.due_at,
        "system_recommendation": case.system_recommendation,
        "recommended_price_type": case.recommended_price_type,
        "human_final_decision": case.human_final_decision,
        "approval_status": case.approval_status,
        "action_required": snapshot.action_required,
        "snapshot_hash": snapshot.snapshot_hash,
        "version": case.version,
    }


@router.get("/summary", response_model=CustomerPriceTypeSummaryResponse)
def get_customer_price_type_summary(
    access: Access,
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
) -> CustomerPriceTypeSummaryResponse:
    try:
        return CustomerPriceTypeSummaryResponse.model_validate(
            CustomerPriceTypeReadService(db).summary(
                snapshot_month=_month(snapshot_month), access=access
            )
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc


@router.get("/worklists", response_model=CustomerPriceTypeWorklistsResponse)
def get_customer_price_type_worklists(
    access: Access,
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
) -> CustomerPriceTypeWorklistsResponse:
    try:
        return CustomerPriceTypeWorklistsResponse.model_validate(
            CustomerPriceTypeReadService(db).worklists(
                snapshot_month=_month(snapshot_month), access=access
            )
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc


@router.get("/cases", response_model=CustomerPriceTypeCaseListResponse)
def list_customer_price_type_cases(
    access: Access,
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    worklist: (
        Literal[
            "manager_work",
            "isolate",
            "recovery",
            "data_check",
            "special_review",
            "downgrade_approval",
        ]
        | None
    ) = None,
    stage: str | None = None,
    review_type: str | None = None,
    source_status: str | None = None,
    department_ref: str | None = None,
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CustomerPriceTypeCaseListResponse:
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        requested_month = _month(snapshot_month)
        run = repository.latest_run(requested_month)
        if run is None:
            rows, total = [], 0
        else:
            rows, total = repository.list_cases(
                access=access,
                run_id=run.id,
                snapshot_month=requested_month or run.snapshot_month,
                worklist=worklist,
                stage=stage,
                review_type=review_type,
                source_status=source_status,
                department_ref=department_ref,
                search=search,
                limit=limit,
                offset=offset,
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    envelope = {
        "run_id": run.id if run else None,
        "snapshot_month": run.snapshot_month if run else _month(snapshot_month),
        "ruleset_version": run.ruleset_version if run else None,
        "source_status": run.status if run else "missing",
    }
    return CustomerPriceTypeCaseListResponse(
        **envelope,
        total=total,
        limit=limit,
        offset=offset,
        payload=[CustomerPriceTypeCaseItem.model_validate(_case_payload(*row)) for row in rows],
    )


@router.get("/cases/{case_id}", response_model=CustomerPriceTypeCaseDetailResponse)
def get_customer_price_type_case(
    case_id: int,
    access: Access,
    db: Session = Depends(get_db),
) -> CustomerPriceTypeCaseDetailResponse:
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        row = repository.get_case_scoped(case_id, access)
        if row is None:
            raise HTTPException(status_code=404, detail="customer price-type case not found")
        events = repository.list_case_events(case_id)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    case, profile, snapshot = row
    return CustomerPriceTypeCaseDetailResponse(
        run_id=snapshot.run_id,
        snapshot_month=snapshot.snapshot_month,
        ruleset_version=snapshot.ruleset_version,
        source_status=snapshot.source_status,
        case=CustomerPriceTypeCaseItem.model_validate(_case_payload(case, profile, snapshot)),
        snapshot=CustomerPriceTypeSnapshotResponse.model_validate(
            _snapshot_payload(snapshot, access)
        ),
        events=[
            CustomerPriceTypeCaseEventResponse(
                id=event.id,
                event_type=event.event_type,
                event_at=event.event_at,
                actor=event.actor,
                source=event.source,
                before_status=event.before_status,
                after_status=event.after_status,
                comment=event.comment,
                metadata=event.metadata_json,
                idempotency_key=event.idempotency_key,
            )
            for event in events
        ],
    )


@router.get("/profiles/{counterparty_ref}", response_model=CustomerPriceTypeProfileResponse)
def get_customer_price_type_profile(
    counterparty_ref: str,
    access: Access,
    db: Session = Depends(get_db),
) -> CustomerPriceTypeProfileResponse:
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        profile = repository.get_profile_scoped(counterparty_ref, access)
        if profile is None:
            raise HTTPException(status_code=404, detail="customer price-type profile not found")
        history = repository.profile_snapshots(profile.id, access=access)
        latest = next((item for item in history if item.id == profile.latest_snapshot_id), None)
        if latest is None and history:
            latest = history[0]
        open_case_row = (
            repository.get_case_scoped(profile.open_case_id, access)
            if profile.open_case_id
            else None
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail="customer price-type profile not found"
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    return CustomerPriceTypeProfileResponse(
        run_id=latest.run_id if latest else None,
        snapshot_month=latest.snapshot_month if latest else None,
        ruleset_version=latest.ruleset_version if latest else None,
        source_status=latest.source_status if latest else "missing",
        id=profile.id,
        counterparty_ref=profile.counterparty_ref,
        counterparty_code=profile.counterparty_code,
        counterparty_name=profile.counterparty_name,
        department_ref=profile.department_ref,
        department_name=profile.department_name,
        owner_ref=profile.owner_ref,
        owner_name=profile.owner_name,
        is_service_card=profile.is_service_card,
        is_hygiene=profile.is_hygiene,
        master_data_flags=profile.master_data_flags,
        latest_snapshot=(
            CustomerPriceTypeSnapshotResponse.model_validate(_snapshot_payload(latest, access))
            if latest
            else None
        ),
        open_case=(
            CustomerPriceTypeCaseItem.model_validate(_case_payload(*open_case_row))
            if open_case_row
            else None
        ),
        history=[
            CustomerPriceTypeSnapshotResponse.model_validate(_snapshot_payload(item, access))
            for item in history
        ],
    )


@router.get("/runs/{run_id}", response_model=CustomerPriceTypeRunResponse)
def get_customer_price_type_run(
    run_id: int,
    access: Access,
    db: Session = Depends(get_db),
) -> CustomerPriceTypeRunResponse:
    if access.role not in {"internal", "network_head", "integration_operator"}:
        raise HTTPException(status_code=403, detail="customer price-type run access denied")
    try:
        row = SqlAlchemyCustomerPriceTypeRepository(db).get_run(run_id)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="customer price-type run not found")
    return CustomerPriceTypeRunResponse.model_validate(
        {
            **{column.name: getattr(row, column.name) for column in row.__table__.columns},
            "run_id": row.id,
            "source_status": row.status,
        }
    )
