from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_engine
from app.core.config import get_settings
from app.main import app
from app.models import Base, ExpertiseCase, ExpertiseCaseEvent
from app.services import expertise as expertise_service


def setup_db():
    fd, path = tempfile.mkstemp(prefix="expertise_api_", suffix=".db")
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


def _auth_headers(token: str = "expertise-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _configure_expertise_auth(monkeypatch, token: str = "expertise-token") -> dict[str, str]:
    monkeypatch.setenv("EXPERTISE_INTERNAL_API_TOKEN", token)
    monkeypatch.delenv("LOGISTICS_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("MANAGEMENT_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("RETURN_SCHEME_INTERNAL_API_TOKEN", raising=False)
    for key in [
        "EXPERTISE_BITRIX_WEBHOOK_URL",
    ]:
        monkeypatch.setenv(key, "")
    for key in [
        "EXPERTISE_BITRIX_ENTITY_TYPE_ID",
        "EXPERTISE_BITRIX_CATEGORY_ID",
        "EXPERTISE_BITRIX_ROOT_FOLDER_ID",
        "EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID",
    ]:
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_STAGE_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_FIELD_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS", "[]")
    monkeypatch.setenv("EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP", "{}")
    monkeypatch.setenv(
        "EXPERTISE_SLA_STORE_GROUP_MAP",
        '{"store-1":"moscow","store-2":"spb","store-10":"other"}',
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_DELIVERY_DAYS_MAP",
        '{"moscow":2,"spb":8,"other":8}',
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_REVIEW_DAYS_MAP",
        '{"moscow":3,"spb":14,"other":14}',
    )
    get_settings.cache_clear()
    get_engine.cache_clear()
    return _auth_headers(token)


def _case_ids(engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            row.external_id: row.id
            for row in session.scalars(select(ExpertiseCase).order_by(ExpertiseCase.id.asc())).all()
        }


def _base_sync_payload() -> list[dict]:
    return [
        {
            "external_id": "exp-001",
            "onec_expertise_ref": "1c-exp-001",
            "onec_expertise_number": "ЭКС-0001",
            "created_at_source": "2026-04-01T10:00:00Z",
            "organization_ref": "org-001",
            "contract_ref": "contract-001",
            "linked_sale_ref": "sale-001",
            "linked_sale_number": "РБГУ010001",
            "store_external_id": "store-1",
            "store_name": "Магазин 1",
            "customer_name": "Иван Иванов",
            "problem_summary": None,
            "owner_user_external_id": "okk-1",
            "linked_customer_order_ref": "order-ref-001",
            "linked_customer_order_number": "ЗК-001",
            "payload": {
                "manager_comment": "Не заряжается",
                "quality_comment": "",
                "items": [
                    {
                        "return_reason_name": "Неисправен разъем питания",
                        "raw_fld9910": "0x01010800000000000000EFBBBF7B2255227D",
                    },
                    {
                        "return_reason_name": "Вторая строка документа",
                    },
                ],
            },
            "attachments": [
                {
                    "attachment_kind": "scan",
                    "storage_ref": "s3://expertise/scan-001.pdf",
                    "comment": "Бланк клиента",
                }
            ],
            "idempotency_key": "sync-exp-001-v1",
        },
        {
            "external_id": "exp-002",
            "onec_expertise_ref": "1c-exp-002",
            "onec_expertise_number": "ЭКС-0002",
            "created_at_source": "2026-03-01T10:00:00Z",
            "store_external_id": "store-2",
            "store_name": "Магазин 2",
            "customer_name": "Петр Петров",
            "owner_user_external_id": "okk-2",
            "payload": {
                "КомментарийМенеджера": "",
                "КомментарийОтделаБрака": "",
                "items": [
                    {
                        "return_reason_name": "Полоса на экране",
                    }
                ],
            },
            "idempotency_key": "sync-exp-002-v1",
        },
    ]


def test_expertise_wave1_api_flow(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_expertise_auth(monkeypatch)
    fixed_now = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(expertise_service, "utcnow", lambda: fixed_now)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    try:
        assert client.get("/api/expertise/cases").status_code == 401

        synced = client.post(
            "/api/expertise/sync/cases", json=_base_sync_payload(), headers=headers
        )
        assert synced.status_code == 200
        assert synced.json() == {"created": 2, "updated": 0}

        ids = _case_ids(engine)

        exp_001 = client.get(f"/api/expertise/cases/{ids['exp-001']}", headers=headers)
        assert exp_001.status_code == 200
        exp_001_json = exp_001.json()
        assert exp_001_json["organization_ref"] == "org-001"
        assert exp_001_json["contract_ref"] == "contract-001"
        assert exp_001_json["linked_sale_ref"] == "sale-001"
        assert exp_001_json["linked_sale_number"] == "РБГУ010001"
        assert exp_001_json["linked_customer_order_ref"] == "order-ref-001"
        assert exp_001_json["linked_customer_order_number"] == "ЗК-001"
        assert exp_001_json["problem_summary"] == "Не заряжается"
        assert exp_001_json["decision_comment"] is None
        assert exp_001_json["due_at"] == "2026-04-03T10:00:00"
        assert exp_001_json["attachments"][0]["attachment_kind"] == "scan"
        assert len(exp_001_json["payload"]["items"]) == 2

        exp_002 = client.get(f"/api/expertise/cases/{ids['exp-002']}", headers=headers)
        assert exp_002.status_code == 200
        exp_002_json = exp_002.json()
        assert exp_002_json["customer_phone"] is None
        assert exp_002_json["problem_summary"] == "Полоса на экране"
        assert exp_002_json["linked_customer_order_ref"] is None
        assert exp_002_json["due_at"] == "2026-03-09T10:00:00"

        overdue = client.get(
            "/api/expertise/cases",
            params={"overdue": "true"},
            headers=headers,
        )
        assert overdue.status_code == 200
        assert [item["external_id"] for item in overdue.json()] == ["exp-002", "exp-001"]

        store_filtered = client.get(
            "/api/expertise/cases",
            params={"store_external_id": "store-1", "status": "created"},
            headers=headers,
        )
        assert store_filtered.status_code == 200
        assert [item["external_id"] for item in store_filtered.json()] == ["exp-001"]
        assert store_filtered.json()[0]["linked_customer_order_ref"] == "order-ref-001"

        invalid_transition = client.post(
            f"/api/expertise/cases/{ids['exp-002']}/start-review",
            json={"actor_external_id": "okk-2"},
            headers=headers,
        )
        assert invalid_transition.status_code == 409
        assert invalid_transition.json()["detail"]["current_status"] == "created"

        invalid_decision = client.post(
            f"/api/expertise/cases/{ids['exp-001']}/decision",
            json={"actor_external_id": "okk-1", "decision_code": "something-else"},
            headers=headers,
        )
        assert invalid_decision.status_code == 422

        receive = client.post(
            f"/api/expertise/cases/{ids['exp-001']}/receive",
            json={
                "actor_external_id": "okk-1",
                "comment": "Принято в ОКК",
                "idempotency_key": "receive-001",
            },
            headers=headers,
        )
        assert receive.status_code == 200
        assert receive.json()["current_status"] == "received_by_okk"
        assert receive.json()["due_at"] == "2026-04-21T12:00:00"

        start_review = client.post(
            f"/api/expertise/cases/{ids['exp-001']}/start-review",
            json={
                "actor_external_id": "okk-1",
                "comment": "Пошли в работу",
                "idempotency_key": "review-001",
            },
            headers=headers,
        )
        assert start_review.status_code == 200
        assert start_review.json()["current_status"] == "under_review"
        assert start_review.json()["due_at"] == "2026-04-21T12:00:00"

        decision = client.post(
            f"/api/expertise/cases/{ids['exp-001']}/decision",
            json={
                "actor_external_id": "okk-1",
                "decision_code": "approved",
                "decision_comment": "Подтвержден дефект",
                "comment": "Решение зафиксировано",
                "idempotency_key": "decision-001",
            },
            headers=headers,
        )
        assert decision.status_code == 200
        assert decision.json()["current_status"] == "decision_ready"
        assert decision.json()["decision_code"] == "approved"
        assert decision.json()["due_at"] == "2026-04-19T12:00:00"

        decision_repeat = client.post(
            f"/api/expertise/cases/{ids['exp-001']}/decision",
            json={
                "actor_external_id": "okk-1",
                "decision_code": "approved",
                "decision_comment": "Подтвержден дефект",
                "idempotency_key": "decision-001",
            },
            headers=headers,
        )
        assert decision_repeat.status_code == 200
        assert decision_repeat.json()["current_status"] == "decision_ready"

        notified = client.post(
            f"/api/expertise/cases/{ids['exp-001']}/client-notified",
            json={
                "actor_external_id": "store-1-manager",
                "comment": "Клиенту позвонили",
                "idempotency_key": "notify-001",
            },
            headers=headers,
        )
        assert notified.status_code == 200
        assert notified.json()["current_status"] == "client_notified"
        assert notified.json()["client_notified"] is True
        assert notified.json()["due_at"] is None

        returned = client.post(
            f"/api/expertise/cases/{ids['exp-001']}/complete",
            json={
                "actor_external_id": "store-1-manager",
                "completion_outcome": "returned_to_central_defect",
                "comment": "Оформили в центральный брак",
                "idempotency_key": "return-001",
            },
            headers=headers,
        )
        assert returned.status_code == 200
        assert returned.json()["current_status"] == "returned_to_central_defect"
        assert returned.json()["due_at"] is None

        returned_overdue = client.get(
            "/api/expertise/cases",
            params={"overdue": "true", "status": "returned_to_central_defect"},
            headers=headers,
        )
        assert returned_overdue.status_code == 200
        assert returned_overdue.json() == []

        history = client.get(f"/api/expertise/cases/{ids['exp-001']}/history", headers=headers)
        assert history.status_code == 200
        event_types = [event["event_type"] for event in history.json()]
        assert event_types == [
            "returned_to_central_defect",
            "client_notified",
            "decision_recorded",
            "moved_to_review",
            "received_by_okk",
            "synced",
        ]
        assert not any(event_type.startswith("closed_") for event_type in event_types)

        with Session(engine) as session:
            total_cases = session.scalar(select(func.count()).select_from(ExpertiseCase))
            decision_events = session.scalar(
                select(func.count())
                .select_from(ExpertiseCaseEvent)
                .where(
                    ExpertiseCaseEvent.expertise_case_id == ids["exp-001"],
                    ExpertiseCaseEvent.event_type == "decision_recorded",
                )
            )
            synced_events = session.scalar(
                select(func.count())
                .select_from(ExpertiseCaseEvent)
                .where(ExpertiseCaseEvent.event_type == "synced")
            )
            assert total_cases == 2
            assert decision_events == 1
            assert synced_events == 2

        receive_rejected = client.post(
            f"/api/expertise/cases/{ids['exp-002']}/receive",
            json={"actor_external_id": "okk-2", "idempotency_key": "receive-002"},
            headers=headers,
        )
        assert receive_rejected.status_code == 200

        review_rejected = client.post(
            f"/api/expertise/cases/{ids['exp-002']}/start-review",
            json={"actor_external_id": "okk-2", "idempotency_key": "review-002"},
            headers=headers,
        )
        assert review_rejected.status_code == 200

        decision_rejected = client.post(
            f"/api/expertise/cases/{ids['exp-002']}/decision",
            json={
                "actor_external_id": "okk-2",
                "decision_code": "rejected",
                "decision_comment": "Дефект не подтвержден",
                "idempotency_key": "decision-002",
            },
            headers=headers,
        )
        assert decision_rejected.status_code == 200

        notified_rejected = client.post(
            f"/api/expertise/cases/{ids['exp-002']}/client-notified",
            json={"actor_external_id": "store-2-manager", "idempotency_key": "notify-002"},
            headers=headers,
        )
        assert notified_rejected.status_code == 200

        rejected_complete = client.post(
            f"/api/expertise/cases/{ids['exp-002']}/complete",
            json={
                "actor_external_id": "store-2-manager",
                "completion_outcome": "returned_to_central_defect",
            },
            headers=headers,
        )
        assert rejected_complete.status_code == 409

        returned_rejected = client.post(
            f"/api/expertise/cases/{ids['exp-002']}/return-to-store",
            json={
                "actor_external_id": "store-2-manager",
                "comment": "Вернули в подразделение",
                "idempotency_key": "return-002",
            },
            headers=headers,
        )
        assert returned_rejected.status_code == 200
        assert returned_rejected.json()["current_status"] == "returned_to_store"
    finally:
        client.close()
        app.dependency_overrides = {}
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_expertise_sync_update_and_sync_idempotency(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_expertise_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    try:
        initial_sync = [
            {
                "external_id": "exp-010",
                "onec_expertise_ref": "1c-exp-010",
                "onec_expertise_number": "ЭКС-0010",
                "created_at_source": "2026-04-01T10:00:00Z",
                "store_external_id": "store-10",
                "store_name": "Магазин 10",
                "customer_name": "Анна Смирнова",
                "customer_phone": "+79990000010",
                "owner_user_external_id": "okk-10",
                "linked_customer_order_ref": "order-ref-010",
                "linked_customer_order_number": "ЗК-010",
                "payload": {
                    "manager_comment": "",
                    "items": [{"return_reason_name": "Нет изображения"}],
                },
                "idempotency_key": "sync-exp-010-v1",
            }
        ]
        created = client.post("/api/expertise/sync/cases", json=initial_sync, headers=headers)
        assert created.status_code == 200
        assert created.json() == {"created": 1, "updated": 0}

        repeated = client.post("/api/expertise/sync/cases", json=initial_sync, headers=headers)
        assert repeated.status_code == 200
        assert repeated.json() == {"created": 0, "updated": 0}

        update_sync = [
            {
                "external_id": "exp-010",
                "onec_expertise_ref": "1c-exp-010",
                "onec_expertise_number": "ЭКС-0010",
                "created_at_source": "2026-04-01T10:00:00Z",
                "store_name": "Магазин 10 / обновлено",
                "owner_user_external_id": "okk-10",
                "linked_customer_order_ref": "order-ref-010-upd",
                "linked_customer_order_number": "ЗК-010-2",
                "decision_code": "rejected",
                "payload": {
                    "manager_comment": "Обновили summary",
                    "quality_comment": "Отдел брака отказал",
                    "items": [{"return_reason_name": "Не используется, есть manager_comment"}],
                },
                "idempotency_key": "sync-exp-010-v2",
            }
        ]
        updated = client.post("/api/expertise/sync/cases", json=update_sync, headers=headers)
        assert updated.status_code == 200
        assert updated.json() == {"created": 0, "updated": 1}

        case_id = _case_ids(engine)["exp-010"]
        detail = client.get(f"/api/expertise/cases/{case_id}", headers=headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["store_name"] == "Магазин 10 / обновлено"
        assert body["customer_phone"] == "+79990000010"
        assert body["problem_summary"] == "Обновили summary"
        assert body["decision_code"] == "rejected"
        assert body["decision_comment"] == "Отдел брака отказал"
        assert body["linked_customer_order_ref"] == "order-ref-010-upd"
        assert body["linked_customer_order_number"] == "ЗК-010-2"

        with Session(engine) as session:
            synced_events = session.scalar(
                select(func.count())
                .select_from(ExpertiseCaseEvent)
                .where(ExpertiseCaseEvent.event_type == "synced")
            )
            assert synced_events == 2
    finally:
        client.close()
        app.dependency_overrides = {}
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_backfill_completion_outcomes_updates_approved_terminal_cases(monkeypatch) -> None:
    engine, path = setup_db()
    _configure_expertise_auth(monkeypatch)

    try:
        with Session(engine) as session:
            case_row = ExpertiseCase(
                external_id="exp-backfill-001",
                onec_expertise_ref="1c-exp-backfill-001",
                onec_expertise_number="ЭКС-BACKFILL-001",
                created_at_source=datetime(2026, 4, 1, 10, 0, 0),
                owner_user_external_id="okk-1",
                current_status="returned_to_store",
                decision_code="approved",
                decision_label="Принято",
                client_notified=True,
                payload={"items": [{"line_no": 1}]},
            )
            session.add(case_row)
            session.commit()
            session.refresh(case_row)
            session.add(
                ExpertiseCaseEvent(
                    expertise_case_id=case_row.id,
                    event_type="returned_to_store",
                    event_at=func.now(),
                    source="api",
                    meta={"to_status": "returned_to_store"},
                )
            )
            session.commit()

            summary = expertise_service.backfill_completion_outcomes(session)
            session.refresh(case_row)
            event = session.scalar(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == case_row.id
                )
            )

            assert summary == {"updated": 1}
            assert case_row.current_status == "returned_to_central_defect"
            assert event is not None
            assert event.event_type == "returned_to_central_defect"
            assert event.meta["to_status"] == "returned_to_central_defect"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)
