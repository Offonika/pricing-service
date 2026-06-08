from __future__ import annotations

import os
import tempfile
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Base, ExpertiseCase, ExpertiseCaseEvent
from app.services.expertise_onec import (
    build_expertise_sync_payloads,
    load_expertise_onec_sql,
    normalize_onec_decision_code,
)
from app.workers.expertise import run_expertise_onec_sync


def _setup_db():
    fd, path = tempfile.mkstemp(prefix="expertise_worker_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


class _FakeExtractor:
    def __init__(self, payloads):
        self.payloads = payloads

    def fetch_case_payloads(self):
        return self.payloads


def test_build_expertise_sync_payloads_groups_rows_into_one_case() -> None:
    rows = [
        {
            "external_id": "ref-001",
            "onec_expertise_ref": "ref-001",
            "onec_expertise_number": "ЭКС-001",
            "created_at_source": datetime(2026, 4, 1, 10, 0, 0),
            "organization_ref": "org-1",
            "contract_ref": "contract-1",
            "linked_sale_ref": "sale-ref-1",
            "linked_sale_number": "РБГУ0001",
            "store_external_id": "store-1",
            "store_name": "Щёлковская",
            "customer_name": "Мамедов Акпер",
            "customer_phone": "+79990000001",
            "owner_user_external_id": "responsible-1",
            "manager_comment": "Черное пятно над челкой",
            "quality_comment": "Подтвержден брак",
            "decision_label": "Принято",
            "item_line_no": 1,
            "item_nomenclature_name": "Дисплей iPhone",
            "item_return_reason_name": "Изображение некорректное",
            "item_linked_customer_order_ref": "order-ref-1",
            "item_linked_customer_order_number": "ЗК-1",
        },
        {
            "external_id": "ref-001",
            "onec_expertise_ref": "ref-001",
            "onec_expertise_number": "ЭКС-001",
            "created_at_source": datetime(2026, 4, 1, 10, 0, 0),
            "owner_user_external_id": "responsible-1",
            "item_line_no": 2,
            "item_nomenclature_name": "Вторая строка",
            "item_return_reason_name": "Нет изображения",
            "item_decision_label": "Отказано",
            "item_linked_customer_order_ref": "order-ref-1",
            "item_linked_customer_order_number": "ЗК-1",
        },
    ]

    payloads = build_expertise_sync_payloads(rows)

    assert len(payloads) == 1
    item = payloads[0]
    assert item["external_id"] == "ref-001"
    assert item["organization_ref"] == "org-1"
    assert item["contract_ref"] == "contract-1"
    assert item["linked_sale_ref"] == "sale-ref-1"
    assert item["linked_sale_number"] == "РБГУ0001"
    assert item["decision_code"] == "approved"
    assert item["decision_label"] == "Принято"
    assert item["problem_summary"] == "Черное пятно над челкой"
    assert item["decision_comment"] == "Подтвержден брак"
    assert item["linked_customer_order_ref"] == "order-ref-1"
    assert item["linked_customer_order_number"] == "ЗК-1"
    assert len(item["payload"]["items"]) == 2
    assert item["payload"]["items"][0]["decision_code"] == "approved"
    assert "raw_fld9910" not in item["payload"]["items"][0]


def test_run_expertise_onec_sync_persists_cases(monkeypatch) -> None:
    engine, path = _setup_db()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
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
    get_settings.cache_clear()

    try:
        result = run_expertise_onec_sync(
            extractor=_FakeExtractor(
                [
                    {
                        "external_id": "exp-worker-1",
                        "onec_expertise_ref": "exp-worker-1",
                        "onec_expertise_number": "ЭКС-W1",
                        "created_at_source": datetime(2026, 4, 1, 10, 0, 0),
                        "organization_ref": "org-worker",
                        "contract_ref": "contract-worker",
                        "linked_sale_ref": "sale-ref-worker",
                        "linked_sale_number": "РБГУ-W1",
                        "store_external_id": "store-1",
                        "store_name": "Store 1",
                        "customer_name": "Иван Иванов",
                        "customer_phone": None,
                        "owner_user_external_id": "okk-1",
                        "linked_customer_order_ref": "order-ref-worker",
                        "linked_customer_order_number": "ЗК-W1",
                        "problem_summary": None,
                        "decision_comment": None,
                        "decision_code": "approved",
                        "payload": {
                            "posted": False,
                            "manager_comment": "Гаснет экран",
                            "quality_comment": "",
                            "items": [{"line_no": 1, "return_reason_name": "Нет изображения"}],
                        },
                    }
                ]
            )
        )

        assert result == {"created": 1, "updated": 0, "fetched": 1}

        with Session(engine) as session:
            stored = session.scalar(
                select(ExpertiseCase).where(ExpertiseCase.external_id == "exp-worker-1")
            )
            assert stored is not None
            assert stored.organization_ref == "org-worker"
            assert stored.contract_ref == "contract-worker"
            assert stored.linked_sale_ref == "sale-ref-worker"
            assert stored.linked_sale_number == "РБГУ-W1"
            assert stored.problem_summary == "Гаснет экран"
            assert stored.linked_customer_order_ref == "order-ref-worker"
            assert stored.linked_customer_order_number == "ЗК-W1"
            assert stored.current_status == "decision_ready"
            event = session.scalar(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == stored.id,
                    ExpertiseCaseEvent.event_type == "decision_recorded",
                )
            )
            assert event is not None
            assert event.source == "sync"
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_expertise_onec_helpers() -> None:
    assert normalize_onec_decision_code("Принято") == "approved"
    assert normalize_onec_decision_code("Отказано") == "rejected"
    assert normalize_onec_decision_code("approved") == "approved"
    assert normalize_onec_decision_code("unknown") is None


def test_load_expertise_onec_sql_from_file(tmp_path) -> None:
    sql_path = tmp_path / "expertise.sql"
    sql_path.write_text("SELECT 1", encoding="utf-8")

    class _Settings:
        expertise_onec_sql = None
        expertise_onec_sql_file = str(sql_path)

    assert load_expertise_onec_sql(_Settings()) == "SELECT 1"
