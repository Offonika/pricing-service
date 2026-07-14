from __future__ import annotations

from datetime import datetime

from infra.cron.return_scheme_pull_from_a import (
    _build_daily_task_payload,
    _is_batch_fresh,
    _resolve_b24_assignee_id,
    _resolve_b24_observer_ids,
    _resolve_b24_webhook_url,
    _upsert_daily_b24_task,
)


def _batch(*, batch_id: int = 10, amount: float = 15000.0) -> dict[str, object]:
    return {
        "id": batch_id,
        "generated_at": "2026-03-27T09:00:00",
        "new_incidents_count": 3,
        "notification_incidents_count": 3,
        "incidents": [
            {
                "id": 101,
                "store_ref": "store-1",
                "store_name": "ТЦ Москва",
                "product_ref": "prod-1",
                "product_name": "iPhone Display",
                "manager_ref": "mgr-1",
                "manager_name": "Иванов",
                "amount": amount,
                "first_sale_doc_number": "R-001",
                "first_sale_doc_datetime": "2026-03-27T08:00:00",
                "return_doc_number": "V-001",
                "return_doc_datetime": "2026-03-27T08:20:00",
                "second_sale_doc_number": "R-002",
                "repeat_store_product_7d_count": 2,
                "repeat_employee_7d_count": 2,
                "second_sale_doc_datetime": "2026-03-27T08:30:00",
            },
            {
                "id": 102,
                "store_ref": "store-2",
                "store_name": "ТЦ Питер",
                "product_ref": "prod-2",
                "product_name": "Samsung Battery",
                "manager_ref": None,
                "manager_name": None,
                "amount": 5000.0,
                "repeat_store_product_7d_count": 1,
                "repeat_employee_7d_count": 0,
                "second_sale_doc_datetime": "2026-03-27T08:45:00",
            },
        ],
    }


def test_build_daily_task_payload_aggregates_escalated_cases() -> None:
    payload = _build_daily_task_payload(_batch(), critical_amount=10000)

    assert payload is not None
    assert payload["date_key"] == "2026-03-27"
    assert payload["title"] == "[RETURN_SCHEME_ESC] Проверить возвраты за 2026-03-27: 1 строк(и)"
    assert "Incident #101" in payload["description"]
    assert "Incident #102" not in payload["description"]
    assert "Это сигнал на ручную проверку, а не готовый вывод о нарушении" in payload["description"]
    assert "Счетчики ниже считают строки товаров" in payload["description"]
    assert "по этому магазину и товару за 7 дней найдено строк: 2" in payload["description"]
    assert "по этому сотруднику за 7 дней найдено строк: 2" in payload["description"]
    assert "критичная сумма >= 10000" in payload["description"]
    assert "Почему создана задача" in payload["description"]
    assert "Что нужно сделать" in payload["description"]
    assert "Первая реализация: R-001 от 2026-03-27T08:00:00" in payload["description"]
    assert "Возврат: V-001 от 2026-03-27T08:20:00" in payload["description"]
    assert "Вторая реализация: R-002 от 2026-03-27T08:30:00" in payload["description"]


def test_batch_freshness_blocks_stale_b24_tasks() -> None:
    assert _is_batch_fresh(
        {"generated_at": "2026-03-27T09:00:00"},
        max_age_days=7,
        now=datetime(2026, 3, 30, 9, 0, 0),
    )
    assert not _is_batch_fresh(
        {"generated_at": "2026-03-27T09:00:00"},
        max_age_days=7,
        now=datetime(2026, 5, 7, 9, 0, 0),
    )


def test_return_scheme_b24_config_falls_back_to_existing_bitrix_env() -> None:
    env = {
        "EXPERTISE_BITRIX_WEBHOOK_URL": "https://bitrix.example/rest/1/token",
        "EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID": "900",
        "EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS": "[901, 902]",
    }

    assert _resolve_b24_webhook_url(env) == "https://bitrix.example/rest/1/token"
    assert _resolve_b24_assignee_id(env) == 900
    assert _resolve_b24_observer_ids(env) == [901, 902]


def test_upsert_daily_b24_task_creates_once_and_updates_same_day() -> None:
    daily_tasks_state: dict[str, dict[str, object]] = {}
    created: list[str] = []
    updated: list[tuple[int, str]] = []

    def create_task(**kwargs):
        created.append(str(kwargs["title"]))
        return {"result": 501}

    def update_task(**kwargs):
        updated.append((int(kwargs["task_id"]), str(kwargs["title"])))

    created_action = _upsert_daily_b24_task(
        batch=_batch(),
        critical_amount=10000,
        webhook_url="https://bitrix.example/rest/1/token",
        assignee_id=100,
        observer_ids=[200],
        daily_tasks_state=daily_tasks_state,
        create_task=create_task,
        update_task=update_task,
    )
    noop_action = _upsert_daily_b24_task(
        batch=_batch(),
        critical_amount=10000,
        webhook_url="https://bitrix.example/rest/1/token",
        assignee_id=100,
        observer_ids=[200],
        daily_tasks_state=daily_tasks_state,
        create_task=create_task,
        update_task=update_task,
    )
    updated_action = _upsert_daily_b24_task(
        batch=_batch(amount=22000.0),
        critical_amount=10000,
        webhook_url="https://bitrix.example/rest/1/token",
        assignee_id=100,
        observer_ids=[200],
        daily_tasks_state=daily_tasks_state,
        create_task=create_task,
        update_task=update_task,
    )

    assert created_action == "created"
    assert noop_action == "noop"
    assert updated_action == "updated"
    assert created == ["[RETURN_SCHEME_ESC] Проверить возвраты за 2026-03-27: 1 строк(и)"]
    assert updated == [(501, "[RETURN_SCHEME_ESC] Проверить возвраты за 2026-03-27: 1 строк(и)")]
    assert daily_tasks_state["2026-03-27"]["task_id"] == 501
