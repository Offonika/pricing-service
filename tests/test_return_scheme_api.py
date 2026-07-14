from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.services.return_scheme import (
    create_return_scheme_alert_batch,
    detect_return_scheme_incidents,
    export_return_scheme_report_xlsx,
    parse_retail_price_types,
    upsert_return_scheme_incidents,
)
from tests.test_return_scheme import _event


def setup_db():
    fd, path = tempfile.mkstemp(prefix="return_scheme_api_", suffix=".db")
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


def seed_batch(engine, report_path: Path) -> int:
    with Session(engine) as session:
        incidents = detect_return_scheme_incidents(
            [
                _event(event_type="sale", hours=0, price_type="Розница", suffix="sale1"),
                _event(event_type="return", hours=1, suffix="return1"),
                _event(event_type="sale", hours=2, price_type="Опт", amount="90", suffix="sale2"),
            ],
            retail_price_types=parse_retail_price_types("Розница"),
            window_days=7,
        )
        persisted = upsert_return_scheme_incidents(session, incidents, detected_at=datetime.now())
        export_return_scheme_report_xlsx(persisted["pending_notification"], report_path)
        batch = create_return_scheme_alert_batch(
            session,
            incidents=persisted["pending_notification"],
            generated_at=datetime(2026, 3, 2, 9, 0, 0),
            window_start=datetime(2026, 2, 24, 9, 0, 0),
            window_end=datetime(2026, 3, 2, 9, 0, 0),
            report_path=report_path,
            new_incidents_count=len(persisted["new"]),
        )
        session.commit()
        return int(batch.id)


def test_internal_return_scheme_alert_api(monkeypatch, tmp_path: Path) -> None:
    engine, path = setup_db()
    report_path = tmp_path / "return_scheme.xlsx"
    batch_id = seed_batch(engine, report_path)

    monkeypatch.setenv("RETURN_SCHEME_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    unauthorized = client.get("/api/internal/alerts/return-scheme/pending")
    assert unauthorized.status_code == 401

    headers = {"Authorization": "Bearer secret-token"}
    pending = client.get("/api/internal/alerts/return-scheme/pending", headers=headers)
    assert pending.status_code == 200
    payload = pending.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == batch_id
    assert payload["items"][0]["incident_ids"]

    report = client.get(f"/api/internal/alerts/return-scheme/{batch_id}/report", headers=headers)
    assert report.status_code == 200
    assert report.content

    ack = client.post(f"/api/internal/alerts/return-scheme/{batch_id}/ack", headers=headers)
    assert ack.status_code == 200
    assert ack.json()["status"] == "delivered"

    pending_after_ack = client.get("/api/internal/alerts/return-scheme/pending", headers=headers)
    assert pending_after_ack.status_code == 200
    assert pending_after_ack.json()["items"] == []

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
