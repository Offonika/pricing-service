from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from tasks.send_employee_receivable_report import (
    build_employee_receivable_changes,
    enrich_employee_items_with_counterparty_codes,
    export_employee_receivable_report,
    load_employee_items,
    load_employee_snapshot_history,
    resolve_employee_snapshot_dates,
)
from tasks.send_employee_receivable_report import (
    build_telegram_message as build_employee_telegram_message,
)
from tasks.send_weekly_manager_sales_report import (
    DEFAULT_OUTPUT_DIR as TASK_DEFAULT_OUTPUT_DIR,
)
from tasks.send_weekly_manager_sales_report import (
    WeeklySalesWindow,
    _build_onec_engine,
    _build_output_path,
    _load_sales_records,
    build_attention_manager_sales_items,
    build_weekly_manager_sales_items,
    build_weekly_manager_store_sales_items,
    enrich_sales_records_with_codes,
    export_weekly_manager_sales_report,
    fetch_onec_shortage_cash_orders,
    load_weekly_sales_history,
)
from tasks.send_weekly_manager_sales_report import (
    build_telegram_message as build_weekly_sales_telegram_message,
)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ARTIFACT_TYPE_SALES = "sales"
ARTIFACT_TYPE_EMPLOYEE = "employee"
VALID_ARTIFACT_TYPES = {ARTIFACT_TYPE_SALES, ARTIFACT_TYPE_EMPLOYEE}
DEFAULT_REPORT_DIR = TASK_DEFAULT_OUTPUT_DIR


@dataclass(frozen=True, slots=True)
class WeeklyManagerSalesReportArtifact:
    artifact_type: str
    title: str
    path: Path
    sha256: str
    size_bytes: int
    generated_at: datetime
    message: str


@dataclass(frozen=True, slots=True)
class WeeklyManagerSalesReportBundle:
    report_key: str
    revision: str
    window: WeeklySalesWindow
    employee_snapshot_date: date
    employee_previous_date: date | None
    generated_at: datetime
    manager_count: int
    attention_count: int
    employee_case_count: int
    cash_order_count: int
    artifacts: tuple[WeeklyManagerSalesReportArtifact, ...]


def week_window_from_end(week_end: date) -> WeeklySalesWindow:
    week_start = week_end - timedelta(days=6)
    compare_week_end = week_start - timedelta(days=1)
    compare_week_start = compare_week_end - timedelta(days=6)
    return WeeklySalesWindow(
        week_start=week_start,
        week_end=week_end,
        compare_week_start=compare_week_start,
        compare_week_end=compare_week_end,
    )


def _artifact_url(*, week_end: date, artifact_type: str) -> str:
    return (
        f"/api/management/weekly-manager-sales-report/{artifact_type}"
        f"?week_end={week_end.isoformat()}"
    )


def _legacy_sales_output_path(*, window: WeeklySalesWindow, output_dir: Path) -> Path:
    dated_dir = output_dir / window.week_end.isoformat()
    filename = (
        f"weekly-manager-sales-{window.week_start.isoformat()}-"
        f"to-{window.week_end.isoformat()}.xlsx"
    )
    return dated_dir / filename


def _build_employee_output_path(
    *,
    window: WeeklySalesWindow,
    snapshot_date: date,
    output_dir: Path,
) -> Path:
    dated_dir = output_dir / window.week_end.isoformat()
    return dated_dir / f"Долги сотрудников {snapshot_date.strftime('%d.%m.%Y')}.xlsx"


