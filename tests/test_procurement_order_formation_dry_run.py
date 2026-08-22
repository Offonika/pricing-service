from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.procurement_order_formation import ProcurementOrderFormation
from app.services.bitrix_order_formation import BitrixCatalogProduct
from app.services.master_mobile_catalog import ProductMediaResolution
from app.services.procurement_order_formation import serialize_line, update_order_line
from app.services.procurement_order_formation_workspace import list_orders
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
    assert all(order["responsible_name"] == "" for order in orders)
    assert all(order["responsible_bitrix_user_id"] == "" for order in orders)
    assert len(seen_guids) == 2
    assert all(line["blockers"] == [] for order in orders for line in order["lines"])
    summary = build_summary(source_rows=sources, selected_rows=sources, orders=orders)
    assert summary["catalog_matched_line_count"] == 2
    assert summary["blocking_line_count"] == 0


def test_grouped_dry_run_rounds_money_half_up(db_session) -> None:
    source = _source("A", "1", "A")
    source["latest_purchase_price"] = "1.005"
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
        contracts={"default": {"code": "C1", "name": "Основной договор"}},
        warehouse={"code": "MAIN", "name": "Центральный склад"},
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-07-10",
        order_date=date(2026, 7, 10),
        calculation_id="calc-money",
    )

    assert orders[0]["lines"][0]["amount"] == "1.01"
    persisted_ids = persist_grouped_orders(db_session, orders)
    persisted = db_session.get(ProcurementOrderFormation, persisted_ids[0])
    assert persisted is not None
    assert persisted.lines[0].amount == Decimal("1.01")


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
    persisted_payload = serialize_line(persisted_line)["payload"]
    assert persisted_payload["b2b_customer_demand"] == line["payload"]["b2b_customer_demand"]
    assert persisted_payload["automatic_recommendation"] == {
        "final_quantity": "5",
        "purchase_price": "100",
        "calculation_id": "calc",
    }


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


