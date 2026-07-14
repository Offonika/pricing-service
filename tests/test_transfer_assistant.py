from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api import logistics as logistics_api
from app.core.config import get_settings
from app.main import app
from app.services import transfer_assistant

AS_OF = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _row(**overrides):
    value = {
        "product_ref": "0xPRODUCT1",
        "product_code": "P-1",
        "product_name": "Display iPhone",
        "warehouse_ref": "0xWH1",
        "warehouse_code": "WH-1",
        "warehouse_name": "Main warehouse",
        "quantity": Decimal("1"),
        "fact_date": AS_OF,
        "data_source": "test",
    }
    value.update(overrides)
    return value


def _statuses(*rows):
    return [
        item["status"]
        for item in transfer_assistant.list_transfer_assistant_candidates(
            source_rows=rows,
            as_of=AS_OF,
        )
    ]


def test_transfer_assistant_classifies_core_statuses() -> None:
    future = AS_OF + timedelta(days=1)
    past = AS_OF - timedelta(days=1)

    rows = [
        _row(stock_quantity=Decimal("5"), quantity=Decimal("5")),
        _row(
            stock_quantity=Decimal("5"),
            reserved_quantity=Decimal("2"),
            order_ref="0xORDER1",
            order_number="RB-1",
        ),
        _row(
            reserved_quantity=Decimal("1"),
            order_ref="0xORDER2",
            delivery_method="Самовывоз",
            pickup_deadline=future,
        ),
        _row(
            reserved_quantity=Decimal("1"),
            order_ref="0xORDER3",
            delivery_method="Самовывоз",
            pickup_deadline=past,
        ),
        _row(
            reserved_quantity=Decimal("1"),
            order_ref="0xORDER4",
            delivery_method="Самовывоз",
            pickup_deadline=past,
            needs_dismantling=True,
        ),
        _row(has_issue_document=True, has_return_document=True),
    ]

    assert set(_statuses(*rows)) == {
        "available_to_transfer",
        "reserved_for_order",
        "pickup_waiting",
        "pickup_expired",
        "dismantling_needed",
        "manual_review",
    }


def test_transfer_assistant_reserve_is_not_available_stock() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                stock_quantity=Decimal("5"),
                reserved_quantity=Decimal("2"),
                order_ref="0xORDER1",
            )
        ],
        as_of=AS_OF,
    )

    assert len(items) == 1
    assert items[0]["status"] == "reserved_for_order"
    assert items[0]["status"] != "available_to_transfer"


def test_transfer_assistant_reserved_stock_register_row_keeps_order_key() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                data_source="1c:reserved_stock_totals",
                source_document_type="reserve_register",
                source_document_ref="0xORDER1",
                source_document_number="RB-1",
                reserved_quantity=Decimal("2"),
                order_ref="0xORDER1",
                order_number="RB-1",
            )
        ],
        as_of=AS_OF,
    )

    assert items[0]["status"] == "reserved_for_order"
    assert items[0]["data_source"] == "1c:reserved_stock_totals"
    assert items[0]["onec_document_keys"]["order_ref"] == "0xORDER1"
    assert items[0]["onec_document_keys"]["reserve_register_ref"] == "0xORDER1"


def test_transfer_assistant_placement_row_keeps_supplier_order_key() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                data_source="1c:customer_order_placements",
                source_document_type="supplier_order",
                source_document_ref="0xSUPPLIER_ORDER1",
                source_document_number="RB-S-1",
                placement_quantity=Decimal("3"),
                order_ref="0xORDER1",
                order_number="RB-1",
            )
        ],
        as_of=AS_OF,
    )

    assert items[0]["status"] == "reserved_for_order"
    assert items[0]["data_source"] == "1c:customer_order_placements"
    assert items[0]["onec_document_keys"]["order_ref"] == "0xORDER1"
    assert items[0]["onec_document_keys"]["supplier_order_ref"] == "0xSUPPLIER_ORDER1"


