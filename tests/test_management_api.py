from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import management as management_api
from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.services.receivables import (
    OneCReceivableLedgerExtractor,
    sync_receivable_ledger,
)
from app.services.staffing import sync_staffing_data
from tests.test_receivables import NORMALIZED_SQL, _setup_onec_source
from tests.test_staffing import _fact_rows, _plan_rows, _staff_rows


def setup_db():
    fd, path = tempfile.mkstemp(prefix="management_api_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def test_retail_director_monthly_kpi_returns_source_error_for_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        management_api,
        "load_retail_director_monthly_kpi",
        lambda _: {
            "schema_version": 2,
            "month": "2026-06",
            "writeoff_amount": "not-a-number",
        },
    )

    response = management_api.get_retail_director_monthly_kpi(
        month="2026-06",
        _="test-token",
    )

    assert response.source_status == "source_error"
    assert response.freshness_status == "error"
    assert response.payload is None


def seed_management_data(engine) -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_source(onec_engine)
    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(engine) as session:
        sync_receivable_ledger(
            session,
            events,
            snapshot_date=date(2026, 3, 20),
            employee_counterparty_refs=["cp-b"],
            fired_manager_refs=["mgr-4"],
        )
        sync_staffing_data(
            session,
            staff_members=_staff_rows(),
            shift_plans=_plan_rows(),
            shift_facts=_fact_rows(),
            snapshot_dates=[date(2026, 3, 20), date(2026, 3, 21), date(2026, 3, 22)],
        )
        session.commit()
    onec_engine.dispose()


def seed_retail_director_monthly_report(tmp_path: Path) -> None:
    report_dir = tmp_path / "2026-03"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "retail-director-summary-2026-03.json").write_text(
        """
{
  "schema_version": 2,
  "header": {
    "title": "Итоги месяца 2026-03",
    "subtitle": "Сагиян Арсен Левонович / Руководитель сети торговых точек",
    "overall_signal": "attention",
    "close_status": "review"
  },
  "shrinkage": {
    "writeoff_amount": 1229121.82,
    "receipt_amount": 526672.97,
    "shrinkage_amount": 702448.85,
    "shrinkage_pct": 0.8499,
    "norm_pct": 0.5,
    "matched_store_count": 12,
    "stores": [
      {
        "store_ref": "store-1",
        "store_name": "Магазин 1",
        "sales_amount": 100000,
        "writeoff_amount": 1000,
        "receipt_amount": 100,
        "shrinkage_amount": 900,
        "shrinkage_pct": 0.9,
        "norm_pct": 0.5,
        "variance_to_norm_pct": 0.4,
        "above_norm": true,
        "source_status": "ready",
        "has_operations": true
      }
    ],
    "top_documents": [
      {
        "stable_key": "writeoff:doc-1",
        "operation_kind": "inventory_writeoff",
        "operation_label": "Инвентаризационное списание",
        "document_type": "_Document210",
        "document_ref": "doc-1",
        "document_number": "0001",
        "document_date": "2026-03-15",
        "store_ref": "store-1",
        "store_name": "Магазин 1",
        "amount": 1000,
        "effect_amount": 1000
      }
    ],
    "data_quality": {
      "source_status": "ready",
      "approved_store_count": 12,
      "source_store_count": 12,
      "matched_store_count": 12,
      "unmatched_store_count": 0,
      "source_document_count": 1,
      "matched_document_count": 1,
      "unmatched_document_count": 0,
      "unmatched_writeoff_amount": "0.00",
      "unmatched_receipt_amount": "0.00"
    },
    "owner": {
      "employee_name": "Сагиян Арсен Левонович",
      "role_code": "retail_director"
    }
  },
  "compensation": {
    "kpi_index_sum": 0.7214,
    "kpi_bonus_amount": 54105.0,
    "to_pay": 234105.0
  },
  "warnings": [
    "Тестовый monthly artifact"
  ],
  "metadata": {
    "period_month": "2026-03"
  }
}
        """.strip(),
        encoding="utf-8",
    )