def test_grouped_dry_run_uses_only_exact_public_catalog_media() -> None:
    media = ProductMediaResolution(
        article="044702",
        status="found",
        product_card_url="https://master-mobile.ru/catalog/displei/40699/",
        photo_thumbnail_url="https://master-mobile.ru/upload/thumb/40699.webp",
        photo_original_url="https://master-mobile.ru/upload/original/40699.webp",
    )
    resolved_articles: list[str] = []

    def resolve_media(article: str) -> ProductMediaResolution:
        resolved_articles.append(article)
        return media

    orders = build_grouped_orders(
        [_source("A", "5", "A")],
        [_lead("A", "S1", "0xs1")],
        nomenclature_by_code={
            "A": {
                "nomenclature_ref": "0x00010025901E48EF11E1967C11111111",
                "article": "044702",
            }
        },
        catalog_resolver=lambda guid: BitrixCatalogProduct(
            product_id="10",
            name="Каталожный товар",
            xml_id=guid,
            photo_original_url="https://untrusted.example/bitrix-photo.jpg",
        ),
        product_media_resolver=resolve_media,
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
    assert resolved_articles == ["044702"]
    assert line["product_media_status"] == "found"
    assert line["payload"] == {
        "photos": [
            {
                "thumbnail": "https://master-mobile.ru/upload/thumb/40699.webp",
                "original": "https://master-mobile.ru/upload/original/40699.webp",
            }
        ],
        "product_card_url": "https://master-mobile.ru/catalog/displei/40699/",
        "photo_source": "master_mobile_site",
        "delivery_days": "10",
        "supplier_prepare_days": 10,
        "logistics_days": 20,
        "lead_time_days": 30,
        "lead_time_confidence": "high",
        "lead_time_source_level": "sku",
    }


def _grouped_orders_for_persist(*, batch_id: str, calculation_id: str) -> list[dict[str, object]]:
    return build_grouped_orders(
        [_source("A", "5", "A")],
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
        batch_id=batch_id,
        order_date=date(2026, 7, 31),
        calculation_id=calculation_id,
        source_run_id=calculation_id,
        responsible_bitrix_user_id="130757",
    )


def test_persist_repeated_open_batch_updates_without_duplicates(db_session) -> None:
    orders = _grouped_orders_for_persist(batch_id="2026-07-31", calculation_id="887")
    first_ids = persist_grouped_orders(db_session, orders)
    persisted = db_session.get(ProcurementOrderFormation, first_ids[0])
    assert persisted is not None
    original_line_id = persisted.lines[0].id

    orders[0]["lines"][0]["final_quantity"] = "7"
    orders[0]["lines"][0]["amount"] = "700"
    second_ids = persist_grouped_orders(db_session, orders)
    refreshed = db_session.get(ProcurementOrderFormation, first_ids[0])

    assert second_ids == first_ids
    assert refreshed is not None
    assert refreshed.version == 2
    assert refreshed.lines[0].id == original_line_id
    assert refreshed.lines[0].version == 2
    assert refreshed.lines[0].final_quantity == Decimal("7")
    assert list_orders(db_session)["total"] == 1


def test_persist_new_batch_creates_revision_without_mutating_approved_order(db_session) -> None:
    old_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_for_persist(batch_id="2026-07-30", calculation_id="886"),
    )
    old_before_sync = db_session.get(ProcurementOrderFormation, old_ids[0])
    assert old_before_sync is not None
    old_before_sync.status = "approved"
    old_before_sync.approved_version = old_before_sync.version
    db_session.commit()
    new_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_for_persist(batch_id="2026-07-31", calculation_id="887"),
        supersede_open_batches=True,
    )

    old_order = db_session.get(ProcurementOrderFormation, old_ids[0])
    assert old_order is not None
    assert old_order.status == "approved"
    assert old_order.approved_version == old_order.version
    assert new_ids != old_ids
    revision = db_session.get(ProcurementOrderFormation, new_ids[0])
    assert revision is not None
    assert revision.status == "draft"
    assert revision.payload["revision_of_order_id"] == old_order.id
    assert revision.payload["revision_of_stable_key"] == old_order.stable_key


def test_persist_new_batch_merges_open_order_and_preserves_manual_values(db_session) -> None:
    first_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_for_persist(batch_id="2026-07-30", calculation_id="886"),
    )
    order = db_session.get(ProcurementOrderFormation, first_ids[0])
    assert order is not None
    line_id = order.lines[0].id
    update_order_line(
        db_session,
        order.id,
        line_id,
        {"final_quantity": Decimal("7"), "purchase_price": Decimal("90")},
    )

    next_payload = _grouped_orders_for_persist(batch_id="2026-07-31", calculation_id="887")
    next_payload[0]["lines"][0].update(
        recommended_quantity="9",
        final_quantity="9",
        purchase_price="110",
        amount="990",
    )
    next_ids = persist_grouped_orders(db_session, next_payload, supersede_open_batches=True)

    assert next_ids == first_ids
    refreshed = db_session.get(ProcurementOrderFormation, first_ids[0])
    assert refreshed is not None
    assert refreshed.batch_id == "2026-07-31"
    assert refreshed.calculation_id == "887"
    assert refreshed.lines[0].id == line_id
    assert refreshed.lines[0].recommended_quantity == Decimal("9")
    assert refreshed.lines[0].final_quantity == Decimal("7")
    assert refreshed.lines[0].purchase_price == Decimal("90")
    assert refreshed.lines[0].amount == Decimal("630")
    assert refreshed.lines[0].payload["recommendation_discrepancy"] == {
        "final_quantity": {"manual": "7.000", "recommended": "9"},
        "purchase_price": {"manual": "90.0000", "recommended": "110"},
    }


