from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_MM_COMPENSATION_ROOT = Path(__file__).resolve().parents[3] / "mm-compensation"
DEFAULT_REPORTS_DIR = DEFAULT_MM_COMPENSATION_ROOT / "build" / "retail_director_monthly"


def _resolve_reports_dir() -> Path:
    explicit_dir = os.getenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR")
    if explicit_dir:
        return Path(explicit_dir)
    compensation_root = os.getenv("MM_COMPENSATION_ROOT")
    if compensation_root:
        return Path(compensation_root) / "build" / "retail_director_monthly"
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


def load_retail_director_monthly_kpi(month: str) -> dict[str, Any] | None:
    artifact_path = _artifact_path(month)
    if not artifact_path.exists():
        return None

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    header = _safe_dict(payload.get("header"))
    shrinkage = _safe_dict(payload.get("shrinkage"))
    compensation = _safe_dict(payload.get("compensation"))
    metadata = _safe_dict(payload.get("metadata"))
    shrinkage_metric = _extract_metric(payload, "shrinkage_rate")

    resolved_month = str(metadata.get("period_month") or month)
    return {
        "month": resolved_month,
        "title": header.get("title"),
        "subtitle": header.get("subtitle"),
        "overall_signal": header.get("overall_signal"),
        "close_status": header.get("close_status"),
        "writeoff_amount": shrinkage.get("writeoff_amount"),
        "receipt_amount": shrinkage.get("receipt_amount"),
        "shrinkage_amount": shrinkage.get("shrinkage_amount"),
        "shrinkage_pct": shrinkage.get("shrinkage_pct", shrinkage_metric.get("fact_value")),
        "matched_store_count": shrinkage.get("matched_store_count"),
        "kpi_index_sum": compensation.get("kpi_index_sum"),
        "kpi_bonus_amount": compensation.get("kpi_bonus_amount"),
        "to_pay": compensation.get("to_pay"),
        "warnings": [
            str(item) for item in _safe_list(payload.get("warnings")) if str(item).strip()
        ],
        "source_path": str(artifact_path),
    }
