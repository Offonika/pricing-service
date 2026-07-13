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


def test_parse_args_accepts_blank_contour_cargo_dropoff_filter() -> None:
    args = sync.parse_args(["--blank-contour-cargo-dropoff-only"])

    assert args.blank_contour_cargo_dropoff_only is True


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
    assert order["title"].startswith("Карго · РБГУ000377")


def test_open_supplier_order_row_routes_blank_contour_with_cargo_date_to_cargo() -> None:
    row = {
        **_row(None),
        "currency_name": "руб.",
        "cargo_dropoff_date": datetime(2026, 6, 23),
        "payment_date": datetime(1753, 1, 1),
        "contour_enum_order": None,
    }

    order = sync.order_from_open_supplier_order_row(row, allowed_contours={"cargo"})

    assert order is not None
    assert order["КонтурЗакупки"] == ""
    assert order["procurement_contour_key"] == "cargo"
    assert order["cargo_dropoff_date"] == "2026-06-23T00:00:00"
    assert order["procurement_stage_key"] == "cargo_dropoff"


def test_run_bitrix_import_passes_finance_user_and_reuses_batch_ids(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    monkeypatch.setattr(sync, "BitrixRestApi", lambda _webhook_base: object())
    monkeypatch.setattr(sync, "CachedBitrixApi", lambda api: api)
    monkeypatch.setattr(sync, "list_existing_procurement_items", lambda _api, _mapping: [])
    monkeypatch.setattr(
        sync,
        "prefetched_procurement_item_id",
        lambda _items, _order, _mapping: "",
    )
    monkeypatch.setattr(
        sync,
        "sync_supplier_to_crm",
        lambda *_args, **_kwargs: {
            "status": "resolved_existing_company",
            "resolution_status_key": "resolved_existing",
        },
    )

    def fake_import_order(_api, order, **kwargs):
        captured.append(kwargs)
        return {"source_number": order["number"], "action": "dry_run_update_or_create"}

    monkeypatch.setattr(sync, "import_order", fake_import_order)

    rows = sync.run_bitrix_import(
        [
            {"number": "РБГУ000377", "supplier": {"title": "Supplier"}},
            {"number": "РБГУ000378", "supplier": {"title": "Supplier"}},
        ],
        webhook_base="https://bitrix.example/rest/1/token",
        mapping={},
        apply=False,
        assigned_by_id="130750",
        finance_user_id="42",
    )

    assert len(rows) == 2
    assert [item["finance_user_id"] for item in captured] == ["42", "42"]
    assert captured[0]["used_batch_ids"] is captured[1]["used_batch_ids"]
