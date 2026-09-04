from __future__ import annotations

import hashlib
import zipfile
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.db import SqlAlchemyUnitOfWork
from app.models import Base
from app.models.weekly_kpi_report import WeeklyKpiReportSnapshot
from app.services import weekly_kpi_reports as weekly_kpi_service
from tasks import build_weekly_kpi_artifacts as build_task


def _install_test_unit_of_work(monkeypatch, engine) -> None:
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    monkeypatch.setattr(
        build_task,
        "SqlAlchemyUnitOfWork",
        lambda: SqlAlchemyUnitOfWork(factory),
    )


def test_partial_artifact_failure_rolls_back_and_retry_replaces_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'weekly-kpi.db'}")
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
                lifecycle_status="published",
                eligibility_status="eligible",
                artifact_status="pending",
            )
        )
        session.commit()
    _install_test_unit_of_work(monkeypatch, engine)

    artifact_path = (
        tmp_path
        / "artifacts"
        / "2026-04-05"
        / "employee"
        / "weekly-kpi-employee-2026-03-30-to-2026-04-05-r1.xlsx"
    )
    original_export = weekly_kpi_service.export_weekly_kpi_report_artifact

    def write_partial_then_fail(_report, *, output_dir: Path):
        assert output_dir == tmp_path / "artifacts"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(b"partial-xlsx")
        raise RuntimeError("artifact write interrupted")

    monkeypatch.setattr(
        weekly_kpi_service,
        "export_weekly_kpi_report_artifact",
        write_partial_then_fail,
    )

    with pytest.raises(RuntimeError, match="artifact write interrupted"):
        build_task.build_artifacts(
            output_dir=tmp_path / "artifacts",
            week_end=date(2026, 4, 5),
            report_ids=None,
        )

    assert artifact_path.read_bytes() == b"partial-xlsx"
    with Session(engine) as session:
        report = session.execute(select(WeeklyKpiReportSnapshot)).scalar_one()
        assert report.artifact_status == "pending"
        assert report.artifact_path is None
        assert report.artifact_sha256 is None

    monkeypatch.setattr(
        weekly_kpi_service,
        "export_weekly_kpi_report_artifact",
        original_export,
    )
    built = build_task.build_artifacts(
        output_dir=tmp_path / "artifacts",
        week_end=date(2026, 4, 5),
        report_ids=None,
    )
    repeated = build_task.build_artifacts(
        output_dir=tmp_path / "artifacts",
        week_end=date(2026, 4, 5),
        report_ids=None,
    )

    assert built["built_count"] == 1
    assert repeated["built_count"] == 0
    assert zipfile.is_zipfile(artifact_path)
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    assert built["reports"] == [
        {
            "report_id": 1,
            "artifact_path": str(artifact_path),
            "sha256": digest,
        }
    ]
    with Session(engine) as session:
        report = session.execute(select(WeeklyKpiReportSnapshot)).scalar_one()
        assert report.artifact_status == "ready"
        assert report.artifact_path == str(artifact_path)
        assert report.artifact_sha256 == digest
    engine.dispose()