def _resolve_sales_output_path(*, window: WeeklySalesWindow, output_dir: Path) -> Path:
    preferred = _build_output_path(window=window, output_dir=output_dir)
    if preferred.exists():
        return preferred
    legacy = _legacy_sales_output_path(window=window, output_dir=output_dir)
    if legacy.exists():
        return legacy
    return preferred


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_generated_at(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(tzinfo=None)


def _build_artifact(
    *,
    artifact_type: str,
    title: str,
    path: Path,
    message: str,
) -> WeeklyManagerSalesReportArtifact:
    return WeeklyManagerSalesReportArtifact(
        artifact_type=artifact_type,
        title=title,
        path=path,
        sha256=_sha256_path(path),
        size_bytes=path.stat().st_size,
        generated_at=_path_generated_at(path),
        message=message,
    )


def _artifact_by_type(
    bundle: WeeklyManagerSalesReportBundle, *, artifact_type: str
) -> WeeklyManagerSalesReportArtifact:
    if artifact_type not in VALID_ARTIFACT_TYPES:
        raise KeyError(artifact_type)
    for artifact in bundle.artifacts:
        if artifact.artifact_type == artifact_type:
            return artifact
    raise KeyError(artifact_type)


def build_weekly_manager_sales_report_bundle(
    session: Session,
    *,
    week_end: date,
    output_dir: Path | str | None = None,
) -> WeeklyManagerSalesReportBundle:
    resolved_output_dir = Path(output_dir) if output_dir is not None else DEFAULT_REPORT_DIR
    settings = get_settings()
    onec_engine = _build_onec_engine(settings)
    window = week_window_from_end(week_end)

    current_records = _load_sales_records(
        session,
        date_from=window.week_start,
        date_to=window.week_end,
    )
    previous_records = _load_sales_records(
        session,
        date_from=window.compare_week_start,
        date_to=window.compare_week_end,
    )
    if not current_records and not previous_records:
        raise RuntimeError(
            "No onec sales KPI rows found for weekly manager sales report "
            f"{window.week_start.isoformat()} - {window.week_end.isoformat()}"
        )

    employee_snapshot_date, employee_previous_date = resolve_employee_snapshot_dates(
        session,
        requested_date=None,
        latest_not_after=window.week_end,
    )
    employee_current_items = load_employee_items(session, snapshot_date=employee_snapshot_date)
    employee_previous_items = (
        load_employee_items(session, snapshot_date=employee_previous_date)
        if employee_previous_date
        else []
    )
    weekly_history = load_weekly_sales_history(session, week_end=window.week_end, limit=4)
    employee_snapshot_history = load_employee_snapshot_history(
        session,
        snapshot_date=employee_snapshot_date,
        limit=7,
    )

    cash_order_items = (
        fetch_onec_shortage_cash_orders(
            onec_engine,
            date_from=window.week_start,
            date_to=window.week_end,
        )
        if onec_engine is not None
        else []
    )
    enrich_sales_records_with_codes(current_records, onec_engine=onec_engine)
    enrich_sales_records_with_codes(previous_records, onec_engine=onec_engine)
    enrich_employee_items_with_counterparty_codes(employee_current_items, onec_engine=onec_engine)
    enrich_employee_items_with_counterparty_codes(employee_previous_items, onec_engine=onec_engine)

    manager_items = build_weekly_manager_sales_items(current_records, previous_records)
    manager_store_items = build_weekly_manager_store_sales_items(current_records, previous_records)
    attention_items = build_attention_manager_sales_items(manager_items)
    employee_changes = build_employee_receivable_changes(
        employee_current_items,
        employee_previous_items,
    )

    sales_report_path = _resolve_sales_output_path(window=window, output_dir=resolved_output_dir)
    if not sales_report_path.exists():
        export_weekly_manager_sales_report(
            window=window,
            manager_items=manager_items,
            attention_items=attention_items,
            manager_store_items=manager_store_items,
            cash_order_items=cash_order_items,
            output_path=sales_report_path,
            weekly_history=weekly_history,
        )

    employee_report_path = _build_employee_output_path(
        window=window,
        snapshot_date=employee_snapshot_date,
        output_dir=resolved_output_dir,
    )
    if not employee_report_path.exists():
        export_employee_receivable_report(
            snapshot_date=employee_snapshot_date,
            previous_date=employee_previous_date,
            current_items=employee_current_items,
            changes=employee_changes,
            output_path=employee_report_path,
            snapshot_history=employee_snapshot_history,
        )

    sales_artifact = _build_artifact(
        artifact_type=ARTIFACT_TYPE_SALES,
        title="Личные продажи менеджеров",
        path=sales_report_path,
        message=build_weekly_sales_telegram_message(
            window=window,
            manager_items=manager_items,
            attention_items=attention_items,
            cash_order_items=cash_order_items,
        ),
    )
    employee_artifact = _build_artifact(
        artifact_type=ARTIFACT_TYPE_EMPLOYEE,
        title="Долги сотрудников",
        path=employee_report_path,
        message=build_employee_telegram_message(
            snapshot_date=employee_snapshot_date,
            previous_date=employee_previous_date,
            current_items=employee_current_items,
            changes=employee_changes,
        ),
    )

    revision = sha256(f"{sales_artifact.sha256}:{employee_artifact.sha256}".encode()).hexdigest()[
        :16
    ]
    return WeeklyManagerSalesReportBundle(
        report_key=f"weekly-manager-sales|{window.week_end.isoformat()}",
        revision=revision,
        window=window,
        employee_snapshot_date=employee_snapshot_date,
        employee_previous_date=employee_previous_date,
        generated_at=max(sales_artifact.generated_at, employee_artifact.generated_at),
        manager_count=len(manager_items),
        attention_count=len(attention_items),
        employee_case_count=len(employee_current_items),
        cash_order_count=len(cash_order_items),
        artifacts=(sales_artifact, employee_artifact),
    )


def build_weekly_manager_sales_report_manifest(
    session: Session,
    *,
    week_end: date,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    bundle = build_weekly_manager_sales_report_bundle(
        session,
        week_end=week_end,
        output_dir=output_dir,
    )
    return {
        "report_key": bundle.report_key,
        "revision": bundle.revision,
        "generated_at": bundle.generated_at,
        "period": {
            "week_start": bundle.window.week_start,
            "week_end": bundle.window.week_end,
            "compare_week_start": bundle.window.compare_week_start,
            "compare_week_end": bundle.window.compare_week_end,
            "employee_snapshot_date": bundle.employee_snapshot_date,
            "employee_previous_date": bundle.employee_previous_date,
        },
        "manager_count": bundle.manager_count,
        "attention_count": bundle.attention_count,
        "employee_case_count": bundle.employee_case_count,
        "cash_order_count": bundle.cash_order_count,
        "artifacts": [
            {
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "filename": artifact.path.name,
                "artifact_url": _artifact_url(
                    week_end=bundle.window.week_end,
                    artifact_type=artifact.artifact_type,
                ),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "message": artifact.message,
            }
            for artifact in bundle.artifacts
        ],
    }


def build_weekly_manager_sales_report_health(
    session: Session,
    *,
    week_end: date,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    try:
        bundle = build_weekly_manager_sales_report_bundle(
            session,
            week_end=week_end,
            output_dir=output_dir,
        )
    except Exception as error:
        return {
            "as_of": week_end,
            "freshness_status": "missing",
            "source_status": "error",
            "week_end": week_end,
            "status": "missing",
            "report_key": None,
            "revision": None,
            "artifact_count": 0,
            "manager_count": 0,
            "attention_count": 0,
            "employee_case_count": 0,
            "cash_order_count": 0,
            "generated_at": None,
            "error": str(error),
        }

    return {
        "as_of": week_end,
        "freshness_status": "fresh",
        "source_status": "ready",
        "week_end": week_end,
        "status": "ready",
        "report_key": bundle.report_key,
        "revision": bundle.revision,
        "artifact_count": len(bundle.artifacts),
        "manager_count": bundle.manager_count,
        "attention_count": bundle.attention_count,
        "employee_case_count": bundle.employee_case_count,
        "cash_order_count": bundle.cash_order_count,
        "generated_at": bundle.generated_at,
        "error": None,
    }


def get_weekly_manager_sales_report_artifact(
    session: Session,
    *,
    week_end: date,
    artifact_type: str,
    output_dir: Path | str | None = None,
) -> WeeklyManagerSalesReportArtifact:
    bundle = build_weekly_manager_sales_report_bundle(
        session,
        week_end=week_end,
        output_dir=output_dir,
    )
    return _artifact_by_type(bundle, artifact_type=artifact_type)