def test_persist_keeps_disappeared_need_visible(db_session) -> None:
    first_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_for_persist(batch_id="2026-07-30", calculation_id="886"),
    )
    next_payload = build_grouped_orders(
        [],
        [],
        nomenclature_by_code={},
        catalog_resolver=lambda _guid: None,
        skip_catalog=False,
        contracts={},
        warehouse={"code": "MAIN", "name": "Склад"},
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-07-31",
        order_date=date(2026, 7, 31),
        calculation_id="887",
    )
    assert next_payload == []

    next_ids = persist_grouped_orders(
        db_session,
        next_payload,
        supersede_open_batches=True,
        sync_context={
            "batch_id": "2026-07-31",
            "order_date": date(2026, 7, 31),
            "calculation_id": "887",
            "source_run_id": "887",
        },
    )

    assert next_ids == first_ids
    refreshed = db_session.get(ProcurementOrderFormation, first_ids[0])
    assert refreshed is not None
    assert refreshed.status == "draft"
    assert refreshed.calculation_id == "887"
    assert refreshed.batch_id == "2026-07-31"
    assert len(refreshed.lines) == 1
    assert refreshed.lines[0].removed is True
    assert refreshed.lines[0].payload["need_status"] == "disappeared"
    assert refreshed.lines[0].payload["disappeared_in_calculation_id"] == "887"
    assert list_orders(db_session)["total"] == 1

    repeated_ids = persist_grouped_orders(
        db_session,
        [],
        supersede_open_batches=True,
        sync_context={
            "batch_id": "2026-07-31",
            "order_date": date(2026, 7, 31),
            "calculation_id": "887",
            "source_run_id": "887",
        },
    )
    assert repeated_ids == first_ids


def test_accepting_fully_disappeared_need_moves_order_to_history(db_session) -> None:
    order_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_for_persist(batch_id="2026-07-30", calculation_id="886"),
    )
    persist_grouped_orders(
        db_session,
        [],
        supersede_open_batches=True,
        sync_context={
            "batch_id": "2026-07-31",
            "order_date": date(2026, 7, 31),
            "calculation_id": "887",
        },
    )
    disappeared = db_session.get(ProcurementOrderFormation, order_ids[0])
    assert disappeared is not None

    resolved = update_order_line(
        db_session,
        disappeared.id,
        disappeared.lines[0].id,
        {"disappearance_resolution": "accepted"},
    )

    assert resolved.status == "superseded"
    assert resolved.lines[0].removed is True
    assert resolved.lines[0].payload["disappearance_resolution"] == "accepted"
    assert list_orders(db_session)["total"] == 0
    assert list_orders(db_session, status="superseded")["total"] == 1
    with pytest.raises(ValueError, match="superseded order is read-only"):
        update_order_line(
            db_session,
            resolved.id,
            resolved.lines[0].id,
            {"final_quantity": Decimal("1")},
        )


def test_manual_retained_need_survives_later_empty_calculation(db_session) -> None:
    order_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_for_persist(batch_id="2026-07-30", calculation_id="886"),
    )
    persist_grouped_orders(
        db_session,
        [],
        supersede_open_batches=True,
        sync_context={
            "batch_id": "2026-07-31",
            "order_date": date(2026, 7, 31),
            "calculation_id": "887",
        },
    )
    disappeared = db_session.get(ProcurementOrderFormation, order_ids[0])
    assert disappeared is not None
    retained = update_order_line(
        db_session,
        disappeared.id,
        disappeared.lines[0].id,
        {"disappearance_resolution": "manual_retained"},
    )
    assert retained.lines[0].removed is False
    assert retained.lines[0].explicit_demand is True
    assert retained.lines[0].payload["manual_overrides"] == {
        "final_quantity": True,
        "purchase_price": True,
    }

    next_ids = persist_grouped_orders(
        db_session,
        [],
        supersede_open_batches=True,
        sync_context={
            "batch_id": "2026-08-01",
            "order_date": date(2026, 8, 1),
            "calculation_id": "888",
        },
    )

    assert next_ids == order_ids
    refreshed = db_session.get(ProcurementOrderFormation, order_ids[0])
    assert refreshed is not None
    assert refreshed.status == "draft"
    assert refreshed.calculation_id == "888"
    assert refreshed.lines[0].removed is False
    assert refreshed.lines[0].explicit_demand is True
    assert refreshed.lines[0].payload["need_status"] == "manual_retained"
    assert refreshed.lines[0].payload["automatic_recommendation"]["final_quantity"] == "0"


