from __future__ import annotations

import os
import tempfile
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.services.counterparty_duplicates import (
    CounterpartySnapshotRecord,
    detect_counterparty_duplicate_cases,
    upsert_counterparty_duplicate_cases,
)


def setup_db():
    fd, path = tempfile.mkstemp(prefix="counterparty_duplicates_api_", suffix=".db")
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


def _record(ref: str, phone: str) -> CounterpartySnapshotRecord:
    return CounterpartySnapshotRecord.from_mapping(
        {
            "counterparty_ref": ref,
            "counterparty_name": ref,
            "phone": phone,
            "updated_at": "2026-03-24T10:00:00",
            "responsible_code": "finance",
        }
    )


def seed_cases(engine) -> int:
    with Session(engine) as session:
        detected = detect_counterparty_duplicate_cases(
            [_record("cp-1", "+7 777 1234567"), _record("cp-2", "8 777 123 45 67")]
        )
        result = upsert_counterparty_duplicate_cases(
            session,
            detected,
            detected_at=datetime(2026, 3, 24, 12, 0, 0),
            anti_duplicate_window_hours=24,
        )
        session.commit()
        return result["new"][0].id


def test_counterparty_duplicates_internal_api(monkeypatch) -> None:
    engine, path = setup_db()
    case_id = seed_cases(engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    unauthorized = client.get("/api/internal/counterparty-duplicates/pending")
    assert unauthorized.status_code == 401

    headers = {"Authorization": "Bearer secret-token"}
    pending = client.get("/api/internal/counterparty-duplicates/pending", headers=headers)
    assert pending.status_code == 200
    payload = pending.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["case_id"] == case_id

    health = client.get("/api/internal/counterparty-duplicates/health", headers=headers)
    assert health.status_code == 200
    assert health.json()["source_status"] == "ready"

    ack = client.post(
        f"/api/internal/counterparty-duplicates/{case_id}/ack",
        headers=headers,
        json={
            "external_case_id": "sp-101",
            "external_status": "Новый",
            "external_url": "https://bitrix.example/sp/101",
            "status": "in_progress",
        },
    )
    assert ack.status_code == 200
    assert ack.json()["delivery_state"] == "acked"
    assert ack.json()["external_case_id"] == "sp-101"

    pending_after = client.get("/api/internal/counterparty-duplicates/pending", headers=headers)
    assert pending_after.status_code == 200
    assert pending_after.json()["items"] == []

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
