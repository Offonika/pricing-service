from __future__ import annotations

import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base, OneCSalesDailyKpi, ReceivableCase
from app.services import weekly_manager_sales_reports as weekly_manager_sales_reports_service


def _setup_db():
    fd, path = tempfile.mkstemp(prefix="weekly_manager_sales_api_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def _override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def _seed_sales_and_receivables(engine) -> None:
    with Session(engine) as session:
        sales_rows = [
            (date(2026, 3, 30), "mgr-1", "Артем", "store-1", "Лира", "120000.00", "10.000"),
            (date(2026, 4, 2), "mgr-1", "Артем", "store-1", "Лира", "130000.00", "11.000"),
            (date(2026, 4, 4), "mgr-2", "Борис", "store-2", "Мега", "40000.00", "4.000"),
            (date(2026, 3, 23), "mgr-1", "Артем", "store-1", "Лира", "100000.00", "8.000"),
            (date(2026, 3, 26), "mgr-2", "Борис", "store-2", "Мега", "65000.00", "6.000"),
        ]
        for (
            sales_date,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            revenue,
            sales_count,
        ) in sales_rows:
            session.add(
                OneCSalesDailyKpi(
                    sales_date=sales_date,
                    manager_ref=manager_ref,
                    manager_name=manager_name,
                    store_ref=store_ref,
                    store_name=store_name,
                    revenue=Decimal(revenue),
                    cost_of_sales=Decimal("0.00"),
                    sales_count=Decimal(sales_count),
                )
            )

        receivable_rows = [
            (date(2026, 4, 5), "cp-1", "Платонов Андрей", "120000.00", "mgr-1", "Артем"),
            (date(2026, 4, 5), "cp-2", "Байрамов Эльвин", "80000.00", "mgr-2", "Борис"),
            (date(2026, 4, 3), "cp-1", "Платонов Андрей", "90000.00", "mgr-1", "Артем"),
        ]
        for (
            snapshot_date,
            counterparty_ref,
            counterparty_name,
            balance,
            manager_ref,
            manager_name,
        ) in receivable_rows:
            session.add(
                ReceivableCase(
                    snapshot_date=snapshot_date,
                    segment="employee",
                    owner_type="finance_hr",
                    recommendation="report",
                    counterparty_ref=counterparty_ref,
                    counterparty_name=counterparty_name,
                    current_balance=Decimal(balance),
                    aged_bucket="unknown",
                    activity_segment="inactive",
                    origin_document_ref=None,
                    origin_document_number=None,
                    origin_document_date=None,
                    origin_manager_ref=None,
                    origin_manager_name=None,
                    current_manager_ref=manager_ref,
                    current_manager_name=manager_name,
                    planned_payment_date=None,
                    credit_depth_days=None,
                    shipment_ban=None,
                    payment_term_source=None,
                    due_date=None,
                    overdue_days=None,
                    is_overdue=False,
                    chain_documents=None,
                )
            )
        session.commit()


def test_weekly_manager_sales_report_api_builds_manifest_and_downloads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    engine, path = _setup_db()
    _seed_sales_and_receivables(engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    monkeypatch.setenv("ONEC_DATABASE_URL", "")
    monkeypatch.setattr(weekly_manager_sales_reports_service, "DEFAULT_REPORT_DIR", tmp_path)
    get_settings.cache_clear()

    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}

    health = client.get(
        "/api/management/weekly-manager-sales-report/health",
        params={"week_end": "2026-04-05"},
        headers=headers,
    )
    assert health.status_code == 200
    health_payload = health.json()
    assert health_payload["status"] == "ready"
    assert health_payload["artifact_count"] == 2
    assert health_payload["manager_count"] == 2
    assert health_payload["employee_case_count"] == 2

    manifest_response = client.get(
        "/api/management/weekly-manager-sales-report",
        params={"week_end": "2026-04-05"},
        headers=headers,
    )
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()["payload"]
    assert manifest["report_key"] == "weekly-manager-sales|2026-04-05"
    assert manifest["period"]["employee_snapshot_date"] == "2026-04-05"
    assert len(manifest["artifacts"]) == 2
    assert {item["artifact_type"] for item in manifest["artifacts"]} == {"sales", "employee"}

    for artifact in manifest["artifacts"]:
        download = client.get(artifact["artifact_url"], headers=headers)
        assert download.status_code == 200
        assert download.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert len(download.content) > 0

    assert (tmp_path / "2026-04-05").exists()

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