def test_persist_reorders_remaining_lines_without_hiding_disappeared_need(db_session) -> None:
    nomenclature = {
        "A": {"nomenclature_ref": "0x00010025901E48EF11E1967C11111111"},
        "B": {"nomenclature_ref": "0x00020025901E48EF11E1967C22222222"},
    }

    def grouped(rows: list[dict[str, str]], *, batch: str, calculation: str):
        return build_grouped_orders(
            rows,
            [_lead("A", "S1", "0xs1"), _lead("B", "S1", "0xs1")],
            nomenclature_by_code=nomenclature,
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
            batch_id=batch,
            order_date=date.fromisoformat(batch),
            calculation_id=calculation,
        )

    first_ids = persist_grouped_orders(
        db_session,
        grouped(
            [_source("A", "5", "A"), _source("B", "2", "B")],
            batch="2026-07-30",
            calculation="886",
        ),
    )
    next_ids = persist_grouped_orders(
        db_session,
        grouped([_source("B", "3", "B")], batch="2026-07-31", calculation="887"),
        supersede_open_batches=True,
    )

    assert next_ids == first_ids
    refreshed = db_session.get(ProcurementOrderFormation, first_ids[0])
    assert refreshed is not None
    by_code = {line.nomenclature_code: line for line in refreshed.lines}
    assert by_code["B"].line_number == 1
    assert by_code["B"].removed is False
    assert by_code["A"].removed is True
    assert by_code["A"].line_number > by_code["B"].line_number

    update_order_line(
        db_session,
        refreshed.id,
        by_code["A"].id,
        {"disappearance_resolution": "manual_retained"},
    )
    third_ids = persist_grouped_orders(
        db_session,
        grouped([_source("B", "4", "B")], batch="2026-08-01", calculation="888"),
        supersede_open_batches=True,
    )

    assert third_ids == first_ids
    db_session.expire_all()
    retained = db_session.get(ProcurementOrderFormation, first_ids[0])
    assert retained is not None
    retained_by_code = {line.nomenclature_code: line for line in retained.lines}
    assert retained_by_code["A"].removed is False
    assert retained_by_code["A"].explicit_demand is True
    assert retained_by_code["A"].payload["need_status"] == "manual_retained"
    assert retained_by_code["B"].final_quantity == Decimal("4")


def test_standard_cron_keeps_blocked_projects_and_uses_shared_queue() -> None:
    script = Path("infra/cron/display_auto_order_sync.sh").read_text(encoding="utf-8")

    assert "--fail-on-blockers" not in script
    assert "DISPLAY_AUTO_ORDER_ASSIGNED_BY_ID:-130757" not in script
    assert "DISPLAY_AUTO_ORDER_ASSIGNED_BY_ID:-}" in script


def test_persist_never_mutates_transmitted_order(db_session) -> None:
    original_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_for_persist(batch_id="2026-07-31", calculation_id="887"),
    )
    original = db_session.get(ProcurementOrderFormation, original_ids[0])
    assert original is not None
    original.status = "transmitted"
    original.onec_status = "transmitted"
    db_session.commit()

    next_payload = _grouped_orders_for_persist(batch_id="2026-07-31", calculation_id="888")
    next_payload[0]["lines"][0]["stable_key"] = "888:A"
    next_ids = persist_grouped_orders(db_session, next_payload)

    db_session.refresh(original)
    assert next_ids != original_ids
    assert original.status == "transmitted"
    assert original.onec_status == "transmitted"
    assert original.lines[0].final_quantity == Decimal("5")
    replacement = db_session.get(ProcurementOrderFormation, next_ids[0])
    assert replacement is not None
    assert replacement.status == "draft"
    assert ":revision:" in replacement.stable_key
