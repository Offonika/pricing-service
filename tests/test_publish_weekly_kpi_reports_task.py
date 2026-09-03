from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.db import SqlAlchemyUnitOfWork
from app.models import Base
from app.models.weekly_kpi_report import WeeklyKpiReportSnapshot
from tasks import publish_weekly_kpi_reports as publish_task


def _install_test_unit_of_work(monkeypatch, engine) -> None:
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    monkeypatch.setattr(
        publish_task,
        "SqlAlchemyUnitOfWork",
        lambda: SqlAlchemyUnitOfWork(factory),
    )


def test_publish_reports_rolls_back_complete_command_on_failure(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE event (id INTEGER PRIMARY KEY)"))
    _install_test_unit_of_work(monkeypatch, engine)

    def fail_after_write(session, **_kwargs):
        session.execute(text("INSERT INTO event (id) VALUES (1)"))
        raise RuntimeError("stop publication")

    monkeypatch.setattr(publish_task, "publish_weekly_kpi_reports", fail_after_write)

    with pytest.raises(RuntimeError, match="stop publication"):
        publish_task.publish_reports(week_end=date(2026, 4, 5), report_keys=None)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM event")).scalar_one() == 0
    engine.dispose()


def test_publish_reports_is_idempotent_after_committed_transition(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            WeeklyKpiReportSnapshot(
                report_key="employee|2026-04-05",
                revision=1,
                week_start=date(2026, 3, 30),
                week_end=date(2026, 4, 5),
                employee_key="employee",
                employee_name="Employee",
                lifecycle_status="draft",
                eligibility_status="eligible",
                artifact_status="pending",
            )
        )
        session.commit()
    _install_test_unit_of_work(monkeypatch, engine)

    first = publish_task.publish_reports(week_end=date(2026, 4, 5), report_keys=None)
    second = publish_task.publish_reports(week_end=date(2026, 4, 5), report_keys=None)

    assert first["published_count"] == 1
    assert second["published_count"] == 0
    with Session(engine) as session:
        report = session.execute(select(WeeklyKpiReportSnapshot)).scalar_one()
        assert report.lifecycle_status == "published"
        assert report.published_at is not None
    engine.dispose()
