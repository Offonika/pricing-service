from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.bitrix_order_formation import BitrixCatalogProduct
from tasks.build_procurement_order_formation_dry_run import (
    build_grouped_orders,
    build_summary,
    select_order_rows,
)


def _source(code: str, qty: str, group: str) -> dict[str, str]:
    return {
        "nomenclature_code": code,
        "name": f"Товар {code}",
        "display_group_key": group,
        "status_label": "Продажа",
        "quality_raw": "ORIG",
        "latest_purchase_price": "100",
        "recommended_order_qty": qty,
        "dry_run_decision": "order" if Decimal(qty) > 0 else "do_not_order",
        "reason_ru": "Расчётная потребность",
        "blockers": "",
        "warnings": "adaptive_lead_time_sync_ready",
    }


def _lead(code: str, supplier_code: str, supplier_ref: str) -> dict[str, str]:
    return {
        "nomenclature_code": code,
        "display_group_key": code,
        "supplier_name": f"Поставщик {supplier_code}",
        "supplier_code": supplier_code,
        "supplier_ref": supplier_ref,
        "responsible_name": "Омар",
        "lead_time_confidence": "high",
        "order_line_count": "10",
        "latest_supplier_order_at": "2026-07-01",
        "recommended_supplier_prepare_days": "10",
        "recommended_logistics_days": "20",
    }


def test_select_order_rows_keeps_only_positive_order_decisions() -> None:
    rows = [_source("A", "5", "A"), _source("B", "0", "B")]
    assert [row["nomenclature_code"] for row in select_order_rows(rows)] == ["A"]


def test_grouped_dry_run_uses_supplier_contract_warehouse_and_exact_catalog_guid() -> None:
    sources = [_source("A", "5", "A"), _source("B", "2", "B")]
    leads = [_lead("A", "S1", "0xs1"), _lead("B", "S2", "0xs2")]
    nomenclature = {
        "A": {"nomenclature_ref": "0x00010025901E48EF11E1967C11111111"},
        "B": {"nomenclature_ref": "0x00020025901E48EF11E1967C22222222"},
    }
    seen_guids: list[str] = []

    def resolve(guid: str) -> BitrixCatalogProduct:
        seen_guids.append(guid)
        return BitrixCatalogProduct(
            product_id="10" if guid.endswith("11111111") else "20",
            name="Каталожный товар",
            xml_id=guid,
            assortment_status="Продажа",
        )

    orders = build_grouped_orders(
        sources,
        leads,
        nomenclature_by_code=nomenclature,
        catalog_resolver=resolve,
        skip_catalog=False,
        contracts={"default": {"code": "C1", "name": "Основной договор"}},
        warehouse={"code": "MAIN", "name": "Центральный склад"},
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-07-10",
        order_date=date(2026, 7, 10),
        calculation_id="calc-1",
    )

    assert len(orders) == 2
    assert {order["supplier"]["code"] for order in orders} == {"S1", "S2"}
    assert all(order["contract"]["code"] == "C1" for order in orders)
    assert all(order["warehouse"]["code"] == "MAIN" for order in orders)
    assert len(seen_guids) == 2
    assert all(line["blockers"] == [] for order in orders for line in order["lines"])
    summary = build_summary(source_rows=sources, selected_rows=sources, orders=orders)
    assert summary["catalog_matched_line_count"] == 2
    assert summary["blocking_line_count"] == 0


def test_missing_catalog_product_is_a_hard_blocker() -> None:
    orders = build_grouped_orders(
        [_source("A", "5", "A")],
        [_lead("A", "S1", "0xs1")],
        nomenclature_by_code={"A": {"nomenclature_ref": "0x00010025901E48EF11E1967C11111111"}},
        catalog_resolver=lambda _guid: None,
        skip_catalog=False,
        contracts={"default": {"code": "C1", "name": "Договор"}},
        warehouse={"code": "MAIN", "name": "Склад"},
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="batch",
        order_date=date(2026, 7, 10),
        calculation_id="calc",
    )
    assert orders[0]["lines"][0]["blockers"] == ["catalog_product_missing"]
