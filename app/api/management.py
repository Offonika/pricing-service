from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_management_internal_token
from app.core.config import get_settings
from app.schemas.executive_dashboard import (
    ExecutiveCashflowPeriodResponse,
    ExecutiveDashboardActionsResponse,
    ExecutiveDashboardResponse,
    ExecutiveProfitLossPeriodResponse,
)
from app.schemas.management import (
    CounterpartyFolderChangeItem,
    CounterpartyFolderChangeResponse,
    CounterpartyFolderRecommendationItem,
    CounterpartyFolderRecommendationResponse,
    CounterpartyFolderSnapshotSyncResponse,
    ManagementComponentHealth,
    ManagementHealthResponse,
    ManagementTaskPayload,
    ManagementTaskPayloadListResponse,
    RetailCustomerPriceTypeRecommendation,
    RetailCustomerPriceTypeRecommendationResponse,
    RetailDirectorMonthlyKpiPayload,
    RetailDirectorMonthlyKpiResponse,
    TaskEfficiencyEmployeeItem,
    TaskEfficiencyResponse,
    WeeklyKpiReportDetail,
    WeeklyKpiReportDetailResponse,
    WeeklyKpiReportHealthResponse,
    WeeklyKpiReportListResponse,
    WeeklyKpiReportManifest,
    WeeklyManagerSalesReportHealthResponse,
    WeeklyManagerSalesReportManifest,
    WeeklyManagerSalesReportResponse,
)
from app.schemas.telephony import (
    TelephonyHealthResponse,
    TelephonyRetailLineItem,
    TelephonyRetailLineMapResponse,
    TelephonyUserLineItem,
    TelephonyUserLineMapResponse,
)
from app.services.bitrix_executive_dashboard_auth import (
    ExecutiveDashboardAuthContext,
    require_executive_dashboard_access,
)
from app.services.counterparty_folder_recommendations import (
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_OVERDUE,
    STATUS_OK,
    build_counterparty_folder_recommendations,
)
from app.services.counterparty_folder_snapshots import (
    build_counterparty_folder_changes,
    sync_counterparty_folder_snapshot,
)
from app.services.exchange_counterparty_settlements import (
    DEFAULT_EXCHANGE_COUNTERPARTY_CODE,
    build_exchange_counterparty_settlements,
)
from app.services.executive_dashboard import (
    build_executive_actions_response,
    build_executive_cashflow_period_response,
    build_executive_dashboard,
    build_executive_profit_loss_period_response,
)
from app.services.finance_cash_position import build_finance_cash_position
from app.services.management_observability import build_management_health
from app.services.management_rules import build_management_task_payloads
from app.services.receivables import (
    fetch_contract_price_type_mapping_from_onec,
    fetch_counterparty_code_mapping_from_onec_group,
    fetch_counterparty_purchase_amounts_from_onec_sales_returns,
)
from app.services.retail_customer_price_types import (
    BUYERS_COUNTERPARTY_GROUP_NAME,
    build_retail_customer_price_type_recommendations,
)
from app.services.retail_director_monthly_kpi import load_retail_director_monthly_kpi
from app.services.task_efficiency import load_task_efficiency_report
from app.services.telephony import (
    build_retail_line_map_projection,
    build_telephony_health,
    load_telephony_user_line_snapshot,
)
from app.services.weekly_kpi_reports import (
    build_weekly_kpi_report_health,
    build_weekly_kpi_report_manifest,
    get_ready_weekly_kpi_report,
    list_ready_weekly_kpi_reports,
)
from app.services.weekly_manager_sales_reports import (
    XLSX_MEDIA_TYPE,
    build_weekly_manager_sales_report_health,
    build_weekly_manager_sales_report_manifest,
    get_weekly_manager_sales_report_artifact,
)

router = APIRouter()


@router.get("/executive-dashboard", response_model=ExecutiveDashboardResponse)
def get_executive_dashboard(
    date_value: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    access: ExecutiveDashboardAuthContext = Depends(require_executive_dashboard_access),
) -> ExecutiveDashboardResponse:
    requested_date = date_value or date.today()
    return build_executive_dashboard(
        db,
        requested_date=requested_date,
        access_context=access,
    )


