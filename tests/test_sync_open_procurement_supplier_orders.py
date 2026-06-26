from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from scripts import sync_open_cargo_supplier_orders_to_bitrix as sync


def _row(contour_enum_order: int) -> dict[str, object]:
    return {
        "onec_ref": "0x010203",
        "number": "РБГУ000377",
        "order_date": datetime(2026, 6, 19, 11, 37, 30),
        "posted": 1,
        "supplier_ref": "0x0a0b0c",
        "supplier_name": "2312 Huarigor battery",
        "contract_name": "Основной договор",
        "store_name": "Сдзк Склад",
        "currency_name": "RMB",
        "open_qty": Decimal("4640.000"),
        "open_amount": Decimal("243200.00"),
        "open_amount_rub": Decimal("0.00"),
        "open_line_count": 29,
        "supplier_dispatch_date": datetime(2026, 6, 22),
        "cargo_dropoff_date": datetime(1753, 1, 1),
        "expected_receipt_date": datetime(1753, 1, 1),
        "payment_date": datetime(2026, 6, 19),
        "comment": "+++заказано турал",
        "contour_enum_order": contour_enum_order,
    }


def test_parse_contour_keys_accepts_ved_aliases() -> None:
    assert sync.parse_contour_keys("cargo, ВЭД импорт, ved-import") == {
        "cargo",
        "ved_import",
    }


def test_open_supplier_order_row_includes_ved_import() -> None:
    order = sync.order_from_open_supplier_order_row(
        _row(2),
        allowed_contours={"cargo", "ved_import"},
    )

    assert order is not None
    assert order["КонтурЗакупки"] == "ВЭДИмпорт"
    assert order["procurement_contour_key"] == "ved_import"
    assert order["procurement_stage_key"] == "docs_collection"
    assert order["title"] == "ВЭД импорт · РБГУ000377 · 243 200 RMB · 2312 Huarigor battery"
    assert order["cargo_dropoff_date"] == ""
    assert order["expected_receipt_date"] == ""


def test_open_supplier_order_row_can_skip_ved_when_filter_is_cargo_only() -> None:
    order = sync.order_from_open_supplier_order_row(_row(2), allowed_contours={"cargo"})

    assert order is None


def test_open_supplier_order_row_keeps_cargo_behavior() -> None:
    order = sync.order_from_open_supplier_order_row(_row(1), allowed_contours={"cargo"})

    assert order is not None
    assert order["procurement_contour_key"] == "cargo"
    assert order["title"].startswith("Cargo · РБГУ000377")
