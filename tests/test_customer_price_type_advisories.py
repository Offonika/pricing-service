from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.customer_price_types import require_customer_price_type_access
from app.domains.customer_price_types.advisories import (
    build_customer_notification_draft,
    build_order_return_lamp,
)
from app.domains.customer_price_types.entities import CustomerPriceTypeAccessScope
from app.main import app
from scripts.build_customer_return_lamp_shadow import build_shadow


@pytest.mark.parametrize(
    ("character", "expected_key", "expected_severity"),
    (
        ("сверхнормативные возвраты (мозга-шпиль)", "critical_returns", "critical"),
        ("повышенные возвраты новым (подбор запчасти)", "parts_fitting_returns", "warning"),
        ("разовая сделка (купил-вернул, не характер)", "one_off_return", "info"),
    ),
)
def test_return_character_becomes_non_blocking_lamp(
    character: str, expected_key: str, expected_severity: str
) -> None:
    lamp = build_order_return_lamp(character=character)
    assert lamp.key == expected_key
    assert lamp.severity == expected_severity
    assert lamp.visible is True
    assert lamp.blocks_fulfillment is False


def test_service_card_has_no_customer_lamp() -> None:
    lamp = build_order_return_lamp(character="вне клиентского контура (служебный инструмент)")
    assert lamp.visible is False
    assert lamp.blocks_fulfillment is False


def test_generic_behavior_group_without_confirmed_character_has_no_lamp() -> None:
    lamp = build_order_return_lamp(character="", behavior_group="critical_returns")
    assert lamp.visible is False


def test_period_mismatch_requires_review_without_blocking() -> None:
    lamp = build_order_return_lamp(
        character="",
        period_mismatch="возврат без продаж в окне - сверка",
    )
    assert lamp.key == "return_period_review"
    assert lamp.blocks_fulfillment is False


@pytest.mark.parametrize("event", ("presignal", "price_type_changed", "recovery"))
def test_notification_is_always_an_unapproved_shadow_draft(event: str) -> None:
    draft = build_customer_notification_draft(event, current_level="2.Бронзовый")
    assert draft.text
    assert draft.approval_status == "requires_approval"
    assert draft.send_allowed is False
    assert "sms" in draft.channel_candidates


def test_shadow_builder_writes_only_visible_non_blocking_lamps(tmp_path: Path) -> None:
    source = tmp_path / "portrait.csv"
    output = tmp_path / "lamps.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "код_1с",
                "контрагент",
                "характер",
                "несоответствие_периодов",
                "группа_поведения",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "код_1с": "РБ1",
                "контрагент": "Клиент",
                "характер": "сверхнормативные возвраты (мозга-шпиль)",
                "несоответствие_периодов": "",
                "группа_поведения": "critical_returns",
            }
        )
        writer.writerow(
            {
                "код_1с": "РБ2",
                "контрагент": "Служебная карточка",
                "характер": "вне клиентского контура (служебный инструмент)",
                "несоответствие_периодов": "",
                "группа_поведения": "critical_returns",
            }
        )

    counts = build_shadow(source, output)
    rows = list(csv.DictReader(output.open("r", encoding="utf-8-sig", newline="")))

    assert counts["critical_returns"] == 1
    assert counts["no_return_signal"] == 1
    assert len(rows) == 1
    assert rows[0]["blocks_fulfillment"] == "false"


def test_shadow_advisory_api_never_allows_send_or_onec_write() -> None:
    access = CustomerPriceTypeAccessScope(actor="test", role="internal")
    app.dependency_overrides[require_customer_price_type_access] = lambda: access
    try:
        response = TestClient(app).post(
            "/api/customer-price-types/advisories/preview",
            json={
                "return_character": "сверхнормативные возвраты (мозга-шпиль)",
                "notification_event": "presignal",
                "current_level": "2.Бронзовый",
            },
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "shadow"
    assert payload["onec_write_allowed"] is False
    assert payload["order_lamp"]["blocks_fulfillment"] is False
    assert payload["notification"]["send_allowed"] is False