@router.get(
    "/executive-dashboard/actions",
    response_model=ExecutiveDashboardActionsResponse,
)
def list_executive_dashboard_actions(
    date_value: date | None = Query(default=None, alias="date"),
    status: str | None = Query(default="open"),
    domain: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    access: ExecutiveDashboardAuthContext = Depends(require_executive_dashboard_access),
) -> ExecutiveDashboardActionsResponse:
    requested_date = date_value or date.today()
    if domain and not access.allows_action_domain(domain):
        raise HTTPException(status_code=403, detail="Нет доступа к домену управленческой витрины")
    return build_executive_actions_response(
        db,
        requested_date=requested_date,
        status=status,
        domain=domain,
        access_context=access,
        limit=limit,
    )


@router.get(
    "/executive-dashboard/cashflow-period",
    response_model=ExecutiveCashflowPeriodResponse,
)
def get_executive_dashboard_cashflow_period(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    dds_group: list[str] | None = Query(default=None),
    cash_account_ref: list[str] | None = Query(default=None),
    currency: list[str] | None = Query(default=None),
    direction: list[str] | None = Query(default=None),
    include_internal: bool = Query(default=True),
    access: ExecutiveDashboardAuthContext = Depends(require_executive_dashboard_access),
) -> ExecutiveCashflowPeriodResponse:
    requested_to = date_to or date.today()
    requested_from = date_from or requested_to.replace(day=1)
    if requested_from > requested_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    if not access.allows_block("money_today") or not access.can_view_money_block("money_today"):
        raise HTTPException(status_code=403, detail="Нет доступа к денежному контуру")
    return build_executive_cashflow_period_response(
        date_from=requested_from,
        date_to=requested_to,
        dds_group=dds_group,
        cash_account_ref=cash_account_ref,
        currency=currency,
        direction=direction,
        include_internal=include_internal,
    )


@router.get(
    "/executive-dashboard/profit-loss-period",
    response_model=ExecutiveProfitLossPeriodResponse,
)
def get_executive_dashboard_profit_loss_period(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    access: ExecutiveDashboardAuthContext = Depends(require_executive_dashboard_access),
) -> ExecutiveProfitLossPeriodResponse:
    requested_to = date_to or date.today()
    requested_from = date_from or requested_to.replace(day=1)
    if requested_from > requested_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    if not access.allows_block("profit_loss") or not access.can_view_money_block("profit_loss"):
        raise HTTPException(status_code=403, detail="Нет доступа к отчету о прибылях и убытках")
    return build_executive_profit_loss_period_response(
        db,
        date_from=requested_from,
        date_to=requested_to,
    )


def _filter_real_rb_counterparty_codes(mapping: dict[str, str]) -> dict[str, str]:
    return {
        counterparty_ref: counterparty_code
        for counterparty_ref, counterparty_code in mapping.items()
        if str(counterparty_code or "").strip().startswith("РБ")
    }


def _add_months(value: date, months: int) -> date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _build_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        raise HTTPException(
            status_code=503,
            detail="1C source is unavailable",
        )
    return create_engine(
        settings.onec_database_url,
        connect_args={
            "timeout": float(settings.onec_query_timeout_seconds),
            "login_timeout": float(settings.onec_login_timeout_seconds),
        },
        pool_pre_ping=True,
    )


@router.get("/retail-director-monthly-kpi", response_model=RetailDirectorMonthlyKpiResponse)
def get_retail_director_monthly_kpi(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    _: str = Depends(require_management_internal_token),
):
    payload = load_retail_director_monthly_kpi(month)
    return RetailDirectorMonthlyKpiResponse(
        as_of=month,
        month=month,
        freshness_status="fresh" if payload else "missing",
        source_status="ready" if payload else "empty",
        payload=(
            RetailDirectorMonthlyKpiPayload.model_validate(payload) if payload is not None else None
        ),
    )


