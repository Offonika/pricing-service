from __future__ import annotations

import os
import tempfile
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.services.telephony import TelephonyUserLineRow, sync_telephony_user_line_snapshot


def _setup_db():
    fd, path = tempfile.mkstemp(prefix="management_telephony_api_", suffix=".db")
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


def _seed(engine) -> None:
    with Session(engine) as session:
        sync_telephony_user_line_snapshot(
            session,
            rows=[
                TelephonyUserLineRow(
                    snapshot_date=date(2026, 4, 18),
                    mapping_source="onec_user_workstation",
                    user_ref_hex="u-1",
                    user_name="Асадбек",
                    physical_person_ref_hex="pp-1",
                    physical_person_name="Асадбек Олимжонов",
                    computer_name="-00-SAV-01",
                    extension="531",
                    store_name="Савелово",
                    staff_store_name="Савелово",
                    employment_status="active",
                    bitrix_user_id="10837",
                    bitrix_full_name="Асадбек Олимжонов",
                    has_extension=True,
                    has_bitrix=True,
                ),
                TelephonyUserLineRow(
                    snapshot_date=date(2026, 4, 18),
                    mapping_source="onec_user_workstation",
                    user_ref_hex="u-2",
                    user_name="Байрамгулыев",
                    physical_person_ref_hex="pp-2",
                    physical_person_name="Байрамгулыев Кувватгелди",
                    computer_name="-00-SPB-SAD-01",
                    extension="620",
                    store_name="СПБ Садовая",
                    staff_store_name="СПБ Садовая",
                    employment_status="active",
                    bitrix_user_id="10893",
                    bitrix_full_name="Байрамгулыев Кувватгелди",
                    has_extension=True,
                    has_bitrix=True,
                ),
                TelephonyUserLineRow(
                    snapshot_date=date(2026, 4, 18),
                    mapping_source="onec_user_workstation",
                    user_ref_hex="u-3",
                    user_name="Бигаев",
                    physical_person_ref_hex="pp-3",
                    physical_person_name="Бигаев Сергей",
                    computer_name="-00-SPB-SAD-02",
                    extension="620",
                    store_name="СПБ Садовая",
                    staff_store_name="СПБ Садовая",
                    employment_status="active",
                    has_extension=True,
                    has_bitrix=False,
                ),
            ],
            snapshot_date=date(2026, 4, 18),
        )
        session.commit()


def test_management_telephony_api_returns_snapshot_and_projection(monkeypatch) -> None:
    engine, path = _setup_db()
    _seed(engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}

    health = client.get(
        "/api/management/telephony/health",
        params={"date": "2026-04-18"},
        headers=headers,
    )
    assert health.status_code == 200
    health_payload = health.json()
    assert health_payload["status"] == "ok"
    assert health_payload["metrics"]["rows_total"] == 3

    employee_map = client.get(
        "/api/management/telephony/employee-line-map",
        params={"snapshot_date": "2026-04-18", "with_extension_only": "true"},
        headers=headers,
    )
    assert employee_map.status_code == 200
    assert len(employee_map.json()["payload"]) == 3

    retail_map = client.get(
        "/api/management/telephony/retail-line-map",
        params={"snapshot_date": "2026-04-18"},
        headers=headers,
    )
    assert retail_map.status_code == 200
    by_line = {item["line_id"]: item for item in retail_map.json()["payload"]}
    assert by_line["531"]["store_id"] == "telephony_user_10837"
    assert by_line["620"]["mapping_mode"] == "shared_extension"
    assert by_line["620"]["active_user_count"] == 2

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
