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
from app.infrastructure.customer_price_types import (
    SqlAlchemyCustomerPriceTypeRepository,
    review_batch_item_matches,
    review_batch_snapshot_status,
)
from app.schemas.customer_price_types import (
    CustomerPriceTypeCaseDetailResponse,
    CustomerPriceTypeCaseEventResponse,
    CustomerPriceTypeCaseItem,
    CustomerPriceTypeCaseListResponse,
    CustomerPriceTypeDataIssueItem,
    CustomerPriceTypeDataIssueListResponse,
    CustomerPriceTypePortfolioBucket,
    CustomerPriceTypePortfolioItem,
    CustomerPriceTypePortfolioResponse,
    CustomerPriceTypeProfileResponse,
    CustomerPriceTypeProfileSearchItem,
    CustomerPriceTypeProfileSearchResponse,
    CustomerPriceTypeQualityGroup,
    CustomerPriceTypeQualityMetricsResponse,
    CustomerPriceTypeQualityPrepareRequest,
    CustomerPriceTypeQualityPrepareResponse,
    CustomerPriceTypeQualityProfileResponse,
    CustomerPriceTypeQualityReviewRequest,
    CustomerPriceTypeQualitySampleDetailResponse,
    CustomerPriceTypeQualitySampleListResponse,
    CustomerPriceTypeQualitySampleResponse,
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
    load_bitrix_headed_departments,
    resolve_customer_price_type_access,
    resolve_customer_price_type_department_refs,
    resolve_customer_price_type_manager_owner_ref,
    verify_customer_price_type_session,
)
from app.services.customer_price_types import (
    CustomerPriceTypeQualityConflict,
    CustomerPriceTypeQualityService,
    CustomerPriceTypeReadService,
    customer_price_type_case_guidance,
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
    db: Session = Depends(get_db),
) -> CustomerPriceTypeSessionResponse:
    settings = get_settings()
    domain, member_id = ensure_bitrix_launch_allowed(
        domain=payload.domain, member_id=payload.member_id, settings=settings
    )
    user = load_bitrix_current_user(
        domain=domain, access_token=payload.access_token, settings=settings
    )
    headed_departments = load_bitrix_headed_departments(
        domain=domain, access_token=payload.access_token, user_id=user.user_id, settings=settings
    )
    headed_refs = resolve_customer_price_type_department_refs(
        db,
        department_names={item.name for item in headed_departments},
    )
    manager_owner_ref = resolve_customer_price_type_manager_owner_ref(
        db,
        bitrix_user_id=user.user_id,
    )
    access = resolve_customer_price_type_access(
        bitrix_user_id=user.user_id,
        department_ids=user.department_ids,
        headed_department_ids=tuple(item.department_id for item in headed_departments),
        headed_department_refs=headed_refs,
        manager_owner_ref=manager_owner_ref,
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
    contract_candidates = []
    for item in row.contract_candidates:
        candidate = dict(item)
        if not money:
            candidate["sales_amount_12m"] = None
        contract_candidates.append(candidate)
    return {
        "id": row.id,
        "run_id": row.run_id,
        "counterparty_ref": row.counterparty_ref,
        "snapshot_month": row.snapshot_month,
        "ruleset_version": row.ruleset_version,
        "current_price_type": row.current_price_type,
        "current_level": row.current_level,
        "price_type_variant": row.price_type_variant,
        "contract_candidates": contract_candidates,
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
        "recommended_price_type": (
            None if row.case_type == "data_check" else row.recommended_price_type
        ),
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
    external_control_active = (
        case.onec_export_status == "exported"
        or case.onec_readback_status in {"pending", "mismatch", "error"}
    )
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
        "recommended_price_type": (
            None if case.case_type == "data_check" else case.recommended_price_type
        ),
        "human_final_decision": case.human_final_decision,
        "approval_status": case.approval_status,
        "action_required": bool(snapshot.action_required or external_control_active),
        "snapshot_hash": snapshot.snapshot_hash,
        "version": case.version,
    }


def _portfolio_payload(item, profile, snapshot, case, access) -> dict:
    if snapshot is None:
        actual_bucket = "review_queue"
        reconciliation_status = "missing_snapshot"
        working_contracts = []
    else:
        actual_bucket = (
            "working_bronze" if snapshot.current_price_type == "2.Бронзовый" else "review_queue"
        )
        reconciliation_status = "match" if review_batch_item_matches(item, snapshot) else "mismatch"
        working_contracts = []
        for raw in snapshot.contract_candidates:
            if not raw.get("is_working"):
                continue
            candidate = dict(raw)
            if not access.can_view_money:
                candidate["sales_amount_12m"] = None
            working_contracts.append(candidate)
    return {
        "counterparty_ref": profile.counterparty_ref,
        "counterparty_code": item.counterparty_code,
        "counterparty_name": profile.counterparty_name,
        "department_name": profile.department_name,
        "owner_name": profile.owner_name,
        "bucket": actual_bucket,
        "expected_bucket": item.expected_bucket,
        "expected_price_type": item.expected_price_type,
        "current_price_type": snapshot.current_price_type if snapshot else None,
        "price_type_variant": snapshot.price_type_variant if snapshot else None,
        "working_contracts": working_contracts,
        "action_required": (
            bool(
                snapshot.action_required
                or (
                    case is not None
                    and (
                        case.onec_export_status == "exported"
                        or case.onec_readback_status in {"pending", "mismatch", "error"}
                    )
                )
            )
            if snapshot
            else False
        ),
        "system_recommendation": snapshot.system_recommendation if snapshot else None,
        "recommended_price_type": (
            None
            if snapshot is None or snapshot.case_type == "data_check"
            else snapshot.recommended_price_type
        ),
        "source_status": snapshot.source_status if snapshot else "missing",
        "stop_factors": list(snapshot.stop_factors) if snapshot else [],
        "review_status": review_batch_snapshot_status(snapshot),
        "case_id": case.id if case else None,
        "case_type": case.case_type if case else None,
        "case_stage": case.stage if case else None,
        "reconciliation_status": reconciliation_status,
    }


def _quality_sample_payload(sample, profile, snapshot) -> dict:
    review_result = None
    if sample.status == "reviewed":
        if sample.correct_group == "data_check" and sample.system_group != "data_check":
            review_result = "data_issue"
        elif sample.correct_group == sample.system_group:
            review_result = "correct"
        else:
            review_result = "incorrect"
    return {
        "id": sample.id,
        "run_id": sample.run_id,
        "snapshot_id": sample.snapshot_id,
        "counterparty_ref": profile.counterparty_ref,
        "counterparty_code": profile.counterparty_code,
        "counterparty_name": profile.counterparty_name,
        "current_price_type": snapshot.current_price_type,
        "recommended_price_type": (
            None if snapshot.case_type == "data_check" else snapshot.recommended_price_type
        ),
        "system_recommendation": snapshot.system_recommendation,
        "recommendation_reason": snapshot.recommendation_reason,
        "stop_factors": snapshot.stop_factors,
        "system_group": sample.system_group,
        "correct_group": sample.correct_group,
        "review_result": review_result,
        "status": sample.status,
        "selected_by": sample.selected_by,
        "selected_at": sample.selected_at,
        "reviewed_by": sample.reviewed_by,
        "reviewed_at": sample.reviewed_at,
        "comment": sample.comment,
        "version": sample.version,
    }


def _ensure_quality_read_access(access: CustomerPriceTypeAccessScope) -> None:
    if access.role not in CustomerPriceTypeQualityService.READ_ROLES:
        raise HTTPException(status_code=403, detail="customer price-type quality access denied")


def _profile_search_payload(profile, snapshot, sample) -> dict:
    is_data_issue = snapshot.case_type == "data_check" or bool(
        sample is not None
        and sample.status == "reviewed"
        and sample.system_group != "data_check"
        and sample.correct_group == "data_check"
    )
    change_proposed = (
        snapshot.recommended_price_type is not None
        and snapshot.recommended_price_type != snapshot.current_price_type
    )
    if is_data_issue:
        state = "data_issue"
        label = "Данные проверяет техническая команда"
    elif change_proposed:
        state = "change_proposed"
        label = "Система предлагает изменить тип"
    else:
        state = "no_change"
        label = "Изменение не требуется"
    return {
        "counterparty_ref": profile.counterparty_ref,
        "counterparty_code": profile.counterparty_code,
        "counterparty_name": profile.counterparty_name,
        "current_price_type": snapshot.current_price_type,
        "recommended_price_type": None if is_data_issue else snapshot.recommended_price_type,
        "result_state": state,
        "result_label": label,
        "can_review": bool(sample is not None and sample.status == "pending" and not is_data_issue),
        "quality_sample_id": sample.id if sample is not None else None,
        "quality_sample_status": sample.status if sample is not None else None,
    }


_DATA_ISSUE_TEXTS = {
    "active_contract_missing": "Не найден однозначный действующий договор.",
    "price_type_missing": "В рабочем договоре не указан тип цены.",
    "price_type_marked": "Тип цены рабочего договора помечен на удаление.",
    "unknown_price_type": "Тип цены рабочего договора не распознан.",
    "conflicting_price_levels": "В рабочих договорах указаны разные ценовые уровни.",
    "conflicting_price_type_variants": "В рабочих договорах указаны разные варианты типа цены.",
    "duplicate_counterparty": "Обнаружен дубль карточки клиента.",
    "partial_source": "Один или несколько источников загружены не полностью.",
    "source_mismatch": "Данные источников расходятся.",
    "return_period_mismatch": "Периоды продаж и возвратов не совпадают.",
    "economics_missing": "Не загружены данные для проверки экономики.",
}


def _data_issue_text(snapshot) -> str:
    for reason in snapshot.reasons or ():
        if reason in _DATA_ISSUE_TEXTS:
            return _DATA_ISSUE_TEXTS[reason]
    return "Техническая причина не расшифрована."


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


@router.get("/portfolio", response_model=CustomerPriceTypePortfolioResponse)
def list_customer_price_type_portfolio(
    access: Access,
    batch_key: str = Query(default="reviewed-working-contracts-2026-07", max_length=128),
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    bucket: CustomerPriceTypePortfolioBucket = "all",
    current_price_type: str | None = Query(default=None, max_length=255),
    action_required: bool | None = None,
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CustomerPriceTypePortfolioResponse:
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        batch = repository.get_review_batch(batch_key)
        if batch is None:
            raise HTTPException(status_code=404, detail="customer price-type batch not found")
        requested_month = _month(snapshot_month)
        run = repository.latest_run(requested_month)
        rows, total, counts, review_status_counts, mismatch_count = repository.list_portfolio(
            batch=batch,
            access=access,
            run_id=run.id if run else None,
            bucket=bucket,
            current_price_type=current_price_type,
            action_required=action_required,
            search=search,
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    return CustomerPriceTypePortfolioResponse(
        run_id=run.id if run else None,
        snapshot_month=run.snapshot_month if run else _month(snapshot_month),
        ruleset_version=run.ruleset_version if run else None,
        source_status=run.status if run else "missing",
        batch_key=batch.batch_key,
        batch_label=batch.label,
        expected_counts={str(key): int(value) for key, value in batch.expected_counts.items()},
        counts=counts,
        review_status_counts=review_status_counts,
        mismatch_count=mismatch_count,
        total=total,
        limit=limit,
        offset=offset,
        payload=[
            CustomerPriceTypePortfolioItem.model_validate(_portfolio_payload(*row, access))
            for row in rows
        ],
    )


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
        guidance=customer_price_type_case_guidance(snapshot),
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
        if (
            access.role == "network_head"
            and latest is not None
            and latest.case_type == "data_check"
        ):
            raise HTTPException(status_code=404, detail="customer price-type profile not found")
        open_case_row = (
            repository.get_case_scoped(profile.open_case_id, access)
            if profile.open_case_id
            else None
        )
        case_history = repository.profile_cases(profile.id, access=access)
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
        case_history=[
            CustomerPriceTypeCaseItem.model_validate(_case_payload(*item)) for item in case_history
        ],
        history=[
            CustomerPriceTypeSnapshotResponse.model_validate(_snapshot_payload(item, access))
            for item in history
        ],
    )


@router.get("/profiles", response_model=CustomerPriceTypeProfileSearchResponse)
def search_customer_price_type_profiles(
    access: Access,
    search: str = Query(min_length=2, max_length=255),
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CustomerPriceTypeProfileSearchResponse:
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        run = repository.latest_run(_month(snapshot_month))
        if run is None:
            rows, total = [], 0
        else:
            rows, total = repository.search_profiles(
                run_id=run.id,
                access=access,
                search=search,
                limit=limit,
                offset=offset,
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    return CustomerPriceTypeProfileSearchResponse(
        run_id=run.id if run else None,
        snapshot_month=run.snapshot_month if run else _month(snapshot_month),
        ruleset_version=run.ruleset_version if run else None,
        source_status=run.status if run else "missing",
        total=total,
        limit=limit,
        offset=offset,
        payload=[
            CustomerPriceTypeProfileSearchItem.model_validate(_profile_search_payload(*row))
            for row in rows
        ],
    )


@router.get("/data-issues", response_model=CustomerPriceTypeDataIssueListResponse)
def list_customer_price_type_data_issues(
    access: Access,
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CustomerPriceTypeDataIssueListResponse:
    if access.role not in {"internal", "quality", "executive"}:
        raise HTTPException(status_code=403, detail="customer price-type data issue access denied")
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        run = repository.latest_run(_month(snapshot_month))
        rows = repository.list_data_issues(run_id=run.id, search=search) if run is not None else []
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    total = len(rows)
    page = rows[offset : offset + limit]
    payload = []
    for source, profile, snapshot, source_row in page:
        if source == "calculation":
            payload.append(
                CustomerPriceTypeDataIssueItem(
                    counterparty_ref=profile.counterparty_ref,
                    counterparty_code=profile.counterparty_code,
                    counterparty_name=profile.counterparty_name,
                    current_price_type=snapshot.current_price_type,
                    issue_source="calculation",
                    issue_text=_data_issue_text(snapshot),
                    case_id=source_row.id if source_row is not None else None,
                )
            )
        else:
            payload.append(
                CustomerPriceTypeDataIssueItem(
                    counterparty_ref=profile.counterparty_ref,
                    counterparty_code=profile.counterparty_code,
                    counterparty_name=profile.counterparty_name,
                    current_price_type=snapshot.current_price_type,
                    issue_source="expert",
                    issue_text="Эксперт заметил ошибку в исходных данных.",
                    reported_by=source_row.reviewed_by,
                    reported_at=source_row.reviewed_at,
                    comment=source_row.comment,
                )
            )
    return CustomerPriceTypeDataIssueListResponse(
        run_id=run.id if run else None,
        snapshot_month=run.snapshot_month if run else _month(snapshot_month),
        ruleset_version=run.ruleset_version if run else None,
        source_status=run.status if run else "missing",
        total=total,
        limit=limit,
        offset=offset,
        payload=payload,
    )


@router.get("/runs/{run_id}", response_model=CustomerPriceTypeRunResponse)
def get_customer_price_type_run(
    run_id: int,
    access: Access,
    db: Session = Depends(get_db),
) -> CustomerPriceTypeRunResponse:
    if access.role not in {"internal", "executive", "network_head", "integration_operator"}:
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


@router.post(
    "/quality/samples/prepare",
    response_model=CustomerPriceTypeQualityPrepareResponse,
)
def prepare_customer_price_type_quality_samples(
    payload: CustomerPriceTypeQualityPrepareRequest,
    access: Access,
    db: Session = Depends(get_db),
) -> CustomerPriceTypeQualityPrepareResponse:
    try:
        result = CustomerPriceTypeQualityService(db).prepare(
            snapshot_month=_month(payload.snapshot_month),
            per_group=payload.per_group,
            access=access,
        )
        return CustomerPriceTypeQualityPrepareResponse.model_validate(result)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc


@router.get(
    "/quality/samples",
    response_model=CustomerPriceTypeQualitySampleListResponse,
)
def list_customer_price_type_quality_samples(
    access: Access,
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    status: Literal["pending", "reviewed"] | None = None,
    group: CustomerPriceTypeQualityGroup | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CustomerPriceTypeQualitySampleListResponse:
    _ensure_quality_read_access(access)
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        run = repository.latest_run(_month(snapshot_month))
        if run is None:
            rows, total = [], 0
        else:
            rows, total = repository.list_quality_samples(
                run_id=run.id,
                access=access,
                status=status,
                group=group,
                limit=limit,
                offset=offset,
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    return CustomerPriceTypeQualitySampleListResponse(
        run_id=run.id if run else None,
        snapshot_month=run.snapshot_month if run else _month(snapshot_month),
        ruleset_version=run.ruleset_version if run else None,
        source_status=run.status if run else "missing",
        total=total,
        limit=limit,
        offset=offset,
        payload=[
            CustomerPriceTypeQualitySampleResponse.model_validate(_quality_sample_payload(*row))
            for row in rows
        ],
    )


@router.get(
    "/quality/samples/{sample_id}",
    response_model=CustomerPriceTypeQualitySampleDetailResponse,
)
def get_customer_price_type_quality_sample(
    sample_id: int,
    access: Access,
    db: Session = Depends(get_db),
) -> CustomerPriceTypeQualitySampleDetailResponse:
    _ensure_quality_read_access(access)
    repository = SqlAlchemyCustomerPriceTypeRepository(db)
    try:
        row = repository.get_quality_sample(sample_id, access)
        if row is None:
            raise HTTPException(
                status_code=404, detail="customer price-type quality sample not found"
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
    sample, profile, snapshot = row
    return CustomerPriceTypeQualitySampleDetailResponse(
        **CustomerPriceTypeReadService._run_envelope(repository.get_run(sample.run_id)),
        sample=CustomerPriceTypeQualitySampleResponse.model_validate(
            _quality_sample_payload(sample, profile, snapshot)
        ),
        profile=CustomerPriceTypeQualityProfileResponse(
            id=profile.id,
            counterparty_ref=profile.counterparty_ref,
            counterparty_code=profile.counterparty_code,
            counterparty_name=profile.counterparty_name,
            department_ref=profile.department_ref,
            department_name=profile.department_name,
            owner_ref=profile.owner_ref,
            owner_name=profile.owner_name,
            master_data_flags=profile.master_data_flags,
        ),
        snapshot=CustomerPriceTypeSnapshotResponse.model_validate(
            _snapshot_payload(snapshot, access)
        ),
    )


@router.put(
    "/quality/samples/{sample_id}",
    response_model=CustomerPriceTypeQualitySampleResponse,
)
def review_customer_price_type_quality_sample(
    sample_id: int,
    payload: CustomerPriceTypeQualityReviewRequest,
    access: Access,
    db: Session = Depends(get_db),
) -> CustomerPriceTypeQualitySampleResponse:
    try:
        row = CustomerPriceTypeQualityService(db).review(
            sample_id=sample_id,
            review_result=payload.review_result,
            correct_group=payload.correct_group,
            comment=payload.comment,
            expected_version=payload.expected_version,
            access=access,
        )
        if row is None:
            raise LookupError("quality sample not found")
        return CustomerPriceTypeQualitySampleResponse.model_validate(_quality_sample_payload(*row))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CustomerPriceTypeQualityConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc


@router.get(
    "/quality/metrics",
    response_model=CustomerPriceTypeQualityMetricsResponse,
)
def get_customer_price_type_quality_metrics(
    access: Access,
    snapshot_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
) -> CustomerPriceTypeQualityMetricsResponse:
    try:
        return CustomerPriceTypeQualityMetricsResponse.model_validate(
            CustomerPriceTypeQualityService(db).metrics(
                snapshot_month=_month(snapshot_month), access=access
            )
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="customer price-type storage unavailable"
        ) from exc