@router.get(
    "/retail-customer-price-type-recommendations",
    response_model=RetailCustomerPriceTypeRecommendationResponse,
)
def get_retail_customer_price_type_recommendations(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    actionable_only: bool = Query(default=True),
    buyers_group_only: bool = Query(default=True),
    buyer_group_name: str = Query(default=BUYERS_COUNTERPARTY_GROUP_NAME, max_length=120),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    counterparty_codes_by_ref: dict[str, str] | None = None
    allowed_counterparty_refs: set[str] | None = None
    previous_purchase_amounts_by_ref = None
    onec_engine = _build_onec_engine() if buyers_group_only else None
    try:
        if buyers_group_only and onec_engine is not None:
            counterparty_codes_by_ref = fetch_counterparty_code_mapping_from_onec_group(
                onec_engine,
                group_name=buyer_group_name,
            )
            counterparty_codes_by_ref = _filter_real_rb_counterparty_codes(
                counterparty_codes_by_ref
            )
            allowed_counterparty_refs = set(counterparty_codes_by_ref)
            month_start = date.fromisoformat(f"{month}-01")
            previous_month_start = _add_months(month_start, -1)
            previous_purchase_amounts_by_ref = (
                fetch_counterparty_purchase_amounts_from_onec_sales_returns(
                    onec_engine,
                    period_start=datetime.combine(previous_month_start, time.min),
                    period_end=datetime.combine(month_start, time.min),
                )
            )

        report = build_retail_customer_price_type_recommendations(
            db,
            month=month,
            actionable_only=actionable_only,
            limit=limit,
            allowed_counterparty_refs=allowed_counterparty_refs,
            counterparty_codes_by_ref=counterparty_codes_by_ref,
            previous_purchase_amounts_by_ref=previous_purchase_amounts_by_ref,
            contract_price_type_loader=(
                (
                    lambda contract_refs: fetch_contract_price_type_mapping_from_onec(
                        onec_engine,
                        contract_refs=tuple(contract_refs),
                    )
                )
                if onec_engine is not None
                else None
            ),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="1C source is unavailable") from error
    finally:
        if onec_engine is not None:
            onec_engine.dispose()

    return RetailCustomerPriceTypeRecommendationResponse(
        as_of=report["month"],
        month=report["month"],
        previous_month=report["previous_month"],
        month_start=report["month_start"],
        month_end=report["month_end"],
        freshness_status=report["freshness_status"],
        source_status=report["source_status"],
        summary=report["summary"],
        payload=[
            RetailCustomerPriceTypeRecommendation.model_validate(item) for item in report["payload"]
        ],
    )


@router.get("/task-efficiency", response_model=TaskEfficiencyResponse)
def get_task_efficiency(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    _: str = Depends(require_management_internal_token),
):
    settings = get_settings()
    database_url = (
        settings.management_task_efficiency_database_url or settings.telephony_mdm_database_url
    )
    try:
        report = load_task_efficiency_report(
            month=month,
            database_url=database_url,
            schema=settings.management_task_efficiency_schema,
            source_scope=settings.management_task_efficiency_source_scope,
            low_threshold_pct=settings.management_task_efficiency_low_threshold_pct,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503,
            detail="task efficiency source is unavailable",
        ) from error

    return TaskEfficiencyResponse(
        as_of=report["as_of"],
        month=report["month"],
        month_start=report["month_start"],
        month_end=report["month_end"],
        freshness_status=report["freshness_status"],
        source_status=report["source_status"],
        note=report["note"],
        summary=report["summary"],
        payload=[TaskEfficiencyEmployeeItem.model_validate(item) for item in report["payload"]],
    )


@router.get("/health", response_model=ManagementHealthResponse)
def get_management_health(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = build_management_health(db, as_of=date_value)
    return ManagementHealthResponse(
        as_of=payload["as_of"],
        status=payload["status"],
        freshness_status=payload["freshness_status"],
        source_status=payload["source_status"],
        components=[
            ManagementComponentHealth.model_validate(item) for item in payload["components"]
        ],
    )


@router.get(
    "/counterparty-folder-recommendations",
    response_model=CounterpartyFolderRecommendationResponse,
)
def get_counterparty_folder_recommendations(
    date_value: date = Query(alias="date"),
    status: str | None = Query(
        default=None,
        pattern=(
            f"^({STATUS_MOVE_RECOMMENDED}|{STATUS_OK}|"
            f"{STATUS_NO_OVERDUE}|{STATUS_NEEDS_REVIEW})$"
        ),
    ),
    limit: int | None = Query(default=None, ge=1, le=10000),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    onec_engine = _build_onec_engine()
    try:
        report = build_counterparty_folder_recommendations(
            db,
            onec_engine=onec_engine,
            snapshot_date=date_value,
            limit=limit,
            status=status,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="1C source is unavailable") from error
    finally:
        onec_engine.dispose()

    payload = [
        CounterpartyFolderRecommendationItem.model_validate(item) for item in report["payload"]
    ]
    source_snapshot_count = int(report["summary"].get("source_snapshot_count") or 0)
    return CounterpartyFolderRecommendationResponse(
        as_of=report["snapshot_date"],
        freshness_status="fresh" if source_snapshot_count else "missing",
        source_status="ready" if source_snapshot_count else "empty",
        report_revision=report["report_revision"],
        summary=report["summary"],
        payload=payload,
    )


@router.post(
    "/counterparty-folder-snapshots/sync",
    response_model=CounterpartyFolderSnapshotSyncResponse,
)
def sync_counterparty_folder_snapshots(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    onec_engine = _build_onec_engine()
    try:
        result = sync_counterparty_folder_snapshot(
            db,
            onec_engine=onec_engine,
            snapshot_date=date_value,
        )
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="1C source is unavailable") from error
    finally:
        onec_engine.dispose()

    summary = {
        "fetched_count": result.fetched_count,
        "inserted_count": result.inserted_count,
        "updated_count": result.updated_count,
        "deleted_count": result.deleted_count,
    }
    return CounterpartyFolderSnapshotSyncResponse(
        as_of=result.snapshot_date,
        freshness_status="fresh" if result.fetched_count else "missing",
        source_status="ready" if result.fetched_count else "empty",
        summary=summary,
    )


@router.get(
    "/counterparty-folder-changes",
    response_model=CounterpartyFolderChangeResponse,
)
def get_counterparty_folder_changes(
    date_value: date = Query(alias="date"),
    previous_date: date | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=10000),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    onec_engine = None
    recommendations_report = None
    debt_enrichment_status = "ready"
    try:
        onec_engine = _build_onec_engine()
        recommendations_report = build_counterparty_folder_recommendations(
            db,
            onec_engine=onec_engine,
            snapshot_date=date_value,
        )
    except (HTTPException, SQLAlchemyError):
        debt_enrichment_status = "unavailable"
    finally:
        if onec_engine is not None:
            onec_engine.dispose()

    report = build_counterparty_folder_changes(
        db,
        snapshot_date=date_value,
        previous_snapshot_date=previous_date,
        recommendations_report=recommendations_report,
        limit=limit,
    )
    report["summary"]["debt_enrichment_status"] = debt_enrichment_status
    payload = [CounterpartyFolderChangeItem.model_validate(item) for item in report["payload"]]
    current_snapshot_count = int(report["summary"].get("current_snapshot_count") or 0)
    previous_snapshot_count = int(report["summary"].get("previous_snapshot_count") or 0)
    source_status = "ready" if current_snapshot_count and previous_snapshot_count else "empty"
    return CounterpartyFolderChangeResponse(
        as_of=report["snapshot_date"],
        previous_as_of=report["previous_snapshot_date"],
        freshness_status="fresh" if current_snapshot_count else "missing",
        source_status=source_status,
        report_revision=report["report_revision"],
        summary=report["summary"],
        payload=payload,
    )


@router.get("/exchange-counterparty-settlements")
def get_exchange_counterparty_settlements(
    counterparty_code: str = Query(default=DEFAULT_EXCHANGE_COUNTERPARTY_CODE, max_length=32),
    period_start: date | None = Query(default=None),
    as_of: datetime | None = Query(default=None),
    _: str = Depends(require_management_internal_token),
) -> dict:
    onec_engine = _build_onec_engine()
    try:
        return build_exchange_counterparty_settlements(
            onec_engine,
            counterparty_code=counterparty_code,
            period_start=period_start,
            as_of=as_of,
        )
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="1C source is unavailable") from error
    finally:
        onec_engine.dispose()


@router.get("/cash-position")
def get_cash_position(
    period_start: date | None = Query(default=None),
    as_of: datetime | None = Query(default=None),
    include_zero: bool = Query(default=False),
    top: int = Query(default=15, ge=0, le=100),
    _: str = Depends(require_management_internal_token),
) -> dict:
    onec_engine = _build_onec_engine()
    try:
        return build_finance_cash_position(
            onec_engine,
            period_start=period_start,
            as_of=as_of,
            include_zero=include_zero,
            top_limit=top,
        )
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="1C source is unavailable") from error
    finally:
        onec_engine.dispose()


@router.get("/task-payloads", response_model=ManagementTaskPayloadListResponse)
def list_management_task_payloads(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = [
        ManagementTaskPayload.model_validate(item)
        for item in build_management_task_payloads(db, as_of=date_value)
    ]
    return ManagementTaskPayloadListResponse(
        as_of=date_value,
        freshness_status="fresh" if payload else "missing",
        source_status="ready" if payload else "empty",
        payload=payload,
    )


@router.get("/weekly-kpi-reports/health", response_model=WeeklyKpiReportHealthResponse)
def get_weekly_kpi_reports_health(
    week_end: date = Query(alias="week_end"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = build_weekly_kpi_report_health(db, week_end=week_end)
    return WeeklyKpiReportHealthResponse.model_validate(payload)


@router.get(
    "/weekly-manager-sales-report/health",
    response_model=WeeklyManagerSalesReportHealthResponse,
)
def get_weekly_manager_sales_report_health(
    week_end: date = Query(alias="week_end"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = build_weekly_manager_sales_report_health(db, week_end=week_end)
    return WeeklyManagerSalesReportHealthResponse.model_validate(payload)


@router.get(
    "/weekly-manager-sales-report",
    response_model=WeeklyManagerSalesReportResponse,
)
def get_weekly_manager_sales_report(
    week_end: date = Query(alias="week_end"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = WeeklyManagerSalesReportManifest.model_validate(
        build_weekly_manager_sales_report_manifest(db, week_end=week_end)
    )
    return WeeklyManagerSalesReportResponse(
        as_of=week_end,
        week_end=week_end,
        freshness_status="fresh",
        source_status="ready",
        payload=payload,
    )


@router.get("/weekly-manager-sales-report/{artifact_type}")
def download_weekly_manager_sales_report_artifact(
    artifact_type: str,
    week_end: date = Query(alias="week_end"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    try:
        artifact = get_weekly_manager_sales_report_artifact(
            db,
            week_end=week_end,
            artifact_type=artifact_type,
        )
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="weekly manager sales artifact not found",
        ) from error

    if not artifact.path.exists():
        raise HTTPException(status_code=404, detail="weekly manager sales artifact not found")

    return FileResponse(
        artifact.path,
        media_type=XLSX_MEDIA_TYPE,
        filename=artifact.path.name,
    )


@router.get("/weekly-kpi-reports", response_model=WeeklyKpiReportListResponse)
def list_weekly_kpi_reports(
    week_end: date = Query(alias="week_end"),
    employee_key: str | None = Query(default=None),
    bitrix_user_id: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    items = [
        WeeklyKpiReportManifest.model_validate(
            build_weekly_kpi_report_manifest(item, include_metrics=False)
        )
        for item in list_ready_weekly_kpi_reports(
            db,
            week_end=week_end,
            employee_key=employee_key,
            bitrix_user_id=bitrix_user_id,
            limit=limit,
        )
    ]
    return WeeklyKpiReportListResponse(
        as_of=week_end,
        week_end=week_end,
        freshness_status="fresh" if items else "missing",
        source_status="ready" if items else "empty",
        payload=items,
    )


@router.get("/weekly-kpi-reports/{report_id}", response_model=WeeklyKpiReportDetailResponse)
def get_weekly_kpi_report_detail(
    report_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    item = get_ready_weekly_kpi_report(db, report_id=report_id)
    if item is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="weekly kpi report not found")
    payload = WeeklyKpiReportDetail.model_validate(
        build_weekly_kpi_report_manifest(item, include_metrics=True)
    )
    return WeeklyKpiReportDetailResponse(
        as_of=item.week_end,
        week_end=item.week_end,
        freshness_status="fresh",
        source_status="ready",
        payload=payload,
    )


@router.get("/weekly-kpi-reports/{report_id}/artifact")
def download_weekly_kpi_report_artifact(
    report_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    item = get_ready_weekly_kpi_report(db, report_id=report_id)
    if item is None or not item.artifact_path:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="weekly kpi artifact not found")
    artifact = Path(item.artifact_path)
    if not artifact.exists():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="weekly kpi artifact not found")
    return FileResponse(
        artifact,
        media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        filename=artifact.name,
    )


@router.get("/telephony/health", response_model=TelephonyHealthResponse)
def get_telephony_health(
    date_value: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    requested_date = date_value or date.today()
    settings = get_settings()
    payload = build_telephony_health(
        db,
        requested_date=requested_date,
        max_lag_days=settings.management_telephony_max_lag_days,
        service_line_labels=settings.telephony_service_line_labels,
        exclude_line_ids=settings.telephony_review_line_ids,
    )
    return TelephonyHealthResponse.model_validate(payload)


@router.get("/telephony/employee-line-map", response_model=TelephonyUserLineMapResponse)
def list_telephony_employee_line_map(
    snapshot_date: date | None = Query(default=None),
    active_only: bool = Query(default=False),
    with_extension_only: bool = Query(default=False),
    with_bitrix_only: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    effective_snapshot_date, items = load_telephony_user_line_snapshot(
        db,
        snapshot_date=snapshot_date,
        active_only=active_only,
        with_extension_only=with_extension_only,
        with_bitrix_only=with_bitrix_only,
        limit=limit,
    )
    payload = [TelephonyUserLineItem.model_validate(item, from_attributes=True) for item in items]
    source_status = "ready" if payload else "empty"
    freshness_status = "fresh" if effective_snapshot_date is not None else "missing"
    return TelephonyUserLineMapResponse(
        as_of=effective_snapshot_date,
        snapshot_date=effective_snapshot_date,
        freshness_status=freshness_status,
        source_status=source_status,
        payload=payload,
    )


@router.get("/telephony/retail-line-map", response_model=TelephonyRetailLineMapResponse)
def list_telephony_retail_line_map(
    snapshot_date: date | None = Query(default=None),
    active_only: bool = Query(default=True),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    effective_snapshot_date, items = load_telephony_user_line_snapshot(
        db,
        snapshot_date=snapshot_date,
        active_only=active_only,
        with_extension_only=True,
        limit=limit,
    )
    settings = get_settings()
    projection = build_retail_line_map_projection(
        items,
        service_line_labels=settings.telephony_service_line_labels,
        exclude_line_ids=settings.telephony_review_line_ids,
    )
    payload = [
        TelephonyRetailLineItem.model_validate(item, from_attributes=True) for item in projection
    ]
    source_status = "ready" if payload else "empty"
    freshness_status = "fresh" if effective_snapshot_date is not None else "missing"
    return TelephonyRetailLineMapResponse(
        as_of=effective_snapshot_date,
        snapshot_date=effective_snapshot_date,
        freshness_status=freshness_status,
        source_status=source_status,
        payload=payload,
    )
