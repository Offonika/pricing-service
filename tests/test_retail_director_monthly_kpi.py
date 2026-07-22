from __future__ import annotations

import json
from pathlib import Path

from app.services.retail_director_monthly_kpi import (
    load_retail_director_monthly_kpi,
    load_retail_director_monthly_kpi_history,
)


def _write_month(root: Path, month: str, payload: dict) -> None:
    target = root / month / f"retail-director-summary-{month}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_loader_keeps_v1_compatible(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(tmp_path))
    _write_month(
        tmp_path,
        "2026-06",
        {
            "header": {"subtitle": "Руководитель сети / Роль"},
            "metadata": {"period_month": "2026-06", "role_code": "retail_director"},
            "shrinkage": {
                "writeoff_amount": 1000,
                "receipt_amount": 250,
                "shrinkage_amount": 750,
                "shrinkage_pct": 0.5,
            },
            "top_metrics": [{"metric_code": "shrinkage_rate", "plan_value": 0.3}],
            "compensation": {},
            "warnings": [],
        },
    )

    payload = load_retail_director_monthly_kpi("2026-06")

    assert payload is not None
    assert payload["schema_version"] == 1
    assert payload["norm_pct"] == 0.3
    assert payload["stores"] == []
    assert payload["top_documents"] == []
    assert payload["owner"]["employee_name"] == "Руководитель сети"


def test_history_skips_gaps_and_scans_at_most_twelve_months(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(tmp_path))
    for month, loss in [("2026-05", 500), ("2026-03", 300), ("2025-12", 100)]:
        _write_month(
            tmp_path,
            month,
            {
                "schema_version": 2,
                "header": {},
                "metadata": {"period_month": month},
                "shrinkage": {
                    "writeoff_amount": loss,
                    "receipt_amount": 0,
                    "shrinkage_amount": loss,
                    "shrinkage_pct": loss / 1000,
                },
                "top_metrics": [],
                "compensation": {},
                "warnings": [],
            },
        )

    history = load_retail_director_monthly_kpi_history("2026-06")

    assert history["source_status"] == "ready"
    assert history["previous_month"]["month"] == "2026-05"
    assert [item["month"] for item in history["history"]] == [
        "2026-05",
        "2026-03",
        "2025-12",
    ]


def test_history_is_partial_when_fewer_than_three_months_exist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(tmp_path))
    _write_month(
        tmp_path,
        "2026-04",
        {
            "header": {},
            "metadata": {"period_month": "2026-04"},
            "shrinkage": {"shrinkage_amount": 100},
            "top_metrics": [],
            "compensation": {},
            "warnings": [],
        },
    )

    history = load_retail_director_monthly_kpi_history("2026-06")

    assert history["previous_month"] is None
    assert history["source_status"] == "partial"
    assert [item["month"] for item in history["history"]] == ["2026-04"]


def test_history_with_zero_limit_returns_no_months(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(tmp_path))
    _write_month(
        tmp_path,
        "2026-05",
        {
            "header": {},
            "metadata": {"period_month": "2026-05"},
            "shrinkage": {"shrinkage_amount": 100},
            "top_metrics": [],
            "compensation": {},
            "warnings": [],
        },
    )

    history = load_retail_director_monthly_kpi_history("2026-06", limit=0)

    assert history == {
        "previous_month": None,
        "history": [],
        "read_error_count": 0,
        "source_status": "ready",
    }


def test_history_skips_broken_artifact_and_continues_lookback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(tmp_path))
    broken = tmp_path / "2026-05" / "retail-director-summary-2026-05.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{broken", encoding="utf-8")
    for month, loss in [("2026-04", 400), ("2026-03", 300), ("2026-02", 200)]:
        _write_month(
            tmp_path,
            month,
            {
                "header": {},
                "metadata": {"period_month": month},
                "shrinkage": {"shrinkage_amount": loss},
                "top_metrics": [],
                "compensation": {},
                "warnings": [],
            },
        )

    history = load_retail_director_monthly_kpi_history("2026-06")

    assert history["previous_month"] is None
    assert history["read_error_count"] == 1
    assert history["source_status"] == "partial"
    assert [item["month"] for item in history["history"]] == [
        "2026-04",
        "2026-03",
        "2026-02",
    ]


def test_history_skips_semantically_invalid_month(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(tmp_path))
    for month, loss in [
        ("2026-05", "not-a-number"),
        ("2026-04", 400),
        ("2026-03", 300),
        ("2026-02", 200),
    ]:
        _write_month(
            tmp_path,
            month,
            {
                "header": {},
                "metadata": {"period_month": month},
                "shrinkage": {"shrinkage_amount": loss},
                "top_metrics": [],
                "compensation": {},
                "warnings": [],
            },
        )

    history = load_retail_director_monthly_kpi_history("2026-06")

    assert history["previous_month"] is None
    assert history["read_error_count"] == 1
    assert history["source_status"] == "partial"
    assert [item["month"] for item in history["history"]] == [
        "2026-04",
        "2026-03",
        "2026-02",
    ]


def test_history_skips_unquantizable_month_and_continues_lookback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(tmp_path))
    for month, loss in [
        ("2026-05", "1e999999"),
        ("2026-04", 400),
        ("2026-03", 300),
        ("2026-02", 200),
    ]:
        _write_month(
            tmp_path,
            month,
            {
                "header": {},
                "metadata": {"period_month": month},
                "shrinkage": {
                    "writeoff_amount": loss,
                    "receipt_amount": 0,
                    "shrinkage_amount": loss,
                    "shrinkage_pct": loss,
                },
                "top_metrics": [],
                "compensation": {},
                "warnings": [],
            },
        )

    history = load_retail_director_monthly_kpi_history("2026-06")

    assert history["previous_month"] is None
    assert history["read_error_count"] == 1
    assert history["source_status"] == "partial"
    assert [item["month"] for item in history["history"]] == [
        "2026-04",
        "2026-03",
        "2026-02",
    ]
