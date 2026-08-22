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
    load_receiving_warehouse,
    parse_args,
    persist_grouped_orders,
    select_order_rows,
)


def test_receiving_warehouse_is_loaded_from_policy(tmp_path: Path) -> None:
    policy = tmp_path / "warehouse-policy.json"
    policy.write_text(
        """{
          "minimum_representation_policy": {"central_warehouse_code": "РБ0000010"},
          "warehouses": [
            {"warehouse_code": "РБ0000010", "name": "Сдэк Склад", "role": "central_transfer_stock"}
          ]
        }""",
        encoding="utf-8",
    )

    assert load_receiving_warehouse(policy) == {
        "ref": "",
        "code": "РБ0000010",
        "name": "Сдэк Склад",
    }


def test_receiving_warehouse_policy_rejects_another_cli_code(tmp_path: Path) -> None:
    policy = tmp_path / "warehouse-policy.json"
    policy.write_text(
        """{
          "minimum_representation_policy": {"central_warehouse_code": "РБ0000010"},
          "warehouses": [
            {"warehouse_code": "РБ0000010", "name": "Сдэк Склад", "role": "central_transfer_stock"}
          ]
        }""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fixed by policy"):
        load_receiving_warehouse(policy, warehouse_code="OTHER")


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


def test_shadow_mode_rejects_every_persistence_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--shadow", "--persist-db"])
    with pytest.raises(SystemExit):
        parse_args(["--shadow", "--supersede-open-batches"])


def test_family_recommendation_selects_both_donor_and_recipient() -> None:
    donor = _source("A", "5", "A")
    donor.update(
        display_family_recommendation_status="allocated_shadow",
        display_family_baseline_order_qty="5",
        display_family_allocated_order_qty="0",
    )
    recipient = _source("B", "0", "B")
    recipient.update(
        display_family_recommendation_status="allocated_shadow",
        display_family_baseline_order_qty="0",
        display_family_allocated_order_qty="5",
    )

    selected = select_order_rows([donor, recipient])

    assert [row["nomenclature_code"] for row in selected] == ["A", "B"]


def test_grouped_dry_run_uses_family_quantity_and_carries_audit_payload() -> None:
    source = _source("A", "5", "A")
    source.update(
        {
            "display_family_recommendation_status": "allocated_shadow",
            "display_family_registry_version": "2",
            "display_family_registry_checksum": "a" * 64,
            "display_family_record_id": "10",
            "display_family_id": "family-1",
            "display_family_label": "Apple iPhone Test",
            "display_family_registry_member_count": "2",
            "display_family_calculation_member_count": "2",
            "display_family_segment_id": "premium|soft_oled",
            "display_family_quality_segment": "premium",
            "display_family_construction_segment": "soft_oled",
            "display_family_baseline_order_qty": "5",
            "display_family_allocated_order_qty": "4",
            "display_family_pool_order_qty": "5",
            "display_family_segment_pool_order_qty": "5",
            "display_family_baseline_share_pct": "100",
            "display_family_target_share_pct": "80",
            "display_family_allocation_source": "completed_sales_rate_30_90",
            "display_family_confidence": "medium",
            "display_family_registry_warning_codes": "",
            "display_family_conflict_codes": "",
            "display_family_reason_ru": "Пул распределён внутри сегмента.",
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
        contracts={"default": {"code": "C1", "name": "Основной договор"}},
        warehouse={"code": "MAIN", "name": "Центральный склад"},
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-08-16",
        order_date=date(2026, 8, 16),
        calculation_id="family-shadow-1",
    )

    line = orders[0]["lines"][0]
    assert line["recommended_quantity"] == "4"
    assert line["source_kind"] == "family_shadow"
    assert line["blockers"] == []
    assert "display_family_manual_approval_required" in line["risk_codes"]
    assert line["payload"]["display_family_recommendation"]["family_id"] == "family-1"
    assert line["payload"]["display_family_recommendation"]["manual_approval_required"] is True
    assert line["recommendation_reason"] == "Пул распределён внутри сегмента."


def test_grouped_dry_run_hides_unchanged_family_reason_and_carries_precise_metrics() -> None:
    source = _source("A", "14", "A")
    source.update(
        {
            "display_family_recommendation_status": "identity_insufficient_eligible_skus",
            "display_family_baseline_order_qty": "14",
            "display_family_allocated_order_qty": "14",
            "display_family_reason_ru": (
                "В подтверждённом сегменте меньше двух доступных SKU; "
                "оставлено базовое количество."
            ),
            "batch_error_return_qty": "5",
            "batch_error_share_pct": "41.7",
            "defect_return_qty": "7",
            "defect_share_pct": "12.6",
            "recommended_order_qty_raw": "14",
            "order_rounding_price_gate": "no_purchase_price",
            "order_rounding_price_gate_ru": "нет закупочной цены",
        }
    )
    orders = build_grouped_orders(
        [source],
        [_lead("A", "S1", "0xs1")],
        nomenclature_by_code={"A": {"nomenclature_ref": "0x00010025901E48EF11E1967C11111111"}},
        catalog_resolver=lambda guid: BitrixCatalogProduct(
            product_id="10", name="Каталожный товар", xml_id=guid, assortment_status="Продажа"
        ),
        skip_catalog=False,
        contracts={"default": {"code": "C1", "name": "Основной договор"}},
        warehouse={"code": "MAIN", "name": "Центральный склад"},
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-08-20",
        order_date=date(2026, 8, 20),
        calculation_id="family-identity-1",
    )

    line = orders[0]["lines"][0]
    assert line["recommendation_reason"] == "Расчётная потребность"
    expected_metrics = {
        "batch_error_return_qty": "5",
        "batch_error_share_pct": "41.7",
        "defect_return_qty": "7",
        "defect_share_pct": "12.6",
        "recommended_order_qty_raw": "14",
        "order_rounding_price_gate": "no_purchase_price",
        "order_rounding_price_gate_ru": "нет закупочной цены",
    }
    assert {key: line["payload"][key] for key in expected_metrics} == expected_metrics


def test_cron_shadow_ignores_truthy_persistence_env(tmp_path: Path) -> None:
    import json
    import os
    import subprocess

    repo_dir = tmp_path / "repo"
    log_dir = tmp_path / "logs"
    result = subprocess.run(
        ["bash", "infra/cron/display_auto_order_sync.sh", "--shadow", "--print-run-mode"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REPO_DIR": str(repo_dir),
            "LOG_DIR": str(log_dir),
            "DISPLAY_AUTO_ORDER_FORMATION_PERSIST_DB": "true",
        },
    )

    payload = json.loads(result.stdout)
    assert payload["run_mode"] == "shadow"
    assert payload["configured_persist_db"] == "true"
    assert payload["effective_persist_db"] == "false"
    assert payload["formation_args"] == "--shadow"


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
        "purchase_price": "1",
        "calculation_id": "calc",
    }


def _grouped_for_supplier_and_price(nomenclature: dict[str, str]) -> list[dict[str, object]]:
    return build_grouped_orders(
        [_source("A", "5", "A")],
        [_lead("A", "S1", "0xs1")],
        nomenclature_by_code={"A": nomenclature},
        catalog_resolver=lambda guid: BitrixCatalogProduct(
            product_id="10", name="Каталожный товар", xml_id=guid, assortment_status="Продажа"
        ),
        skip_catalog=False,
        contracts={"default": {"code": "C1", "name": "Основной договор"}},
        warehouse={"ref": "0xw", "code": "W1", "name": "Склад"},
        currency="RUB",
        procurement_contour="display",
        route="direct",
        batch_id="2026-08-19",
        order_date=date(2026, 8, 19),
        calculation_id="calc",
    )


def test_purchase_price_in_project_is_always_one_rouble() -> None:
    orders = _grouped_for_supplier_and_price(
        {"nomenclature_ref": "0x00010025901E48EF11E1967C11111111"}
    )
    line = orders[0]["lines"][0]
    assert line["purchase_price"] == "1"
    assert line["amount"] == "5.00"
    assert "purchase_price_missing" not in line["blockers"]


def test_main_supplier_from_card_wins_over_purchase_history() -> None:
    orders = _grouped_for_supplier_and_price(
        {
            "nomenclature_ref": "0x00010025901E48EF11E1967C11111111",
            "main_supplier_ref": "0xcard",
            "main_supplier_code": "S9",
            "main_supplier_name": "Основной поставщик карточки",
        }
    )
    assert orders[0]["supplier"]["name"] == "Основной поставщик карточки"
    assert orders[0]["supplier"]["ref"] == "0xcard"


def test_fresh_onec_card_wins_over_earlier_calculation_snapshot() -> None:
    source = _source("A", "5", "A")
    source.update(
        {
            "main_supplier_ref": "0xsnapshot",
            "main_supplier_code": "S8",
            "main_supplier_name": "Поставщик снимка расчёта",
        }
    )
    orders = build_grouped_orders(
        [source],
        [_lead("A", "S8", "0xsnapshot")],
        nomenclature_by_code={
            "A": {
                "nomenclature_ref": "0x00010025901E48EF11E1967C11111111",
                "main_supplier_ref": "0xchanged",
                "main_supplier_code": "S9",
                "main_supplier_name": "Изменён после расчёта",
            }
        },
        catalog_resolver=lambda guid: BitrixCatalogProduct(
            product_id=1,
            name="Каталожный товар",
            xml_id=guid,
        ),
        skip_catalog=False,
        contracts={"default": {"code": "C1", "name": "Основной договор"}},
        warehouse={"ref": "0xw", "code": "W1", "name": "Склад"},
        currency="RUB",
        procurement_contour="display",
        route="direct",
        batch_id="2026-08-20",
        order_date=date(2026, 8, 20),
        calculation_id="calc",
    )

    assert orders[0]["supplier"]["ref"] == "0xchanged"
    assert orders[0]["supplier"]["name"] == "Изменён после расчёта"


def test_purchase_history_supplier_is_used_when_card_is_empty() -> None:
    orders = _grouped_for_supplier_and_price(
        {"nomenclature_ref": "0x00010025901E48EF11E1967C11111111"}
    )
    assert orders[0]["supplier"]["name"] == "Поставщик S1"


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
        "supplier_selection_rule": "historical_evidence_fallback",
        "supplier_selection_reason": "only_historical_supplier_candidate",
    }


def _grouped_orders_with_codes(
    codes: list[str], *, batch_id: str, calculation_id: str
) -> list[dict[str, object]]:
    return build_grouped_orders(
        [_source(code, "5", code) for code in codes],
        [_lead(code, "S1", "0xs1") for code in codes],
        nomenclature_by_code={
            # Ссылка обязана зависеть от кода, а не от позиции в списке: иначе
            # разные карточки получают одну identity и тест перестаёт быть тестом.
            code: {"nomenclature_ref": f"0x0001002590{sum(map(ord, code)):022X}"}
            for code in codes
        },
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


def test_persist_renumbers_disappeared_line_when_batch_grows(db_session) -> None:
    # Боевое падение 2026-08-19 09:31 (uq_proc_order_line_order_number,
    # order_id/line_number = 99/16): строка, помеченная "потребность исчезла"
    # на прошлом прогоне, получила номер за пределами тогдашней партии. Когда
    # партия выросла, этот номер занимает новая строка, а старая остаётся на
    # месте и ловит конфликт уникальности.
    persist_grouped_orders(
        db_session,
        _grouped_orders_with_codes(["A", "B"], batch_id="2026-08-19", calculation_id="900"),
    )
    # Потребность по B исчезла: строка остаётся видимой и уезжает за границу партии.
    ids = persist_grouped_orders(
        db_session,
        _grouped_orders_with_codes(["A"], batch_id="2026-08-19", calculation_id="901"),
    )
    stored = db_session.get(ProcurementOrderFormation, ids[0])
    assert stored is not None
    disappeared = [line for line in stored.lines if line.removed]
    assert [line.nomenclature_code for line in disappeared] == ["B"]
    occupied_number = disappeared[0].line_number

    # Партия выросла: новых карточек больше, чем номер исчезнувшей строки.
    grown_codes = ["A", "C", "D", "E"]
    assert len(grown_codes) >= occupied_number, "тест должен перекрыть занятый номер"
    grown_ids = persist_grouped_orders(
        db_session,
        _grouped_orders_with_codes(grown_codes, batch_id="2026-08-19", calculation_id="902"),
    )
    refreshed = db_session.get(ProcurementOrderFormation, grown_ids[0])

    assert refreshed is not None
    numbers = [line.line_number for line in refreshed.lines]
    assert len(numbers) == len(set(numbers)), "номера строк обязаны остаться уникальными"
    assert {line.nomenclature_code for line in refreshed.lines if not line.removed} == set(
        grown_codes
    )
    assert [line.nomenclature_code for line in refreshed.lines if line.removed] == ["B"]


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
    next_payload = _grouped_orders_for_persist(batch_id="2026-07-31", calculation_id="887")
    next_payload[0]["lines"] = []

    next_ids = persist_grouped_orders(db_session, next_payload, supersede_open_batches=True)

    assert next_ids == first_ids
    refreshed = db_session.get(ProcurementOrderFormation, first_ids[0])
    assert refreshed is not None
    assert len(refreshed.lines) == 1
    assert refreshed.lines[0].removed is True
    assert refreshed.lines[0].payload["need_status"] == "disappeared"
    assert refreshed.lines[0].payload["disappeared_in_calculation_id"] == "887"


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


def test_standard_cron_keeps_blocked_projects_and_uses_shared_queue() -> None:
    script = Path("infra/cron/display_auto_order_sync.sh").read_text(encoding="utf-8")

    assert "--fail-on-blockers" not in script
    assert "DISPLAY_AUTO_ORDER_ASSIGNED_BY_ID:-130757" not in script
    assert "DISPLAY_AUTO_ORDER_ASSIGNED_BY_ID:-}" in script
    assert "tasks.backfill_procurement_order_metrics" in script
    assert '--order-ids-from-json "${ORDER_FORMATION_OUTPUT_JSON}"' in script
    assert 'if is_truthy "${ORDER_FORMATION_PERSIST_DB}"; then' in script


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
