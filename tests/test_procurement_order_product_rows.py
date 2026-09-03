from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

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
    result = []
    for index, row in enumerate(
        product_rows_service.build_procurement_product_rows(order), start=1
    ):
        api_row = {key: value for key, value in row.items() if key != "CURRENCY_ID"}
        result.append({"ID": str(9000 + index), **api_row})
    return result


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


def test_dynamic_product_row_owner_type_uses_bitrix_hex_abbreviation() -> None:
    assert product_rows_service.dynamic_product_row_owner_type(1056) == "T420"
    assert product_rows_service.PRODUCT_ROW_OWNER_TYPE == "T420"


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


def test_configured_consumable_is_excluded_and_audited(db_session, monkeypatch, tmp_path) -> None:
    order = _order(db_session)
    excluded_line = order.lines[0]
    excluded_line.bitrix_product_id = None
    config_path = tmp_path / "product-row-exclusions.json"
    config_path.write_text(
        json.dumps(
            {
                "exclusions": [
                    {
                        "nomenclature_ref": excluded_line.nomenclature_ref,
                        "reason": "household_consumable",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(procurement_product_row_exclusions_path=str(config_path))
    exclusions = product_rows_service.load_procurement_product_row_exclusions(settings)
    desired = []
    for index, row in enumerate(
        product_rows_service.build_procurement_product_rows(order, exclusions=exclusions),
        start=1,
    ):
        desired.append(
            {
                "ID": str(9000 + index),
                **{key: value for key, value in row.items() if key != "CURRENCY_ID"},
            }
        )
    list_results = iter(([], desired))

    def fake_call(method, _params, **_kwargs):
        if method == "crm.item.get":
            return {"result": {"item": {"currencyId": "RMB"}}}
        if method == "crm.productrow.list":
            return {"result": next(list_results)}
        if method == "batch":
            return {"result": {"result": {}, "result_error": {}}}
        raise AssertionError(method)

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)

    result = product_rows_service.sync_procurement_order_product_rows(
        db_session, order, apply=True, settings=settings
    )
    db_session.commit()

    assert result["state"] == "synced"
    assert result["expected_count"] == 1
    assert result["excluded_count"] == 1
    event = next(
        event for event in order.events if event.event_type == "bitrix_product_rows_synced"
    )
    assert event.payload["excluded_lines"] == [
        {
            "line_number": 1,
            "nomenclature_ref": excluded_line.nomenclature_ref,
            "reason": "household_consumable",
        }
    ]


def test_versioned_consumable_exclusion_list_contains_twenty_unique_guids() -> None:
    exclusions = product_rows_service.load_procurement_product_row_exclusions(Settings())

    assert len(exclusions) == 20
    assert set(exclusions.values()) == {"household_consumable"}


def test_linked_process_product_view_hides_exclusions_and_keeps_source_order(
    db_session,
) -> None:
    order = _order(db_session)
    exclusions = product_rows_service.load_procurement_product_row_exclusions(Settings())
    excluded_ref = next(iter(exclusions))
    order.lines[0].nomenclature_ref = excluded_ref
    order.lines[0].bitrix_product_xml_id = excluded_ref
    order.lines[0].nomenclature_name = "Скотч хозяйственный"

    linked_process = serialize_linked_process(order)
    product_sync = linked_process["product_rows_sync"]

    assert product_sync["excluded_count"] == 1
    assert [row["name"] for row in product_sync["rows"]] == ["Товар 2"]
    assert product_sync["rows"][0] == {
        "line_number": 2,
        "product_id": "2002",
        "name": "Товар 2",
        "quantity": Decimal("2"),
        "purchase_price": Decimal("2.5"),
        "currency": "RMB",
        "sort": 20,
        "catalog_matched": True,
    }
    assert len([line for line in order.lines if not line.removed]) == 2


def test_product_view_marks_other_unmatched_goods_for_diagnostics(db_session) -> None:
    order = _order(db_session, line_count=1)
    order.lines[0].bitrix_product_id = None

    view = product_rows_service.build_procurement_product_rows_view(order, exclusions={})

    assert view["excluded_count"] == 0
    assert view["rows"][0]["name"] == "Товар 1"
    assert view["rows"][0]["catalog_matched"] is False


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
        if method == "crm.item.get":
            return {"result": {"item": {"currencyId": "RMB"}}}
        if method == "crm.productrow.list":
            assert params["filter"]["OWNER_TYPE"] == "T420"
            return {"result": next(list_results)}
        assert method == "batch"
        assert _kwargs["timeout_seconds"] >= 120
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
        "currency_id": "RMB",
        "currency_update": False,
        "synced_count": 2,
    }
    flattened = [command for batch in batch_commands for command in batch]
    assert flattened[0].startswith("crm.productrow.add?")
    assert "fields%5BOWNER_TYPE%5D=T420" in flattened[0]
    assert all("CURRENCY_ID" not in command for command in flattened)
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
        if method == "crm.item.get":
            return {"result": {"item": {"currencyId": "RMB"}}}
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
        if method == "crm.item.get":
            return {"result": {"item": {"currencyId": "RMB"}}}
        return {"result": desired}

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)

    result = product_rows_service.sync_procurement_order_product_rows(
        db_session, order, apply=True, settings=Settings()
    )

    assert result["state"] == "synced"
    assert result["add"] == result["update"] == result["delete"] == 0
    assert calls == [
        "crm.item.get",
        "crm.productrow.list",
        "crm.productrow.list",
        "crm.item.get",
    ]


def test_product_row_list_reads_all_bitrix_pages(monkeypatch) -> None:
    starts: list[int] = []

    def fake_call(method, params, **_kwargs):
        assert method == "crm.productrow.list"
        starts.append(params["start"])
        start = params["start"]
        rows = [{"ID": str(index)} for index in range(start + 1, start + 51)]
        if start == 0:
            return {"result": rows, "next": 50, "total": 75}
        return {"result": rows[:25], "total": 75}

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)

    rows = product_rows_service.list_procurement_product_rows(item_id="317", settings=Settings())

    assert len(rows) == 75
    assert starts == [0, 50]


@pytest.mark.parametrize(
    "error_message",
    (
        "Bitrix API crm.productrow.list is unavailable",
        "Bitrix API crm.productrow.list: HTTP 500 Internal Server Error",
    ),
)
def test_product_row_list_retries_transient_read_failure(monkeypatch, error_message) -> None:
    calls = 0
    sleeps: list[int] = []

    def fake_call(method, params, **_kwargs):
        nonlocal calls
        assert method == "crm.productrow.list"
        calls += 1
        if calls == 1:
            raise RuntimeError(error_message)
        return {"result": [{"ID": "1"}], "total": 1}

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)
    monkeypatch.setattr(product_rows_service.time, "sleep", sleeps.append)

    rows = product_rows_service.list_procurement_product_rows(item_id="317", settings=Settings())

    assert rows == [{"ID": "1"}]
    assert calls == 2
    assert sleeps == [1]


def test_currency_is_updated_on_process_card_not_product_row(db_session, monkeypatch) -> None:
    order = _order(db_session)
    desired = _readback(order)
    currencies = iter(("RUB", "RMB"))
    calls: list[tuple[str, dict]] = []

    def fake_call(method, params, **_kwargs):
        calls.append((method, params))
        if method == "crm.item.get":
            return {"result": {"item": {"currencyId": next(currencies)}}}
        if method == "crm.item.update":
            return {"result": {"item": {"id": 317}}}
        if method == "crm.productrow.list":
            return {"result": desired}
        raise AssertionError(method)

    monkeypatch.setattr(product_rows_service, "bitrix_call", fake_call)

    result = product_rows_service.sync_procurement_order_product_rows(
        db_session, order, apply=True, settings=Settings()
    )

    assert result["state"] == "synced"
    assert result["currency_update"] is True
    update = next(params for method, params in calls if method == "crm.item.update")
    assert update["fields"] == {"currencyId": "RMB"}
