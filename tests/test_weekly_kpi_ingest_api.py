from __future__ import annotations

from collections.abc import Generator
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_db, get_uow, require_weekly_kpi_ingest_token
from app.infrastructure.db import SqlAlchemyUnitOfWork
from app.main import app
from app.models import Base
from app.models.onec_sales_daily_kpi import OneCSalesDailyKpi
from app.models.weekly_kpi_report import WeeklyKpiReportSnapshot


def _payload(*, fact_value: str = "350000.00") -> dict[str, object]:
    return {
        "contract_version": "weekly-kpi-report.v1",
        "generated_at": "2026-04-06T08:00:00",
        "reports": [
            {
                "report_key": "emp_manager|2026-04-05",
                "revision": 1,
                "week_start": "2026-03-30",
                "week_end": "2026-04-05",
                "employee_key": "emp_manager",
                "employee_name": "Адамян Арен Вагеевич",
                "role_code": "pos_menedzher-po-prodazham",
                "position_code": "pos_menedzher-po-prodazham",
                "position_name": "Менеджер по продажам",
                "bitrix_user_id": "10959",
                "eligibility_status": "eligible",
                "overall_signal": "attention",
                "summary_payload": {"header": {"title": "Итоги недели"}},
                "source_as_of": "2026-04-05",
                "generated_at": "2026-04-06T08:00:00",
                "metrics": [
                    {
                        "metric_code": "sales_revenue",
                        "metric_name": "Выручка",
                        "unit": "RUB",
                        "fact_value": fact_value,
                        "sort_order": 10,
                        "source_system": "mm_compensation",
                        "source_entity": "weekly_kpi_report.v1",
                        "source_as_of": "2026-04-05",
                    }
                ],
            }
        ],
    }


def test_weekly_kpi_ingest_is_atomic_and_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'weekly-kpi-ingest.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_uow() -> Generator[SqlAlchemyUnitOfWork, None, None]:
        with SqlAlchemyUnitOfWork(factory) as unit_of_work:
            yield unit_of_work

    def override_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    with factory.begin() as session:
        session.add(
            OneCSalesDailyKpi(
                sales_date=date(2026, 4, 1),
                manager_ref="mgr-1",
                manager_name="Менеджер 1",
                store_ref="store-1",
                store_name="Магазин 1",
                revenue="350000.00",
                cost_of_sales="250000.00",
                sales_count="400.000",
            )
        )

    app.dependency_overrides = {
        get_db: override_db,
        get_uow: override_uow,
        require_weekly_kpi_ingest_token: lambda: "test-token",
    }
    try:
        client = TestClient(app)
        headers = {"Idempotency-Key": "weekly-kpi-2026-04-05-01"}

        source = client.get(
            "/api/management/internal/sales-daily-kpi",
            params={"date_from": "2026-03-30", "date_to": "2026-04-05"},
        )
        assert source.status_code == 200
        assert source.json()[0]["manager_ref"] == "mgr-1"

        created = client.post(
            "/api/management/internal/weekly-kpi-snapshots",
            headers=headers,
            json=_payload(),
        )
        assert created.status_code == 200
        assert created.json() == {
            "contract_version": "weekly-kpi-report.v1",
            "inserted": 1,
            "updated": 0,
            "noop": 0,
            "quarantined": 0,
            "replayed": False,
        }

        replayed = client.post(
            "/api/management/internal/weekly-kpi-snapshots",
            headers=headers,
            json=_payload(),
        )
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True

        conflict = client.post(
            "/api/management/internal/weekly-kpi-snapshots",
            headers=headers,
            json=_payload(fact_value="360000.00"),
        )
        assert conflict.status_code == 409

        noop = client.post(
            "/api/management/internal/weekly-kpi-snapshots",
            headers={"Idempotency-Key": "weekly-kpi-2026-04-05-02"},
            json=_payload(),
        )
        assert noop.status_code == 200
        assert noop.json()["noop"] == 1

        with Session(engine) as session:
            snapshots = session.scalars(select(WeeklyKpiReportSnapshot)).all()
            assert len(snapshots) == 1
            assert snapshots[0].week_end == date(2026, 4, 5)
            assert len(snapshots[0].metrics) == 1
    finally:
        app.dependency_overrides = {}
        engine.dispose()