def test_management_api_endpoints(monkeypatch) -> None:
    engine, path = setup_db()
    seed_management_data(engine)
    monthly_tmp_dir = Path(tempfile.mkdtemp(prefix="retail_director_monthly_"))
    seed_retail_director_monthly_report(monthly_tmp_dir)

    def fake_load_task_efficiency_report(**kwargs):
        month = kwargs["month"]
        return {
            "as_of": month,
            "month": month,
            "month_start": date(2026, 3, 1),
            "month_end": date(2026, 3, 31),
            "freshness_status": "fresh",
            "source_status": "ready",
            "note": None,
            "summary": {
                "employee_count": 1,
                "applicable_count": 1,
                "total_personal_tasks_with_deadline": 4,
                "closed_on_time_personal_tasks": 3,
                "late_closed_personal_tasks": 1,
                "open_overdue_personal_tasks": 0,
                "canceled_personal_tasks": 0,
                "average_on_time_share": 75.0,
                "bitrix_average_effectiveness_pct": 75.0,
                "bitrix_total_in_work_count": 4,
                "bitrix_completed_tasks_count": 3,
                "bitrix_task_remarks_count": 1,
                "low_efficiency_threshold": 80.0,
                "low_efficiency_count": 1,
            },
            "payload": [
                {
                    "month_start": date(2026, 3, 1),
                    "month_end": date(2026, 3, 31),
                    "employee_bitrix_id": "2",
                    "employee_key": "emp-petr",
                    "employee_name": "Петр",
                    "total_personal_tasks_with_deadline": 4,
                    "closed_on_time_personal_tasks": 3,
                    "late_closed_personal_tasks": 1,
                    "open_overdue_personal_tasks": 0,
                    "canceled_personal_tasks": 0,
                    "personal_tasks_on_time_share": 75.0,
                    "bitrix_total_in_work_count": 4,
                    "bitrix_completed_tasks_count": 3,
                    "bitrix_task_remarks_count": 1,
                    "bitrix_effectiveness_pct": 75.0,
                    "is_metric_applicable": True,
                }
            ],
        }

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    monkeypatch.setenv("RETAIL_DIRECTOR_MONTHLY_REPORTS_DIR", str(monthly_tmp_dir))
    monkeypatch.setattr(
        "app.api.management.load_task_efficiency_report",
        fake_load_task_efficiency_report,
    )
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    unauthorized = client.get("/api/receivables/new-daily", params={"date": "2026-03-20"})
    assert unauthorized.status_code == 401

    headers = {"Authorization": "Bearer secret-token"}

    new_daily = client.get(
        "/api/receivables/new-daily",
        params={"date": "2026-03-20"},
        headers=headers,
    )
    assert new_daily.status_code == 200
    new_daily_payload = new_daily.json()
    assert new_daily_payload["freshness_status"] == "fresh"
    assert len(new_daily_payload["payload"]) == 1
    assert new_daily_payload["payload"][0]["counterparty_ref"] == "cp-d"

    employee_cases = client.get(
        "/api/receivables/employee-cases",
        params={"date": "2026-03-20"},
        headers=headers,
    )
    assert employee_cases.status_code == 200
    assert employee_cases.json()["payload"][0]["counterparty_ref"] == "cp-b"

    manager_summary = client.get(
        "/api/receivables/manager-summary",
        params={"date": "2026-03-20"},
        headers=headers,
    )
    assert manager_summary.status_code == 200
    manager_payload = manager_summary.json()["payload"]
    manager_mgr5 = next(item for item in manager_payload if item["manager_ref"] == "mgr-5")
    assert manager_mgr5["new_daily_count"] == 1

    staffing_daily = client.get(
        "/api/staffing/daily",
        params={"date": "2026-03-20"},
        headers=headers,
    )
    assert staffing_daily.status_code == 200
    staffing_daily_payload = staffing_daily.json()
    assert staffing_daily_payload["freshness_status"] == "fresh"
    assert len(staffing_daily_payload["payload"]) == 2
    store_1 = next(
        item for item in staffing_daily_payload["payload"] if item["store_ref"] == "store-1"
    )
    assert store_1["deficit_count"] == 1
    assert store_1["criticality"] == "warning"

    staffing_summary = client.get(
        "/api/staffing/period-summary",
        params={"date_from": "2026-03-20", "date_to": "2026-03-22"},
        headers=headers,
    )
    assert staffing_summary.status_code == 200
    summary_payload = staffing_summary.json()["payload"]
    store_1_summary = next(item for item in summary_payload if item["store_ref"] == "store-1")
    assert store_1_summary["days_with_deficit"] == 3
    assert store_1_summary["forecast_deficit_days"] == {"3": 2, "7": 2, "14": 2}

    retail_director_monthly = client.get(
        "/api/management/retail-director-monthly-kpi",
        params={"month": "2026-03"},
        headers=headers,
    )
    assert retail_director_monthly.status_code == 200
    retail_director_monthly_payload = retail_director_monthly.json()
    assert retail_director_monthly_payload["source_status"] == "ready"
    assert retail_director_monthly_payload["payload"]["schema_version"] == 2
    assert retail_director_monthly_payload["payload"]["shrinkage_amount"] == "702448.85"
    assert retail_director_monthly_payload["payload"]["norm_pct"] == "0.5"
    assert retail_director_monthly_payload["payload"]["stores"][0]["store_ref"] == "store-1"
    assert (
        retail_director_monthly_payload["payload"]["top_documents"][0]["document_number"] == "0001"
    )
    assert retail_director_monthly_payload["payload"]["kpi_bonus_amount"] == "54105.0"

    task_efficiency = client.get(
        "/api/management/task-efficiency",
        params={"month": "2026-03"},
        headers=headers,
    )
    assert task_efficiency.status_code == 200
    task_efficiency_payload = task_efficiency.json()
    assert task_efficiency_payload["source_status"] == "ready"
    assert task_efficiency_payload["summary"]["employee_count"] == 1
    assert task_efficiency_payload["summary"]["bitrix_task_remarks_count"] == 1
    assert task_efficiency_payload["payload"][0]["employee_name"] == "Петр"
    assert task_efficiency_payload["payload"][0]["bitrix_effectiveness_pct"] == "75.0"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
    shutil.rmtree(monthly_tmp_dir, ignore_errors=True)
