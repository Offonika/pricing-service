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
from app.models import Base
from app.models.weekly_kpi_report import WeeklyKpiReportMetricSnapshot, WeeklyKpiReportSnapshot
from app.services.weekly_kpi_reports import (
    build_pending_weekly_kpi_artifacts,
    publish_weekly_kpi_reports,
)


def setup_db():
    fd, path = tempfile.mkstemp(prefix="weekly_kpi_api_", suffix=".db")
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


def seed_weekly_kpi_reports(engine) -> None:
    with Session(engine) as session:
        eligible = WeeklyKpiReportSnapshot(
            report_key="emp_manager|2026-04-05",
            revision=1,
            week_start=date(2026, 3, 30),
            week_end=date(2026, 4, 5),
            employee_key="emp_manager",
            employee_name="Адамян Арен Вагеевич",
            role_code="pos_menedzher-po-prodazham",
            position_code="pos_menedzher-po-prodazham",
            position_name="Менеджер по продажам",
            bitrix_user_id="10959",
            lifecycle_status="draft",
            eligibility_status="eligible",
            artifact_status="pending",
            overall_signal="attention",
            summary_payload={
                "header": {
                    "title": "Итоги недели",
                    "subtitle": "Факт без плана",
                },
                "wins": ["Выручка выросла к прошлой неделе"],
                "risks": ["Средний чек просел"],
                "top_metrics": [
                    {"metric_code": "sales_revenue", "label": "Выручка", "signal": "good"},
                ],
                "next_actions": ["Поднять средний чек"],
                "overall_signal": "attention",
                "employee": {"employee_key": "emp_manager"},
                "period": {"week_end": "2026-04-05"},
            },
            source_as_of=date(2026, 4, 5),
        )
        eligible.metrics = [
            WeeklyKpiReportMetricSnapshot(
                metric_code="sales_revenue",
                metric_name="Выручка",
                unit="RUB",
                fact_value=Decimal("350000.00"),
                previous_fact_value=Decimal("320000.00"),
                delta_abs=Decimal("30000.00"),
                delta_pct=Decimal("9.3750"),
                signal="good",
                sort_order=10,
                source_system="pricing_service",
                source_entity="onec_sales_daily_kpi",
                source_as_of=date(2026, 4, 5),
                comment="Рост относительно прошлой недели",
            ),
            WeeklyKpiReportMetricSnapshot(
                metric_code="avg_ticket",
                metric_name="Средний чек",
                unit="RUB",
                fact_value=Decimal("875.00"),
                previous_fact_value=Decimal("910.00"),
                delta_abs=Decimal("-35.00"),
                delta_pct=Decimal("-3.8462"),
                signal="warning",
                sort_order=20,
                source_system="pricing_service",
                source_entity="onec_sales_daily_kpi",
                source_as_of=date(2026, 4, 5),
                comment="Ниже прошлой недели",
            ),
        ]
        quarantine = WeeklyKpiReportSnapshot(
            report_key="emp_unrouted|2026-04-05",
            revision=1,
            week_start=date(2026, 3, 30),
            week_end=date(2026, 4, 5),
            employee_key="emp_unrouted",
            employee_name="Сотрудник без маршрута",
            role_code="pos_assistent",
            position_code="pos_assistent",
            position_name="Ассистент",
            lifecycle_status="draft",
            eligibility_status="quarantine",
            eligibility_reason="missing_bitrix_mapping",
            artifact_status="pending",
            overall_signal="blocked",
            summary_payload={"header": {"title": "Не готово к доставке"}},
            source_as_of=date(2026, 4, 5),
        )
        session.add_all([eligible, quarantine])
        session.commit()


def test_weekly_kpi_publish_health_and_download(monkeypatch, tmp_path: Path) -> None:
    engine, path = setup_db()
    seed_weekly_kpi_reports(engine)

    with Session(engine) as session:
        publish_result = publish_weekly_kpi_reports(session, week_end=date(2026, 4, 5))
        assert publish_result["published_count"] == 1
        artifact_result = build_pending_weekly_kpi_artifacts(
            session,
            output_dir=tmp_path,
            week_end=date(2026, 4, 5),
        )
        assert artifact_result["built_count"] == 1
        session.commit()

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}

    health = client.get(
        "/api/management/weekly-kpi-reports/health",
        params={"week_end": "2026-04-05"},
        headers=headers,
    )
    assert health.status_code == 200
    health_payload = health.json()
    assert health_payload["report_count"] == 2
    assert health_payload["ready_count"] == 1
    assert health_payload["lifecycle_counts"]["published"] == 1
    assert health_payload["eligibility_counts"]["quarantine"] == 1
    assert health_payload["artifact_counts"]["ready"] == 1

    listing = client.get(
        "/api/management/weekly-kpi-reports",
        params={"week_end": "2026-04-05"},
        headers=headers,
    )
    assert listing.status_code == 200
    listing_payload = listing.json()["payload"]
    assert len(listing_payload) == 1
    report = listing_payload[0]
    assert report["report_key"] == "emp_manager|2026-04-05"
    assert report["employee"]["bitrix_user_id"] == "10959"
    assert report["artifact_url"].endswith(
        f"/api/management/weekly-kpi-reports/{report['report_id']}/artifact"
    )

    detail = client.get(
        f"/api/management/weekly-kpi-reports/{report['report_id']}",
        headers=headers,
    )
    assert detail.status_code == 200
    detail_payload = detail.json()["payload"]
    assert len(detail_payload["metrics"]) == 2
    assert detail_payload["metrics"][0]["metric_code"] == "sales_revenue"

    artifact = client.get(
        f"/api/management/weekly-kpi-reports/{report['report_id']}/artifact",
        headers=headers,
    )
    assert artifact.status_code == 200
    assert artifact.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert len(artifact.content) > 0

    filtered = client.get(
        "/api/management/weekly-kpi-reports",
        params={"week_end": "2026-04-05", "bitrix_user_id": "10959"},
        headers=headers,
    )
    assert filtered.status_code == 200
    assert len(filtered.json()["payload"]) == 1

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
