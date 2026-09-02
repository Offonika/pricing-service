from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.config import Settings
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationLine,
)
from app.services import procurement_order_product_rows as product_rows_service
from app.services.procurement_order_formation import serialize_linked_process


def _order(db_session, *, line_count: int = 2) -> ProcurementOrderFormation:
    order = ProcurementOrderFormation(
        stable_key=f"product-rows:{line_count}",
        status="transmitted",
        lifecycle_status="active",
        origin="onec_import",
        version=1,
        bitrix_entity_type_id=1056,
        bitrix_item_id="317",
        supplier_name="1077 MINA",
        contract_name="Основной договор",
        warehouse_name="Склад",
        currency="RMB",
        procurement_contour="cargo",
        route="cargo",
        batch_id="onec-590",
        order_date=date(2026, 9, 1),
        calculation_id="onec:590",
        onec_status="transmitted",
        onec_document_ref="0x0123456789abcdef0123456789abcdef",
        onec_document_number="РБГУ0000590",
    )
    order.lines = [
        ProcurementOrderFormationLine(
            stable_key=f"product-rows:{line_count}:{index}",
            line_number=index,
            bitrix_product_id=str(2000 + index),
            bitrix_product_xml_id=f"00000000-0000-0000-0000-{index:012d}",
            nomenclature_ref=f"00000000-0000-0000-0000-{index:012d}",
            nomenclature_code=f"РБ{index:09d}",
            nomenclature_name=f"Товар {index}",
            recommended_quantity=Decimal(index),
            final_quantity=Decimal(index),
            purchase_price=Decimal("2.5"),
            amount=Decimal(index) * Decimal("2.5"),
            currency="RMB",
        )
        for index in range(1, line_count + 1)
    ]
    db_session.add(order)
    db_session.commit()
    return order


def _readback(order: ProcurementOrderFormation) -> list[dict[str, object]]:
    return [
        {"ID": str(9000 + index), **row}
        for index, row in enumerate(
            product_rows_service.build_procurement_product_rows(order), start=1
        )
    ]


def test_product_rows_are_exact_onec_purchase_facts(db_session) -> None:
    order = _order(db_session)

    rows = product_rows_service.build_procurement_product_rows(order)

    assert rows[0] == {
        "PRODUCT_ID": 2001,
        "PRODUCT_NAME": "Товар 1",
        "PRICE": "2.5",
        "QUANTITY": "1",
        "CURRENCY_ID": "RMB",
        "SORT": 10,
    }
    assert len(product_rows_service.procurement_product_rows_checksum(rows)) == 64


def test_missing_catalog_product_keeps_link_and_records_error(db_session, monkeypatch) -> None:
    order = _order(db_session)
    order.lines[0].bitrix_product_id = None
    calls: list[str] = []
    monkeypatch.setattr(
        product_rows_service,
        "bitrix_call",
        lambda method, *_args, **_kwargs: calls.append(method),
    )

    result = product_rows_service.sync_procurement_order_product_rows(
        db_session, order, apply=True, settings=Settings()
    )
    db_session.commit()

    assert result["state"] == "error"
    assert "нет товара Bitrix" in result["error"]
    assert calls == []
    assert serialize_linked_process(order)["state"] == "linked"
    assert serialize_linked_process(order)["product_rows_sync"]["state"] == "error"
    assert any(event.event_type == "bitrix_product_rows_sync_failed" for event in order.events)


def test_sync_updates_adds_then_deletes_and_verifies_readback(db_session, monkeypatch) -> None:
    order = _order(db_session)
    desired = _readback(order)
    current = [
        {
            "ID": "7001",
            "PRODUCT_ID": 2001,
            "PRODUCT_NAME": "Старое имя",
            "PRICE": "1",
            "QUANTITY": "1",
            "CURRENCY_ID": "RMB",
            "SORT": 10,
        },
        {
            "ID": "7002",
            "PRODUCT_ID": 9999,
            "PRODUCT_NAME": "Ручная строка",
            "PRICE": "1",
            "QUANTITY": "1",
            "CURRENCY_ID": "RMB",
            "SORT": 999,
        },
    ]
    list_results = iter((current, desired))
    batch_commands: list[list[str]] = []

    def fake_call(method, params, **_kwargs):
        if method == "crm.productrow.list":
            return {"result": next(list_results)}
        assert method == "batch"
        commands = list(params["cmd"].values())
        batch_commands.append(commands)
        return {"result": {"result": {}, "result_error": {}}}

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)

    result = product_rows_service.sync_procurement_order_product_rows(
        db_session, order, apply=True, settings=Settings()
    )
    db_session.commit()

    assert result == {
        "state": "synced",
        "order_id": order.id,
        "item_id": "317",
        "expected_count": 2,
        "current_count": 2,
        "checksum": order.bitrix_product_rows_checksum,
        "add": 1,
        "update": 1,
        "delete": 1,
        "synced_count": 2,
    }
    flattened = [command for batch in batch_commands for command in batch]
    assert flattened[0].startswith("crm.productrow.add?")
    assert flattened[1].startswith("crm.productrow.update?")
    assert flattened[-1] == "crm.productrow.delete?id=7002"
    assert order.bitrix_product_rows_synced_count == 2
    assert any(event.event_type == "bitrix_product_rows_synced" for event in order.events)


def test_249_rows_are_sent_in_batches_of_at_most_50(db_session, monkeypatch) -> None:
    order = _order(db_session, line_count=249)
    desired = _readback(order)
    list_results = iter(([], desired))
    command_counts: list[int] = []

    def fake_call(method, params, **_kwargs):
        if method == "crm.productrow.list":
            return {"result": next(list_results)}
        command_counts.append(len(params["cmd"]))
        return {"result": {"result": {}, "result_error": {}}}

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)

    result = product_rows_service.sync_procurement_order_product_rows(
        db_session, order, apply=True, settings=Settings()
    )

    assert result["state"] == "synced"
    assert command_counts == [50, 50, 50, 50, 49]


def test_unchanged_rows_are_a_noop_but_still_read_back(db_session, monkeypatch) -> None:
    order = _order(db_session)
    desired = _readback(order)
    calls: list[str] = []

    def fake_call(method, _params, **_kwargs):
        calls.append(method)
        return {"result": desired}

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)

    result = product_rows_service.sync_procurement_order_product_rows(
        db_session, order, apply=True, settings=Settings()
    )

    assert result["state"] == "synced"
    assert result["add"] == result["update"] == result["delete"] == 0
    assert calls == ["crm.productrow.list", "crm.productrow.list"]