def test_transfer_assistant_expired_pickup_does_not_release_stock() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                reserved_quantity=Decimal("1"),
                order_ref="0xORDER1",
                delivery_method="Самовывоз",
                pickup_deadline=AS_OF - timedelta(days=1),
            )
        ],
        as_of=AS_OF,
    )

    assert len(items) == 1
    assert items[0]["status"] == "pickup_expired"
    assert items[0]["quantity"] == Decimal("1")


def test_transfer_assistant_derives_pickup_deadline_when_onec_fact_is_missing() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                fact_date=AS_OF - timedelta(days=8),
                reserved_quantity=Decimal("1"),
                order_ref="0xORDER1",
                delivery_method="Самовывоз",
            )
        ],
        as_of=AS_OF,
    )

    assert items[0]["status"] == "pickup_expired"
    assert items[0]["pickup_deadline"] == AS_OF - timedelta(days=1)
    assert items[0]["pickup_deadline_source"] == "derived"


def test_transfer_assistant_rtu_is_only_queue_candidate_for_pickup() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                source_document_type="rtu",
                has_issue_document=True,
                order_ref="0xDELIVERY_ORDER",
                delivery_method="Доставка",
            ),
            _row(
                source_document_type="rtu",
                has_issue_document=True,
                order_ref="0xPICKUP_ORDER",
                delivery_method="Самовывоз",
            ),
        ],
        as_of=AS_OF,
    )

    assert len(items) == 1
    assert items[0]["status"] == "pickup_waiting"
    assert items[0]["order"]["ref"] == "0xPICKUP_ORDER"


def test_transfer_assistant_closing_document_removes_expired_pickup() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                reserved_quantity=Decimal("1"),
                order_ref="0xORDER1",
                delivery_method="Самовывоз",
                pickup_deadline=AS_OF - timedelta(days=1),
                has_closing_document=True,
            )
        ],
        as_of=AS_OF,
    )

    assert items == []


def test_transfer_assistant_manual_review_for_missing_order() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[_row(reserved_quantity=Decimal("1"))],
        as_of=AS_OF,
    )

    assert items[0]["status"] == "manual_review"
    assert items[0]["reason"] == "customer order was not found"


def test_transfer_assistant_source_sql_reads_core_onec_facts() -> None:
    statement, _ = transfer_assistant._transfer_assistant_source_statement(
        date_from=None,
        date_to=None,
        warehouse_id=None,
        limit=10,
    )

    sql = str(statement)
    assert "_AccumRgT7745" in sql
    assert "_AccumRgT7662" in sql
    assert "reserve._Fld7655RRef" in sql
    assert "order_reserve._Fld7657_RRRef = customer_order._IDRRef" in sql
    assert "_AccumRgT7606" in sql
    assert "placement._Fld7600_RRRef" in sql
    assert "placement._Fld7601_RRRef" in sql
    assert "supplier_order._Fld2506RRef" in sql
    assert "_Document132_VT2427" in sql
    assert "order_line._Fld2434RRef" in sql
    assert "_Document203_VT4966" in sql
    assert "_Document109_VT1698" in sql
    assert "_Document178_VT3822" in sql
    assert "_Document135_VT2569" in sql


def test_transfer_assistant_status_routes_to_minimal_onec_sources() -> None:
    assert transfer_assistant._source_kinds_for_status("available_to_transfer") == {"stock"}
    assert transfer_assistant._source_kinds_for_status("reserved_for_order") == {
        "reserve",
        "placement",
        "order",
    }
    assert transfer_assistant._source_kinds_for_status("pickup_expired") == {
        "reserve",
        "placement",
        "order",
        "rtu",
    }
    assert transfer_assistant._source_kinds_for_status("manual_review") == {
        "reserve",
        "placement",
        "rtu",
        "return",
        "transfer",
    }
    assert transfer_assistant._source_kinds_for_status(None) is None


