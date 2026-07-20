from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.procurement_order_formation import ProcurementOrderFormation
from app.services.bitrix_order_formation import BitrixCatalogProduct
from app.services.procurement_order_formation import serialize_line
from tasks.build_procurement_order_formation_dry_run import (
    build_grouped_orders,
    build_summary,
    persist_grouped_orders,
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


def test_grouped_dry_run_carries_b2b_advisory_without_changing_order_quantity(
    db_session,
) -> None:
    source = _source("A", "5", "A")
    source.update(
        {
            "b2b_profile_as_of_exclusive": "2026-07-10",
            "b2b_profile_age_days": "0",
            "b2b_demand_mode": "advisory_only",
            "b2b_dependency_class": "Клиентский спрос 3/4/5 преобладает",
            "b2b_active_customer_count": "2",
            "b2b_passive_customer_count": "1",
            "b2b_due_customer_count": "1",
            "b2b_managed_sales_qty_window": "12",
            "b2b_active_daily_rate": "0.06667",
            "b2b_client_forecast_qty": "6",
            "b2b_ordinary_net_sales_qty_window": "3",
            "b2b_replacement_target_stock_qty": "9",
            "b2b_replacement_decision": "order",
            "b2b_replacement_recommended_order_qty": "7",
            "b2b_order_delta_qty": "2",
            "b2b_reason_ru": "Отдельный клиентский прогноз; основной заказ не изменён.",
        }
    )
    orders = build_grouped_orders(
        [source],
        [_lead("A", "S1", "0xs1")],
        nomenclature_by_code={"A": {"nomenclature_ref": "0x00010025901E48EF11E1967C11111111"}},
        catalog_resolver=lambda guid: BitrixCatalogProduct(
            product_id="10",
            name="Каталожный товар",
            xml_id=guid,
            assortment_status="Продажа",
        ),
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

    line = orders[0]["lines"][0]
    assert line["recommended_quantity"] == "5"
    assert line["final_quantity"] == "5"
    assert line["payload"]["b2b_customer_demand"] == {
        "mode": "advisory_only",
        "profile_as_of_exclusive": "2026-07-10",
        "profile_age_days": 0,
        "dependency_class": "Клиентский спрос 3/4/5 преобладает",
        "active_customer_count": 2,
        "passive_customer_count": 1,
        "due_customer_count": 1,
        "managed_sales_qty_window": "12",
        "active_daily_rate": "0.06667",
        "client_forecast_qty": "6",
        "ordinary_net_sales_qty_window": "3",
        "replacement_target_stock_qty": "9",
        "replacement_decision": "order",
        "replacement_recommended_order_qty": "7",
        "order_delta_qty": "2",
        "reason_ru": "Отдельный клиентский прогноз; основной заказ не изменён.",
    }
    persisted_ids = persist_grouped_orders(db_session, orders)
    persisted_order = db_session.get(ProcurementOrderFormation, persisted_ids[0])
    assert persisted_order is not None
    persisted_line = persisted_order.lines[0]
    assert persisted_line.recommended_quantity == Decimal("5")
    assert persisted_line.final_quantity == Decimal("5")
    assert serialize_line(persisted_line)["payload"] == line["payload"]


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
