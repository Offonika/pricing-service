from __future__ import annotations

from datetime import date, datetime

import pytest

import app.services.bitrix_order_formation as bitrix_order_service
from app.models.procurement_order_formation import ProcurementOrderFormation
from app.services.bitrix_order_formation import create_or_update_bitrix_card
from app.services.procurement_order_formation import serialize_order
from app.services.procurement_order_process_link import (
    ProcurementProcessCardSnapshot,
    reconcile_procurement_order_process_links,
)
from app.services.procurement_order_registry import upsert_onec_order_snapshot

ONEC_REF = "0x0123456789abcdef0123456789abcdef"


def _order(db_session) -> ProcurementOrderFormation:
    result = upsert_onec_order_snapshot(
        db_session,
        {
            "onec_ref": ONEC_REF,
            "number": "РБГУ0000595",
            "order_date": date(2026, 9, 1),
            "posted": True,
            "supplier": {"title": "Поставщик"},
            "contract_name": "Договор",
            "store_name": "Склад",
            "currency": "RUB",
            "ordered_qty": 1,
            "open_qty": 1,
            "lines": [],
        },
    )
    db_session.flush()
    order = db_session.get(ProcurementOrderFormation, result.order_id)
    assert order is not None
    return order


def _card(**overrides) -> ProcurementProcessCardSnapshot:
    values = {
        "item_id": "324",
        "onec_ref": ONEC_REF,
        "onec_number": "РБГУ0000595",
        "onec_date": date(2026, 9, 1),
        "category_id": 53,
        "stage_id": "DT1056_53:PAYREQ",
        "stage_name": "Заявка на оплату / оплата в работе",
    }
    values.update(overrides)
    return ProcurementProcessCardSnapshot(**values)


def test_reconciliation_links_canonical_process_and_writes_audit(db_session) -> None:
    order = _order(db_session)

    summary = reconcile_procurement_order_process_links(
        db_session,
        [_card()],
        checked_at=datetime(2026, 9, 1, 20, 0),
    )

    assert summary == {"checked": 1, "linked": 1, "unchanged": 0, "broken": 0}
    assert order.bitrix_entity_type_id == 1056
    assert order.bitrix_item_id == "324"
    assert order.bitrix_category_id == 53
    assert order.bitrix_stage_id == "DT1056_53:PAYREQ"
    assert order.bitrix_stage_name == "Заявка на оплату / оплата в работе"
    assert order.bitrix_item_url is None
    assert order.bitrix_link_error is None
    assert any(event.event_type == "bitrix_process_linked" for event in order.events)
    linked = serialize_order(order)["linked_process"]
    assert linked["state"] == "linked"
    assert linked["process_title"] == "Закупка/Заказ"
    assert linked["category_name"] == "Карго"


def test_reconciliation_rejects_number_mismatch(db_session) -> None:
    order = _order(db_session)

    summary = reconcile_procurement_order_process_links(
        db_session,
        [_card(onec_number="РБГУ0000999")],
    )

    assert summary["broken"] == 1
    assert order.bitrix_entity_type_id is None
    assert "номер документа не совпадает" in (order.bitrix_link_error or "")
    assert serialize_order(order)["linked_process"]["state"] == "broken"
    assert any(event.event_type == "bitrix_process_link_failed" for event in order.events)


def test_reconciliation_rejects_duplicate_process_cards(db_session) -> None:
    order = _order(db_session)

    summary = reconcile_procurement_order_process_links(
        db_session,
        [_card(), _card(item_id="325")],
    )

    assert summary["broken"] == 1
    assert "несколько карточек" in (order.bitrix_link_error or "")


def test_draft_without_onec_document_has_not_created_process_state(db_session) -> None:
    order = _order(db_session)
    order.onec_document_ref = None
    order.onec_document_number = None

    assert serialize_order(order)["linked_process"]["state"] == "not_created"


def _legacy_mapping() -> dict:
    return {
        "process": {"entity_type_id": 1136, "category_id": 58},
        "fields": {},
        "stage_map": {},
    }


def test_legacy_process_rejects_new_cards(db_session, monkeypatch) -> None:
    order = _order(db_session)
    calls: list[str] = []
    monkeypatch.setattr(
        bitrix_order_service, "load_order_formation_mapping", lambda settings: _legacy_mapping()
    )
    monkeypatch.setattr(
        bitrix_order_service,
        "bitrix_call",
        lambda method, params, *, settings: calls.append(method),
    )

    with pytest.raises(RuntimeError, match="does not accept new links"):
        create_or_update_bitrix_card(db_session, order.id, apply=True)

    assert calls == []


def test_legacy_process_keeps_existing_card_compatible(db_session, monkeypatch) -> None:
    order = _order(db_session)
    order.bitrix_entity_type_id = 1136
    order.bitrix_item_id = "7001"
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bitrix_order_service, "load_order_formation_mapping", lambda settings: _legacy_mapping()
    )
    monkeypatch.setattr(
        bitrix_order_service,
        "bitrix_call",
        lambda method, params, *, settings: calls.append((method, params)) or {"result": {}},
    )
    monkeypatch.setattr(
        bitrix_order_service, "sync_bitrix_product_rows", lambda *args, **kwargs: None
    )

    result = create_or_update_bitrix_card(db_session, order.id, apply=True)

    assert result["item_id"] == "7001"
    assert calls[0][0] == "crm.item.update"
    assert calls[0][1]["entityTypeId"] == 1136


def test_legacy_mapping_never_updates_canonical_link(db_session, monkeypatch) -> None:
    order = _order(db_session)
    order.bitrix_entity_type_id = 1056
    order.bitrix_item_id = "324"
    calls: list[str] = []
    monkeypatch.setattr(
        bitrix_order_service, "load_order_formation_mapping", lambda settings: _legacy_mapping()
    )
    monkeypatch.setattr(
        bitrix_order_service,
        "bitrix_call",
        lambda method, params, *, settings: calls.append(method),
    )

    with pytest.raises(RuntimeError, match="does not accept new links"):
        create_or_update_bitrix_card(db_session, order.id, apply=True)

    assert calls == []