def test_transfer_assistant_source_sql_can_disable_unused_onec_branches() -> None:
    statement, _ = transfer_assistant._transfer_assistant_source_statement(
        date_from=None,
        date_to=None,
        warehouse_id=None,
        limit=10,
        source_kinds={"stock"},
    )

    sql = str(statement)
    assert sql.count("AND 1 = 0") == 6


def test_transfer_assistant_status_filter_fetches_wider_window(monkeypatch) -> None:
    captured = {}

    def fake_fetch(**kwargs):
        captured.update(kwargs)
        return [_row(stock_quantity=Decimal("2"), quantity=Decimal("2"))]

    monkeypatch.setattr(
        transfer_assistant,
        "fetch_transfer_assistant_source_rows",
        fake_fetch,
    )

    items = transfer_assistant.list_transfer_assistant_candidates(
        status="available_to_transfer",
        warehouse_id="WH-1",
        limit=5,
        as_of=AS_OF,
    )

    assert captured["limit"] == 1000
    assert captured["source_kinds"] == {"stock"}
    assert len(items) == 1


def test_transfer_assistant_available_status_requires_warehouse() -> None:
    try:
        transfer_assistant.list_transfer_assistant_candidates(
            status="available_to_transfer",
            source_rows=[],
        )
    except ValueError as exc:
        assert str(exc) == "available_to_transfer requires warehouse_id in v1"
    else:
        raise AssertionError("expected warehouse guard for available_to_transfer")


def test_transfer_assistant_return_and_transfer_rows_need_manual_review() -> None:
    items = transfer_assistant.list_transfer_assistant_candidates(
        source_rows=[
            _row(
                source_document_type="customer_return",
                source_document_ref="0xRETURN1",
                return_quantity=Decimal("1"),
                manual_review_reason="customer return requires source order check",
            ),
            _row(
                source_document_type="transfer",
                source_document_ref="0xTRANSFER1",
                manual_review_reason="transfer document requires logistics state check",
            ),
        ],
        as_of=AS_OF,
    )

    assert [item["status"] for item in items] == ["manual_review", "manual_review"]


def test_transfer_assistant_api_requires_token_and_returns_filtered_schema(monkeypatch) -> None:
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", "logistics-token")
    get_settings.cache_clear()

    def fake_candidates(**kwargs):
        assert kwargs["status"] == "available_to_transfer"
        assert kwargs["warehouse_id"] == "WH-1"
        assert kwargs["limit"] == 10
        return [
            {
                "product": {"ref": "0xPRODUCT1", "code": "P-1", "name": "Display iPhone"},
                "warehouse": {"ref": "0xWH1", "code": "WH-1", "name": "Main warehouse"},
                "order": None,
                "source_document": None,
                "quantity": Decimal("3"),
                "status": "available_to_transfer",
                "reason": "stock has no reserve, placement, or blocking order",
                "onec_document_keys": {"product_ref": "0xPRODUCT1", "warehouse_ref": "0xWH1"},
                "fact_date": AS_OF,
                "data_source": "test",
                "measures": {
                    "stock_quantity": Decimal("3"),
                    "reserved_quantity": Decimal("0"),
                    "placement_quantity": Decimal("0"),
                    "order_quantity": Decimal("0"),
                },
                "pickup_deadline": None,
                "pickup_deadline_source": None,
            }
        ]

    monkeypatch.setattr(
        logistics_api.transfer_assistant_service,
        "list_transfer_assistant_candidates",
        fake_candidates,
    )

    client = TestClient(app)
    assert client.get("/api/logistics/transfer-assistant/candidates").status_code == 401

    response = client.get(
        "/api/logistics/transfer-assistant/candidates",
        params={"status": "available_to_transfer", "warehouse_id": "WH-1", "limit": 10},
        headers={"Authorization": "Bearer logistics-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body[0]["status"] == "available_to_transfer"
    assert body[0]["product"]["code"] == "P-1"
    assert body[0]["warehouse"]["code"] == "WH-1"
    assert body[0]["data_source"] == "test"
