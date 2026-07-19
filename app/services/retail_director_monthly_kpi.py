from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.infrastructure.contracts import ContractIntegrityError, read_json_contract

DEFAULT_REPORTS_DIR = Path("/var/lib/mm-data-contracts/retail-director-monthly")


def _resolve_reports_dir() -> Path:
    explicit_dir = os.getenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR")
    if explicit_dir:
        return Path(explicit_dir)
    return DEFAULT_REPORTS_DIR


def _artifact_path(month: str) -> Path:
    return _resolve_reports_dir() / month / f"retail-director-summary-{month}.json"


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _extract_metric(payload: dict[str, Any], metric_code: str) -> dict[str, Any]:
    for item in _safe_list(payload.get("top_metrics")):
        if isinstance(item, dict) and str(item.get("metric_code") or "") == metric_code:
            return item
    return {}


def _shift_month(month: str, offset: int) -> str:
    value = date.fromisoformat(f"{month}-01")
    month_index = value.year * 12 + value.month - 1 + offset
    return f"{month_index // 12:04d}-{month_index % 12 + 1:02d}"


def _is_finite_decimal(value: Any) -> bool:
    if value in (None, "") or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)).is_finite()
    except InvalidOperation:
        return False


def _is_quantizable_decimal(value: Any, quantum: str) -> bool:
    if not _is_finite_decimal(value):
        return False
    try:
        Decimal(str(value)).quantize(Decimal(quantum))
    except InvalidOperation:
        return False
    return True


def _has_usable_shrinkage_amount(payload: dict[str, Any]) -> bool:
    for key, quantum in (
        ("writeoff_amount", "0.01"),
        ("receipt_amount", "0.01"),
        ("shrinkage_pct", "0.0001"),
    ):
        value = payload.get(key)
        if value not in (None, "") and not _is_quantizable_decimal(value, quantum):
            return False
    shrinkage_amount = payload.get("shrinkage_amount")
    if shrinkage_amount not in (None, ""):
        return _is_quantizable_decimal(shrinkage_amount, "0.01")
    return _is_quantizable_decimal(
        payload.get("writeoff_amount"), "0.01"
    ) and _is_quantizable_decimal(payload.get("receipt_amount"), "0.01")


def load_retail_director_monthly_kpi(month: str) -> dict[str, Any] | None:
    artifact_path = _artifact_path(month)
    if not artifact_path.exists():
        return None

    payload = read_json_contract(artifact_path)
    header = _safe_dict(payload.get("header"))
    shrinkage = _safe_dict(payload.get("shrinkage"))
    compensation = _safe_dict(payload.get("compensation"))
    metadata = _safe_dict(payload.get("metadata"))
    shrinkage_metric = _extract_metric(payload, "shrinkage_rate")
    owner = _safe_dict(shrinkage.get("owner"))
    if not owner:
        subtitle = str(header.get("subtitle") or "")
        owner = {
            "employee_key": metadata.get("employee_key"),
            "employee_bitrix_id": metadata.get("employee_bitrix_id"),
            "employee_name": metadata.get("employee_name") or subtitle.split(" / ", 1)[0],
            "role_code": metadata.get("role_code"),
        }

    resolved_month = str(metadata.get("period_month") or month)
    return {
        "schema_version": int(payload.get("schema_version") or 1),
        "month": resolved_month,
        "title": header.get("title"),
        "subtitle": header.get("subtitle"),
        "overall_signal": header.get("overall_signal"),
        "close_status": header.get("close_status"),
        "writeoff_amount": shrinkage.get("writeoff_amount"),
        "receipt_amount": shrinkage.get("receipt_amount"),
        "shrinkage_amount": shrinkage.get("shrinkage_amount"),
        "shrinkage_pct": shrinkage.get("shrinkage_pct", shrinkage_metric.get("fact_value")),
        "norm_pct": shrinkage.get("norm_pct", shrinkage_metric.get("plan_value")),
        "matched_store_count": shrinkage.get("matched_store_count"),
        "stores": [
            dict(item) for item in _safe_list(shrinkage.get("stores")) if isinstance(item, dict)
        ],
        "top_documents": [
            dict(item)
            for item in _safe_list(shrinkage.get("top_documents"))
            if isinstance(item, dict)
        ],
        "data_quality": _safe_dict(shrinkage.get("data_quality")),
        "owner": owner,
        "kpi_index_sum": compensation.get("kpi_index_sum"),
        "kpi_bonus_amount": compensation.get("kpi_bonus_amount"),
        "to_pay": compensation.get("to_pay"),
        "warnings": [
            str(item) for item in _safe_list(payload.get("warnings")) if str(item).strip()
        ],
        "source_path": str(artifact_path),
    }


def load_retail_director_monthly_kpi_history(
    month: str,
    *,
    limit: int = 3,
    lookback_months: int = 12,
) -> dict[str, Any]:
    if limit <= 0:
        return {
            "previous_month": None,
            "history": [],
            "read_error_count": 0,
            "source_status": "ready",
        }
    previous_month_payload: dict[str, Any] | None = None
    history: list[dict[str, Any]] = []
    read_error_count = 0
    for offset in range(1, max(lookback_months, 0) + 1):
        candidate_month = _shift_month(month, -offset)
        try:
            candidate = load_retail_director_monthly_kpi(candidate_month)
        except (ContractIntegrityError, OSError, TypeError, ValueError):
            read_error_count += 1
            continue
        if candidate is None:
            continue
        if not _has_usable_shrinkage_amount(candidate):
            if any(
                candidate.get(key) not in (None, "")
                for key in ("shrinkage_amount", "writeoff_amount", "receipt_amount")
            ):
                read_error_count += 1
            continue
        if offset == 1:
            previous_month_payload = candidate
        history.append(candidate)
        if len(history) >= max(limit, 0):
            break
    target_limit = max(limit, 0)
    if read_error_count:
        source_status = "partial" if history else "source_error"
    elif len(history) >= target_limit:
        source_status = "ready"
    elif history:
        source_status = "partial"
    else:
        source_status = "source_missing"
    return {
        "previous_month": previous_month_payload,
        "history": history,
        "read_error_count": read_error_count,
        "source_status": source_status,
    }
