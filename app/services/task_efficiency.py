from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.db import build_application_engine

TASK_EFFICIENCY_METRIC_CODE = "personal_tasks_on_time_share"
TASK_EFFICIENCY_TABLE = "bitrix_fact_employee_task_kpi_monthly"
TASK_RAW_TABLE = "bitrix_raw_task_current"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _parse_month(value: str) -> tuple[date, date]:
    if not _MONTH_RE.match(value):
        raise ValueError("month must be in YYYY-MM format")
    month_start = datetime.strptime(value, "%Y-%m").date()
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return month_start, next_month - timedelta(days=1)


def _validate_identifier(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER_RE.match(normalized):
        raise ValueError(f"{field_name} contains unsupported SQL identifier")
    return normalized


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return datetime.fromisoformat(stripped).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _json_value(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


def _task_is_top_level(row: dict[str, Any]) -> bool:
    parent_id = str(row.get("parent_task_id") or "").strip()
    return parent_id in {"", "0"}


def _bitrix_participants(row: dict[str, Any]) -> list[tuple[str, str | None]]:
    result: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    responsible_id = str(row.get("responsible_id") or "").strip()
    if responsible_id:
        result.append((responsible_id, row.get("responsible_name")))
        seen.add(responsible_id)

    payload = _json_value(row.get("payload_json")) or {}
    accomplices_data = payload.get("accomplicesData") if isinstance(payload, dict) else {}
    accomplices = _json_value(row.get("accomplices_json")) or []
    if isinstance(accomplices, dict):
        accomplices = list(accomplices)
    if not isinstance(accomplices, list):
        return result

    for item in accomplices:
        accomplice_id = ""
        accomplice_name = None
        if isinstance(item, dict):
            accomplice_id = str(item.get("id") or item.get("ID") or "").strip()
            accomplice_name = item.get("name") or item.get("NAME")
        else:
            accomplice_id = str(item or "").strip()
        if not accomplice_id or accomplice_id in seen:
            continue
        if not accomplice_name and isinstance(accomplices_data, dict):
            data_item = accomplices_data.get(accomplice_id) or {}
            if isinstance(data_item, dict):
                accomplice_name = data_item.get("name") or data_item.get("NAME")
        result.append((accomplice_id, accomplice_name))
        seen.add(accomplice_id)
    return result


def _task_is_in_period(row: dict[str, Any], period_start: datetime, period_end: datetime) -> bool:
    created_at = _to_datetime(row.get("created_date"))
    closed_at = _to_datetime(row.get("closed_date"))
    changed_at = _to_datetime(row.get("changed_date")) or _to_datetime(
        row.get("status_changed_date")
    )
    deadline_at = _to_datetime(row.get("deadline_date"))

    start_at = created_at or changed_at or deadline_at
    if start_at is None:
        return False
    if start_at > period_end:
        return False
    if closed_at is None:
        return True
    return closed_at >= period_start


def _task_completed_in_period(
    row: dict[str, Any],
    period_start: datetime,
    period_end: datetime,
) -> bool:
    closed_at = _to_datetime(row.get("closed_date"))
    return closed_at is not None and period_start <= closed_at <= period_end


def _task_has_bitrix_remark(
    row: dict[str, Any],
    period_start: datetime,
    period_end: datetime,
) -> bool:
    deadline_at = _to_datetime(row.get("deadline_date"))
    if deadline_at is None:
        return False
    created_at = _to_datetime(row.get("created_date"))
    if created_at is not None and created_at > deadline_at:
        return False
    closed_at = _to_datetime(row.get("closed_date"))
    overdue_until = closed_at or period_end
    return deadline_at < overdue_until and max(deadline_at, period_start) <= min(
        overdue_until, period_end
    )


def _bitrix_effectiveness_pct(remarks: int, total_in_work: int) -> float | None:
    if total_in_work <= 0:
        return None
    return round(max(0.0, 100.0 - (remarks / total_in_work) * 100.0), 2)


def _build_bitrix_stats(
    rows: list[dict[str, Any]],
    *,
    month_start: date,
    month_end: date,
    include_subtasks: bool,
) -> dict[str, dict[str, Any]]:
    period_start = datetime.combine(month_start, time.min)
    period_end = datetime.combine(month_end, time.max)
    stats: dict[str, dict[str, Any]] = {}

    for row in rows:
        if not include_subtasks and not _task_is_top_level(row):
            continue
        if not _task_is_in_period(row, period_start, period_end):
            continue

        created_by = str(row.get("created_by") or "").strip()
        for employee_id, employee_name in _bitrix_participants(row):
            if created_by and created_by == employee_id:
                continue
            item = stats.setdefault(
                employee_id,
                {
                    "bitrix_total_in_work_count": 0,
                    "bitrix_completed_tasks_count": 0,
                    "bitrix_task_remarks_count": 0,
                    "employee_name": employee_name,
                },
            )
            if employee_name and not item.get("employee_name"):
                item["employee_name"] = employee_name
            item["bitrix_total_in_work_count"] += 1
            if _task_completed_in_period(row, period_start, period_end):
                item["bitrix_completed_tasks_count"] += 1
            if _task_has_bitrix_remark(row, period_start, period_end):
                item["bitrix_task_remarks_count"] += 1

    for item in stats.values():
        item["bitrix_effectiveness_pct"] = _bitrix_effectiveness_pct(
            item["bitrix_task_remarks_count"],
            item["bitrix_total_in_work_count"],
        )
        item["bitrix_effectiveness_source"] = "bitrix_raw_task_current"
    return stats


def _fallback_bitrix_stats(item: dict[str, Any]) -> dict[str, Any]:
    total = _to_int(item.get("total_personal_tasks_with_deadline"))
    closed_on_time = _to_int(item.get("closed_on_time_personal_tasks"))
    late_closed = _to_int(item.get("late_closed_personal_tasks"))
    open_overdue = _to_int(item.get("open_overdue_personal_tasks"))
    remarks = late_closed + open_overdue
    return {
        "bitrix_total_in_work_count": total,
        "bitrix_completed_tasks_count": closed_on_time + late_closed,
        "bitrix_task_remarks_count": remarks,
        "bitrix_effectiveness_pct": _bitrix_effectiveness_pct(remarks, total),
        "bitrix_effectiveness_source": "monthly_fact_deadline_proxy",
    }


def _empty_raw_bitrix_stats(employee_name: str | None) -> dict[str, Any]:
    return {
        "bitrix_total_in_work_count": 0,
        "bitrix_completed_tasks_count": 0,
        "bitrix_task_remarks_count": 0,
        "bitrix_effectiveness_pct": None,
        "bitrix_effectiveness_source": "bitrix_raw_task_current",
        "employee_name": employee_name,
    }


def _row_to_item(
    row: dict[str, Any],
    bitrix_stats: dict[str, Any] | None = None,
    *,
    use_fallback_bitrix_stats: bool = True,
) -> dict[str, Any]:
    item = {
        "month_start": row.get("month_start"),
        "month_end": row.get("month_end"),
        "employee_bitrix_id": (
            None if row.get("employee_bitrix_id") is None else str(row.get("employee_bitrix_id"))
        ),
        "employee_key": row.get("employee_key"),
        "employee_name": row.get("employee_name"),
        "metric_code": row.get("metric_code") or TASK_EFFICIENCY_METRIC_CODE,
        "total_personal_tasks_with_deadline": _to_int(
            row.get("total_personal_tasks_with_deadline")
        ),
        "closed_on_time_personal_tasks": _to_int(row.get("closed_on_time_personal_tasks")),
        "late_closed_personal_tasks": _to_int(row.get("late_closed_personal_tasks")),
        "open_overdue_personal_tasks": _to_int(row.get("open_overdue_personal_tasks")),
        "canceled_personal_tasks": _to_int(row.get("canceled_personal_tasks")),
        "personal_tasks_on_time_share": _to_float(row.get("personal_tasks_on_time_share")),
        "include_subtasks": bool(row.get("include_subtasks") or False),
        "min_task_count": _to_int(row.get("min_task_count") or 1),
        "is_metric_applicable": (
            bool(row.get("is_metric_applicable"))
            if row.get("is_metric_applicable") is not None
            else True
        ),
        "exclusion_reason": row.get("exclusion_reason"),
        "source_scope": row.get("source_scope"),
        "calculation_note": row.get("calculation_note"),
        "calculated_at": row.get("calculated_at"),
    }
    if bitrix_stats:
        item.update(
            {
                "employee_name": item.get("employee_name") or bitrix_stats.get("employee_name"),
                "bitrix_total_in_work_count": _to_int(
                    bitrix_stats.get("bitrix_total_in_work_count")
                ),
                "bitrix_completed_tasks_count": _to_int(
                    bitrix_stats.get("bitrix_completed_tasks_count")
                ),
                "bitrix_task_remarks_count": _to_int(bitrix_stats.get("bitrix_task_remarks_count")),
                "bitrix_effectiveness_pct": _to_float(bitrix_stats.get("bitrix_effectiveness_pct")),
                "bitrix_effectiveness_source": bitrix_stats.get("bitrix_effectiveness_source"),
            }
        )
    elif use_fallback_bitrix_stats:
        item.update(_fallback_bitrix_stats(item))
    return {
        **item,
    }


def _empty_report(
    *,
    month: str,
    month_start: date,
    month_end: date,
    source_status: str,
    note: str,
    low_threshold_pct: float,
) -> dict[str, Any]:
    return {
        "as_of": month,
        "month": month,
        "month_start": month_start,
        "month_end": month_end,
        "freshness_status": "missing",
        "source_status": source_status,
        "note": note,
        "summary": {
            "employee_count": 0,
            "applicable_count": 0,
            "total_personal_tasks_with_deadline": 0,
            "closed_on_time_personal_tasks": 0,
            "late_closed_personal_tasks": 0,
            "open_overdue_personal_tasks": 0,
            "canceled_personal_tasks": 0,
            "average_on_time_share": None,
            "bitrix_average_effectiveness_pct": None,
            "bitrix_total_in_work_count": 0,
            "bitrix_completed_tasks_count": 0,
            "bitrix_task_remarks_count": 0,
            "low_efficiency_threshold": low_threshold_pct,
            "low_efficiency_count": 0,
        },
        "payload": [],
    }


def load_task_efficiency_report(
    *,
    month: str,
    database_url: str | None,
    schema: str = "reconciliation",
    source_scope: str | None = "personal_tasks_on_time_share_v1",
    low_threshold_pct: float = 80.0,
) -> dict[str, Any]:
    """Load monthly employee task-efficiency KPI from the mm-compensation read model."""
    month_start, month_end = _parse_month(month)
    if not database_url:
        return _empty_report(
            month=month,
            month_start=month_start,
            month_end=month_end,
            source_status="not_configured",
            note=(
                "database URL for task efficiency is not configured; set "
                "MANAGEMENT_TASK_EFFICIENCY_DATABASE_URL or TELEPHONY_MDM_DATABASE_URL"
            ),
            low_threshold_pct=low_threshold_pct,
        )

    schema_name = _validate_identifier(schema, field_name="schema")
    scope_clause = ""
    params: dict[str, Any] = {
        "month_start": month_start,
        "metric_code": TASK_EFFICIENCY_METRIC_CODE,
    }
    if source_scope:
        scope_clause = "AND source_scope = :source_scope"
        params["source_scope"] = source_scope

    sql = text(f"""
        SELECT
            month_start,
            month_end,
            employee_bitrix_id,
            employee_key,
            employee_name,
            metric_code,
            total_personal_tasks_with_deadline,
            closed_on_time_personal_tasks,
            late_closed_personal_tasks,
            open_overdue_personal_tasks,
            canceled_personal_tasks,
            personal_tasks_on_time_share,
            include_subtasks,
            min_task_count,
            is_metric_applicable,
            exclusion_reason,
            source_scope,
            calculation_note,
            calculated_at
        FROM {schema_name}.{TASK_EFFICIENCY_TABLE}
        WHERE month_start = :month_start
          AND metric_code = :metric_code
          {scope_clause}
        ORDER BY
            personal_tasks_on_time_share IS NULL,
            personal_tasks_on_time_share ASC,
            open_overdue_personal_tasks DESC,
            total_personal_tasks_with_deadline DESC,
            employee_name ASC
        """)

    engine = build_application_engine(database_url)
    raw_rows: list[dict[str, Any]] = []
    try:
        with engine.connect() as connection:
            rows = [dict(row._mapping) for row in connection.execute(sql, params)]
            try:
                raw_rows = [dict(row._mapping) for row in connection.execute(text(f"""
                            SELECT
                                task_id,
                                parent_task_id,
                                status_code,
                                responsible_id,
                                responsible_name,
                                created_by,
                                created_date,
                                changed_date,
                                status_changed_date,
                                deadline_date,
                                closed_date,
                                accomplices_json,
                                payload_json
                            FROM {schema_name}.{TASK_RAW_TABLE}
                            """))]
            except SQLAlchemyError:
                raw_rows = []
    finally:
        engine.dispose()

    raw_source_available = bool(raw_rows)
    raw_stats = _build_bitrix_stats(
        raw_rows,
        month_start=month_start,
        month_end=month_end,
        include_subtasks=bool(rows[0].get("include_subtasks")) if rows else False,
    )
    items = []
    for row in rows:
        employee_id = str(row.get("employee_bitrix_id") or "").strip()
        bitrix_stats = raw_stats.get(employee_id)
        if raw_source_available and bitrix_stats is None:
            bitrix_stats = _empty_raw_bitrix_stats(row.get("employee_name"))
        items.append(
            _row_to_item(
                row,
                bitrix_stats,
                use_fallback_bitrix_stats=not raw_source_available,
            )
        )
    existing_employee_ids = {str(item.get("employee_bitrix_id") or "").strip() for item in items}
    for employee_id, stats in raw_stats.items():
        if employee_id in existing_employee_ids:
            continue
        items.append(
            _row_to_item(
                {
                    "month_start": month_start,
                    "month_end": month_end,
                    "employee_bitrix_id": employee_id,
                    "employee_name": stats.get("employee_name") or employee_id,
                    "metric_code": TASK_EFFICIENCY_METRIC_CODE,
                    "total_personal_tasks_with_deadline": 0,
                    "closed_on_time_personal_tasks": 0,
                    "late_closed_personal_tasks": 0,
                    "open_overdue_personal_tasks": 0,
                    "canceled_personal_tasks": 0,
                    "personal_tasks_on_time_share": None,
                    "include_subtasks": False,
                    "min_task_count": 1,
                    "is_metric_applicable": True,
                    "exclusion_reason": None,
                    "source_scope": source_scope,
                    "calculation_note": "Bitrix-like stats from raw task current.",
                },
                stats,
            )
        )
    items.sort(
        key=lambda item: (
            item.get("bitrix_effectiveness_pct") is None,
            float(item.get("bitrix_effectiveness_pct") or 0),
            -int(item.get("bitrix_task_remarks_count") or 0),
            str(item.get("employee_name") or ""),
        )
    )
    if not items:
        return _empty_report(
            month=month,
            month_start=month_start,
            month_end=month_end,
            source_status="empty",
            note=f"task efficiency fact for {month} is empty",
            low_threshold_pct=low_threshold_pct,
        )

    applicable_items = [
        item
        for item in items
        if item["is_metric_applicable"] and item["personal_tasks_on_time_share"] is not None
    ]
    average_on_time_share = None
    if applicable_items:
        average_on_time_share = round(
            sum(float(item["personal_tasks_on_time_share"]) for item in applicable_items)
            / len(applicable_items),
            2,
        )
    low_items = [
        item
        for item in applicable_items
        if float(item["personal_tasks_on_time_share"]) < low_threshold_pct
    ]
    bitrix_applicable_items = [
        item for item in items if item.get("bitrix_effectiveness_pct") is not None
    ]
    bitrix_average_effectiveness_pct = None
    if bitrix_applicable_items:
        bitrix_average_effectiveness_pct = round(
            sum(float(item["bitrix_effectiveness_pct"]) for item in bitrix_applicable_items)
            / len(bitrix_applicable_items),
            2,
        )
    bitrix_low_items = [
        item
        for item in bitrix_applicable_items
        if float(item["bitrix_effectiveness_pct"]) < low_threshold_pct
    ]

    return {
        "as_of": month,
        "month": month,
        "month_start": month_start,
        "month_end": month_end,
        "freshness_status": "fresh",
        "source_status": "ready",
        "note": None,
        "summary": {
            "employee_count": len(items),
            "applicable_count": (
                len(bitrix_applicable_items) if bitrix_applicable_items else len(applicable_items)
            ),
            "total_personal_tasks_with_deadline": sum(
                item["total_personal_tasks_with_deadline"] for item in applicable_items
            ),
            "closed_on_time_personal_tasks": sum(
                item["closed_on_time_personal_tasks"] for item in applicable_items
            ),
            "late_closed_personal_tasks": sum(
                item["late_closed_personal_tasks"] for item in applicable_items
            ),
            "open_overdue_personal_tasks": sum(
                item["open_overdue_personal_tasks"] for item in applicable_items
            ),
            "canceled_personal_tasks": sum(
                item["canceled_personal_tasks"] for item in applicable_items
            ),
            "average_on_time_share": average_on_time_share,
            "bitrix_average_effectiveness_pct": bitrix_average_effectiveness_pct,
            "bitrix_total_in_work_count": sum(
                item["bitrix_total_in_work_count"] for item in bitrix_applicable_items
            ),
            "bitrix_completed_tasks_count": sum(
                item["bitrix_completed_tasks_count"] for item in bitrix_applicable_items
            ),
            "bitrix_task_remarks_count": sum(
                item["bitrix_task_remarks_count"] for item in bitrix_applicable_items
            ),
            "low_efficiency_threshold": low_threshold_pct,
            "low_efficiency_count": (
                len(bitrix_low_items) if bitrix_applicable_items else len(low_items)
            ),
        },
        "payload": items,
    }
