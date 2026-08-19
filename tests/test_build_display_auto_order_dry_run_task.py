from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.procurement_b2b_customer_demand import (
    B2BCustomerDemandComponent,
    B2BSkuDemandProfile,
)
from tasks.build_display_auto_order_dry_run import (
    DEFAULT_POLICY_JSON,
    AutoOrderPolicy,
    DemandUpliftRule,
    OrderRoundingPriceGatePolicy,
    OrderRoundingRule,
    PriceBatchRule,
    SpeedHorizonRule,
    _price_batch_rule_for_row,
    auto_order_scope_gate_reasons,
    build_auto_order_scope_gate_audit,
    build_dry_run_rows,
    build_summary,
    fetch_active_customer_order_totals,
    fetch_days_in_sale_totals,
    fetch_reserved_totals,
    fetch_sales_totals,
    fetch_stock_totals,
    load_auto_order_items_with_scope_audit,
    load_auto_order_policy,
    load_warehouse_policy,
    order_rounding_price_gates,
    rounded_order_qty,
    write_scope_gate_csv,
)


class _EmptyMappingsResult:
    def mappings(self):
        return self

    def __iter__(self):
        return iter(())


class _CaptureConnection:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, statement):
        self.statements.append(str(statement))
        return _EmptyMappingsResult()


class _CaptureEngine:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def connect(self):
        return _CaptureConnection(self.statements)


def test_stock_query_filters_by_onec_quality_and_warehouse_role(tmp_path) -> None:
    # Найдено и подтверждено пользователем 2026-07-31: одного качества
    # ("Новый") недостаточно - остаток на складах с ролью Резерв/Производства/
    # Брак (Уценка, утерянный карго и т.п.) не готов к продаже, хотя качество
    # у него формально "Новый". Добавлен фильтр по реальному реквизиту
    # "Роль склада" (тот же генерик-механизм, что и для маркетплейса) -
    # sellable_stock_qty теперь ограничен ролями Точка продаж/Транзит/
    # Центральный. central_stock_qty по-прежнему считается отдельно по
    # policy.central_codes (не связано с этим фильтром), а fetch_reserved_
    # totals по-прежнему не фильтрует склад вообще - это НЕ менялось.
    policy_path = tmp_path / "warehouse-policy.json"
    policy_path.write_text(
        '{"usable_stock_quality_names":["Новый"],"warehouses":'
        '[{"warehouse_code":"SALE","sells_systematically":true},'
        '{"warehouse_code":"TRANSIT","is_transit":true}]}',
        encoding="utf-8",
    )
    policy = load_warehouse_policy(policy_path)
    engine = _CaptureEngine()

    fetch_stock_totals(engine, codes=["RB1"], policy=policy)
    fetch_reserved_totals(engine, codes=["RB1"], policy=policy)

    stock_sql, reserve_sql = engine.statements
    assert policy.usable_stock_quality_names == ("Новый",)
    assert "_Reference48 AS quality" in stock_sql
    assert "stock._Fld7741RRef" in stock_sql
    assert "usable_stock_quality_names" in stock_sql
    assert "sellable_warehouse_role_names" in stock_sql
    assert "_InfoRg6309" in stock_sql
    assert "sellable_codes" not in stock_sql
    assert "sellable_codes" not in reserve_sql
    assert "_InfoRg6309" not in reserve_sql


def test_sales_totals_query_includes_90_and_30_day_trend_windows() -> None:
    engine = _CaptureEngine()

    fetch_sales_totals(
        engine,
        codes=["RB1"],
        sellable_codes=("SALE",),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 7, 30),
    )

    (sales_sql,) = engine.statements
    assert "sales_qty_window_medium" in sales_sql
    assert "sales_qty_window_short" in sales_sql
    assert "window_medium_from" in sales_sql
    assert "window_short_from" in sales_sql


def test_active_customer_order_query_uses_positive_open_balances_without_name_filter() -> None:
    engine = _CaptureEngine()

    fetch_active_customer_order_totals(
        engine,
        codes=["RB1"],
        as_of=date(2026, 8, 18),
        max_age_days=30,
    )

    (customer_order_sql,) = engine.statements
    assert "_AccumRgT7145" in customer_order_sql
    assert "balance._Fld7131RRef" in customer_order_sql
    assert "balance._Fld7129RRef" in customer_order_sql
    assert "balance._Fld7140" in customer_order_sql
    assert "customer_order._Posted = 0x01" in customer_order_sql
    assert "customer_order._Marked = 0x00" in customer_order_sql
    assert "HAVING SUM" in customer_order_sql
    assert "> 0" in customer_order_sql
    assert "Потребности" not in customer_order_sql
    # Решение 2026-08-18: 1С не закрывает старые потребности, поэтому без
    # отсечки по дате формула тянет многолетние мёртвые остатки регистра.
    assert "customer_order._Date_Time >= :active_order_min_date" in customer_order_sql


def test_active_customer_order_policy_reads_max_age_and_validates(tmp_path) -> None:
    policy_path = tmp_path / "auto-order-policy.json"
    policy_path.write_text(
        '{"auto_order_policy": {"active_customer_order_policy":'
        ' {"active_order_max_age_days": 45}}}',
        encoding="utf-8",
    )
    policy = load_auto_order_policy(policy_path)
    assert policy.active_customer_order_max_age_days == 45

    default_policy = load_auto_order_policy(tmp_path / "missing.json")
    assert default_policy.active_customer_order_max_age_days == 30

    with pytest.raises(SystemExit):
        AutoOrderPolicy(active_customer_order_max_age_days=0).validate()


def test_stock_totals_batches_more_than_sql_server_rpc_limit(tmp_path) -> None:
    policy_path = tmp_path / "warehouse-policy.json"
    policy_path.write_text(
        '{"usable_stock_quality_names":["Новый"],"warehouses":'
        '[{"warehouse_code":"SALE","sells_systematically":true}]}',
        encoding="utf-8",
    )
    engine = _CaptureEngine()

    fetch_stock_totals(
        engine,
        codes=[f"CODE-{index:04d}" for index in range(2336)],
        policy=load_warehouse_policy(policy_path),
    )

    assert len(engine.statements) == 3


def test_display_auto_order_excludes_bitok_before_quantity_calculation() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "KEEP",
                "name": "Дисплей обычный",
                "status_label": "Рабочий",
            },
            {
                "nomenclature_code": "DROP",
                "name": "Дисплей (БИТОК)",
                "status_label": "Рабочий",
            },
        ],
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    assert [row["nomenclature_code"] for row in rows] == ["KEEP"]


def test_display_auto_order_summary_carries_scope_policy_audit() -> None:
    audit = {
        "scope_policy_version": "display_scope_policy.v1",
        "source_item_count": 2,
        "included_item_count": 1,
        "excluded_item_count": 1,
        "excluded_row_count": 1,
        "excluded_reason_counts": {"excluded_display_name_bitok": 1},
        "exclusions": [],
    }

    summary = build_summary([], run_id=1, source_errors={}, scope_policy_audit=audit)

    assert summary["scope_policy"] == audit


def test_display_auto_order_b2b_customer_demand_is_advisory_only() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB_B2B",
                "name": "Display B2B",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB_B2B": {"sellable_stock_qty": Decimal("0")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB_B2B": {"sales_qty_window": Decimal("180")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        b2b_customer_demand_profiles={
            "RB_B2B": B2BSkuDemandProfile(
                nomenclature_code="RB_B2B",
                profile_as_of_exclusive=date(2026, 7, 10),
                managed_sales_qty_180=Decimal("90"),
                managed_sales_qty_270=Decimal("120"),
                dependency_class="Только активные клиенты 3/4/5",
                active_high_tier_share_pct=Decimal("75"),
                components=(
                    B2BCustomerDemandComponent(
                        counterparty_ref="C1",
                        activity_status="Активный",
                        expected_purchase_date=date(2026, 7, 15),
                        daily_rate=Decimal("0.2"),
                    ),
                    B2BCustomerDemandComponent(
                        counterparty_ref="C2",
                        activity_status="Пассивный",
                        expected_purchase_date=date(2026, 6, 1),
                        daily_rate=Decimal("0.1"),
                    ),
                ),
            )
        },
        as_of=date(2026, 7, 10),
    )

    row = rows[0]
    assert row["recommended_order_qty"] == "14"
    assert row["target_stock_qty"] == "14"
    assert row["b2b_demand_mode"] == "advisory_only"
    assert row["b2b_active_customer_count"] == 1
    assert row["b2b_passive_customer_count"] == 1
    assert row["b2b_due_customer_count"] == 1
    assert row["b2b_managed_sales_qty_window"] == "90"
    assert row["b2b_ordinary_net_sales_qty_window"] == "90"
    assert row["b2b_client_forecast_qty"] == "3"
    assert row["b2b_replacement_target_stock_qty"] == "10"
    assert row["b2b_replacement_decision"] == "order"
    assert row["b2b_replacement_recommended_order_qty"] == "10"
    assert row["b2b_order_delta_qty"] == "-4"
    assert "основное количество не изменено" in row["b2b_reason_ru"]
    assert "b2b_customer_demand_advisory" in row["warnings"]
    assert "b2b_client_only_sku" in row["warnings"]
    assert "b2b_passive_reactivation_not_calibrated" in row["warnings"]


def test_display_auto_order_b2b_customer_demand_applies_independently_per_card() -> None:
    # Раньше называлось "...consolidates_analog_group" и проверяло, что
    # b2b-прогноз сводится к "победителю" группы аналогов - тот же принцип
    # консолидации, что отменён и удалён 2026-07-31 (раздел 4). Найден
    # реальный побочный баг: без победителя карточки, попавшие в группу по
    # токенам, молча пропускали b2b-пересчёт вовсе. Исправлено -
    # apply_b2b_final_order_policies больше не группирует по аналогам.
    # Проверяем: ОБЕ карточки с общими токенами получают b2b-пересчёт
    # независимо и одинаково (симметрично), ни одна не пропущена.
    items = [
        {
            "nomenclature_code": "RB_ORIG",
            "name": "Дисплей для Apple iPhone 11 + тачскрин ORIG",
            "status_label": "Рабочий",
            "brand_compatibility": "Apple",
            "model_compatibility": "iPhone 11",
            "quality_raw": "ORIG",
        },
        {
            "nomenclature_code": "RB_COPY",
            "name": "Дисплей для Apple iPhone 11 + тачскрин Medium",
            "status_label": "Рабочий",
            "brand_compatibility": "Apple",
            "model_compatibility": "iPhone 11",
            "quality_raw": "Medium",
        },
    ]
    profiles = {
        code: B2BSkuDemandProfile(
            nomenclature_code=code,
            profile_as_of_exclusive=date(2026, 7, 10),
            managed_sales_qty_180=Decimal("90"),
            managed_sales_qty_270=Decimal("120"),
            dependency_class="Смешанный спрос",
            active_high_tier_share_pct=Decimal("25"),
            components=(
                B2BCustomerDemandComponent(
                    counterparty_ref=f"C-{code}",
                    activity_status="Активный",
                    expected_purchase_date=date(2026, 7, 15),
                    daily_rate=Decimal("0.2"),
                ),
            ),
        )
        for code in ("RB_ORIG", "RB_COPY")
    }

    rows = build_dry_run_rows(
        items,
        facts={
            "stock": {
                "RB_ORIG": {"sellable_stock_qty": Decimal("0")},
                "RB_COPY": {"sellable_stock_qty": Decimal("0")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB_ORIG": {"sales_qty_window": Decimal("180")},
                "RB_COPY": {"sales_qty_window": Decimal("180")},
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        b2b_customer_demand_profiles=profiles,
        as_of=date(2026, 7, 10),
    )

    by_code = {row["nomenclature_code"]: row for row in rows}
    orig = by_code["RB_ORIG"]
    copy = by_code["RB_COPY"]
    for card in (orig, copy):
        assert card["analog_role"] == "single_sku"
        assert card["recommended_order_qty"] == "14"
        assert card["b2b_replacement_target_stock_qty"] == "10"
        assert card["b2b_replacement_recommended_order_qty"] == "10"
        assert card["b2b_replacement_decision"] == "order"
        assert card["b2b_order_delta_qty"] == "-4"


def test_display_auto_order_dry_run_caps_recommended_order_qty() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB1",
                "name": "Display test",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
                "price_segment": "middle",
            }
        ],
        facts={
            "stock": {
                "RB1": {
                    "sellable_stock_qty": Decimal("7"),
                    "central_stock_qty": Decimal("7"),
                    "total_stock_qty": Decimal("7"),
                }
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB1": {
                    "sales_qty_window": Decimal("170"),
                    "sales_doc_count": 17,
                    "sales_warehouse_count": 3,
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        max_order_qty=5,
    )

    assert rows[0]["target_stock_qty"] == "14"
    assert rows[0]["recommended_order_qty_raw"] == "7"
    assert rows[0]["recommended_order_qty"] == "5"
    assert rows[0]["dry_run_decision"] == "order"
    assert rows[0]["warnings"] == "order_qty_capped"
    assert "Рекомендуем 5 шт." in rows[0]["reason_ru"]

    summary = build_summary(rows, run_id=203, source_errors={})
    assert summary["decision_counts"] == {"order": 1}
    assert summary["total_recommended_order_qty"] == "5"


def test_display_auto_order_dry_run_can_run_without_max_order_qty_cap() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB_NO_CAP",
                "name": "Display no cap",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
                "price_segment": "middle",
            }
        ],
        facts={
            "stock": {"RB_NO_CAP": {"sellable_stock_qty": Decimal("7")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB_NO_CAP": {"sales_qty_window": Decimal("170")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    assert rows[0]["recommended_order_qty_raw"] == "7"
    assert rows[0]["recommended_order_qty"] == "7"
    assert rows[0]["dry_run_decision"] == "order"
    assert rows[0]["warnings"] == ""


def test_active_customer_orders_increase_need_for_sale_and_working_without_reserve() -> None:
    items = [
        {
            "nomenclature_code": f"RB_{status.upper()}",
            "name": f"Display {status}",
            "status": status,
            "status_label": status_label,
            "auto_order_allowed": True,
        }
        for status, status_label in (
            ("sale", "Растим (ПРОДАЖА)"),
            ("working", "Поддерживаем (Рабочий)"),
        )
    ]
    codes = [item["nomenclature_code"] for item in items]
    rows = build_dry_run_rows(
        items,
        facts={
            "stock": {code: {"sellable_stock_qty": Decimal("10")} for code in codes},
            "reserve": {code: {"reserved_qty": Decimal("4")} for code in codes},
            "customer_orders": {
                code: {
                    "active_customer_order_qty": Decimal("7"),
                    "active_customer_order_count": 3,
                }
                for code in codes
            },
            "incoming": {},
            "sales": {code: {"sales_qty_window": Decimal("180")} for code in codes},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    for row in rows:
        assert row["target_stock_qty"] == "14"
        assert row["sellable_stock_qty"] == "10"
        assert row["reserved_qty"] == "4"
        assert row["free_stock_qty"] == "6"
        assert row["active_customer_order_qty"] == "7"
        assert row["active_customer_order_count"] == 3
        assert row["order_available_stock_qty"] == "3"
        assert row["recommended_order_qty_raw"] == "11"
        assert row["recommended_order_qty"] == "11"
        assert "active_customer_orders_added_to_need" in row["warnings"]
        assert "Зарезервировано и Под заказ" in row["reason_ru"]


def test_active_customer_orders_do_not_change_other_status_formulas() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB_NEW",
                "name": "Display new item",
                "status": "new_item",
                "status_label": "Завезли (Новинка)",
                "auto_order_allowed": True,
            }
        ],
        facts={
            "stock": {"RB_NEW": {"sellable_stock_qty": Decimal("10")}},
            "reserve": {"RB_NEW": {"reserved_qty": Decimal("4")}},
            "customer_orders": {
                "RB_NEW": {
                    "active_customer_order_qty": Decimal("7"),
                    "active_customer_order_count": 3,
                }
            },
            "incoming": {},
            "sales": {"RB_NEW": {"sales_qty_window": Decimal("180")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    row = rows[0]
    assert row["active_customer_order_qty"] == "0"
    assert row["active_customer_order_count"] == 0
    assert row["order_available_stock_qty"] == "6"
    assert row["recommended_order_qty_raw"] == "8"


def test_display_auto_order_dry_run_rounds_large_orders_by_tiers() -> None:
    rules = (
        OrderRoundingRule(threshold_gt=Decimal("2000"), round_to=100),
        OrderRoundingRule(threshold_gt=Decimal("1000"), round_to=50),
        OrderRoundingRule(threshold_gt=Decimal("100"), round_to=10),
    )

    assert rounded_order_qty(
        Decimal("2258"),
        min_order_qty=1,
        max_order_qty=None,
        order_rounding_rules=rules,
    ) == Decimal("2300")
    assert rounded_order_qty(
        Decimal("1127"),
        min_order_qty=1,
        max_order_qty=None,
        order_rounding_rules=rules,
    ) == Decimal("1150")
    assert rounded_order_qty(
        Decimal("371"),
        min_order_qty=1,
        max_order_qty=None,
        order_rounding_rules=rules,
    ) == Decimal("380")
    assert rounded_order_qty(
        Decimal("100"),
        min_order_qty=1,
        max_order_qty=None,
        order_rounding_rules=rules,
    ) == Decimal("100")


def test_display_auto_order_dry_run_applies_scoped_demand_uplift_by_model_token() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "IPHONE11",
                "name": "Дисплей для Apple iPhone 11 + тачскрин (черный) (JK)",
                "status_label": "Рабочий",
                "brand_compatibility": "Apple",
                "model_compatibility": "iPhone 11",
                "quality_raw": "Optima",
            },
            {
                "nomenclature_code": "IPHONE12",
                "name": "Дисплей для Apple iPhone 12 + тачскрин (черный) (JK)",
                "status_label": "Рабочий",
                "brand_compatibility": "Apple",
                "model_compatibility": "iPhone 12",
                "quality_raw": "Optima",
            },
        ],
        facts={
            "stock": {
                "IPHONE11": {"sellable_stock_qty": Decimal("0")},
                "IPHONE12": {"sellable_stock_qty": Decimal("0")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "IPHONE11": {"sales_qty_window": Decimal("180")},
                "IPHONE12": {"sales_qty_window": Decimal("180")},
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        demand_uplift_rules=(
            DemandUpliftRule(
                rule_id="iphone11_demand_uplift",
                match_any_analog_model_tokens=("apple:model:iphone 11",),
                demand_multiplier=Decimal("1.3"),
                reason_ru="проверочный рост спроса",
            ),
        ),
    )

    by_code = {row["nomenclature_code"]: row for row in rows}

    assert by_code["IPHONE11"]["base_avg_daily_sales_qty"] == "1"
    assert by_code["IPHONE11"]["avg_daily_sales_qty"] == "1.3"
    assert by_code["IPHONE11"]["adjusted_net_sales_qty_window"] == "234"
    assert by_code["IPHONE11"]["target_stock_qty"] == "19"
    assert by_code["IPHONE11"]["recommended_order_qty"] == "19"
    assert by_code["IPHONE11"]["demand_adjustment_rule_id"] == "iphone11_demand_uplift"
    assert "stockout_demand_uplift_applied" in by_code["IPHONE11"]["warnings"]

    assert by_code["IPHONE12"]["base_avg_daily_sales_qty"] == "1"
    assert by_code["IPHONE12"]["avg_daily_sales_qty"] == "1"
    assert by_code["IPHONE12"]["target_stock_qty"] == "14"
    assert by_code["IPHONE12"]["recommended_order_qty"] == "14"
    assert by_code["IPHONE12"]["demand_adjustment_rule_id"] == ""
    assert "stockout_demand_uplift_applied" not in by_code["IPHONE12"]["warnings"]


def test_display_auto_order_dry_run_deducts_incoming_from_need() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB2",
                "name": "Display test 2",
                "status_label": "Рабочий",
                "quality_raw": "ORIG",
                "price_segment": "premium",
            }
        ],
        facts={
            "stock": {
                "RB2": {
                    "sellable_stock_qty": Decimal("3"),
                    "central_stock_qty": Decimal("3"),
                    "total_stock_qty": Decimal("3"),
                }
            },
            "reserve": {"RB2": {"reserved_qty": Decimal("1")}},
            "incoming": {"RB2": {"incoming_qty": Decimal("3"), "incoming_order_count": 1}},
            "sales": {"RB2": {"sales_qty_window": Decimal("36")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        max_order_qty=5,
    )

    assert rows[0]["free_stock_qty"] == "2"
    assert rows[0]["target_stock_qty"] == "3"
    assert rows[0]["recommended_order_qty"] == "0"
    assert rows[0]["dry_run_decision"] == "do_not_order"
    assert rows[0]["warnings"] == "incoming_deducted_from_need"
    assert "товаром в пути 3 шт." in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_rounds_positive_need_to_min_order_qty() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB_MIN",
                "name": "Display min order",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
                "price_segment": "middle",
            }
        ],
        facts={
            "stock": {"RB_MIN": {"sellable_stock_qty": Decimal("9")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB_MIN": {"sales_qty_window": Decimal("128.5")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        min_order_qty=1,
        max_order_qty=5,
    )

    assert rows[0]["target_stock_qty"] == "10"
    assert rows[0]["recommended_order_qty_raw"] == "1"
    assert rows[0]["recommended_order_qty"] == "1"
    assert rows[0]["dry_run_decision"] == "order"


# test_display_auto_order_dry_run_caps_group_horizon_by_speed_tier удалён
# 2026-07-31: проверял, что СУММА скорости группы аналогов (2+1=3 шт/день)
# переводит тир, хотя ни одна карточка поодиночке не набирает порог -
# ровно та консолидация, что отменена и удалена из кода (раздел 4). Тир
# теперь считается по собственной скорости каждой карточки, см.
# test_display_auto_order_dry_run_speed_tier_does_not_undo_blocker_zeroed_
# order и другие тесты apply_independent_speed_tier ниже.


def test_display_auto_order_dry_run_speed_tier_horizon_extends_by_distribution_to_shelf_days() -> (
    None
):
    # Найдено 2026-07-30: тиры speed_horizon_rules (max_effective_target_days
    # 60/70/82) считались отдельно от базовой формулы и не учитывали +7 дней
    # распределения по полке - хотя ту же дыру уже закрыли для одиночных
    # строк (build_dry_run_rows). По просьбе пользователя добавлено и сюда,
    # через тот же параметр, а не хардкодом в конфиге.
    def _build(distribution_to_shelf_days: int) -> dict:
        rows = build_dry_run_rows(
            [
                {
                    "nomenclature_code": "RB-MED",
                    "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный) (Medium)",
                    "status": "working",
                    "status_label": "Рабочий",
                    "auto_order_allowed": True,
                    "quality_raw": "Medium",
                },
            ],
            facts={
                "stock": {"RB-MED": {"sellable_stock_qty": Decimal("0")}},
                "reserve": {},
                "incoming": {},
                "sales": {"RB-MED": {"sales_qty_window": Decimal("360"), "sales_doc_count": 36}},
                "returns": {},
                "purchase": {"RB-MED": {"latest_purchase_price": Decimal("35")}},
            },
            source_errors={},
            target_days=14,
            order_cadence_days=7,
            supplier_prepare_days=18,
            logistics_days=30,
            supplier_delay_buffer_days=3,
            receiving_buffer_days=1,
            safety_stock_days=25,
            sales_window_days=180,
            distribution_to_shelf_days=distribution_to_shelf_days,
            speed_horizon_rules=(
                SpeedHorizonRule(
                    tier="super_fast",
                    min_group_avg_daily_sales_qty=Decimal("2"),
                    max_effective_target_days=60,
                    safety_stock_days=7,
                    label_ru="супер-ходовая группа",
                ),
            ),
        )
        return rows[0]

    baseline = _build(0)
    extended = _build(7)

    assert baseline["effective_target_days"] == 60
    assert baseline["distribution_to_shelf_days"] == 0
    assert extended["effective_target_days"] == 67
    assert extended["distribution_to_shelf_days"] == 7
    assert int(extended["recommended_order_qty"]) > int(baseline["recommended_order_qty"])


def test_display_auto_order_dry_run_sends_slow_group_to_manual_review() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SLOW",
                "name": "Дисплей медленный",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            # Остаток 15 шт - выше структурного пола (13, см.
            # STRUCTURAL_FLOOR_QTY) - карточка не должна попасть в
            # исключение "стартовый заказ", проверяем обычное поведение
            # review_only.
            "stock": {"RB-SLOW": {"sellable_stock_qty": Decimal("15")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-SLOW": {"sales_qty_window": Decimal("18")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="slow",
                min_group_avg_daily_sales_qty=Decimal("0"),
                review_only=True,
                label_ru="медленная группа",
            ),
        ),
    )

    assert rows[0]["speed_tier"] == "slow"
    assert rows[0]["dry_run_decision"] == "manual_review"
    assert rows[0]["recommended_order_qty"] == "0"
    assert "speed_tier_manual_review" in rows[0]["warnings"]
    assert "автозаказ не раздуваем" in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_slow_group_below_structural_floor_gets_starter_order() -> None:
    # Решение 2026-07-31 (РБ000064721/РБ000057817): медленная карточка с
    # остатком по сети ниже структурного пола (13 шт, STRUCTURAL_FLOOR_QTY)
    # не должна зануляться - используется уже посчитанная цель как обычный
    # заказ, decision="order".
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SLOW-EMPTY",
                "name": "Дисплей медленный без остатка",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB-SLOW-EMPTY": {"sellable_stock_qty": Decimal("0")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-SLOW-EMPTY": {"sales_qty_window": Decimal("18")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="slow",
                min_group_avg_daily_sales_qty=Decimal("0"),
                review_only=True,
                label_ru="медленная группа",
            ),
        ),
    )

    assert rows[0]["speed_tier"] == "slow"
    assert rows[0]["dry_run_decision"] == "order"
    assert Decimal(rows[0]["recommended_order_qty"]) > 0
    assert "structural_floor_starter_order" in rows[0]["warnings"]
    assert "Структурный пол" in rows[0]["reason_ru"]


class _IntervalRowsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return self._rows


class _IntervalConnection:
    def __init__(self, rows) -> None:
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, _statement):
        return _IntervalRowsResult(self._rows)


class _IntervalEngine:
    def __init__(self, rows) -> None:
        self._rows = rows

    def connect(self):
        return _IntervalConnection(self._rows)


def test_days_in_sale_merges_point_intervals_instead_of_averaging() -> None:
    # Исправление 2026-08-09: дни наличия - это дни, когда товар был хотя бы
    # в одной точке (объединение интервалов), а не сумма точко-дней,
    # делённая на число точек. Две точки: 01-31 июля (31 день) и
    # 15 июля - 09 августа (26 дней). Объединение = 01 июля - 09 августа =
    # 40 дней. Прежняя формула дала бы (31 + 26) / 2 = 28.5 дня.
    engine = _IntervalEngine(
        [
            {
                "product_code": "RB1",
                "available_from": date(2026, 7, 1),
                "available_to": date(2026, 7, 31),
            },
            {
                "product_code": "RB1",
                "available_from": date(2026, 7, 15),
                "available_to": date(2026, 8, 9),
            },
        ]
    )

    totals = fetch_days_in_sale_totals(
        engine,
        codes=["RB1"],
        physical_sales_point_codes=("SALE-A", "SALE-B"),
        date_to=date(2026, 8, 9),
        windows_days=(180,),
    )

    assert totals["RB1"][180] == Decimal("40")


def test_days_in_sale_clips_intervals_to_the_requested_window() -> None:
    # Интервал шире окна обрезается его границами: окно 30 дней до 09.08
    # начинается 11.07, значит из интервала 01.06-09.08 в окно попадает
    # ровно 30 дней.
    engine = _IntervalEngine(
        [
            {
                "product_code": "RB1",
                "available_from": date(2026, 6, 1),
                "available_to": date(2026, 8, 9),
            }
        ]
    )

    totals = fetch_days_in_sale_totals(
        engine,
        codes=["RB1"],
        physical_sales_point_codes=("SALE-A",),
        date_to=date(2026, 8, 9),
        windows_days=(30, 90),
    )

    assert totals["RB1"][30] == Decimal("30")
    assert totals["RB1"][90] == Decimal("70")


def test_display_auto_order_dry_run_uses_soft_availability_correction() -> None:
    # Решение 2026-08-09: дни без товара достраиваются МЯГКО, по календарной
    # скорости, а не делением продаж на дни наличия. Окно 180, продажи 18,
    # дни в продаже 90: base = 18/180 = 0.1; virtual = 90 * 0.1 = 9;
    # rate = (18 + 9)/180 = 0.15. Жёсткая формула дала бы 18/90 = 0.2.
    # Потолок мягкой поправки - x2 к календарной скорости.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SOFT-RATE",
                "name": "Дисплей, половину окна не было на полке",
                "status": "working",
                "status_label": "Поддерживаем",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB-SOFT-RATE": {"sellable_stock_qty": Decimal("0")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-SOFT-RATE": {"sales_qty_window": Decimal("18")}},
            "returns": {},
            "days_in_sale": {"RB-SOFT-RATE": {180: Decimal("90")}},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    # base_avg_daily_sales_qty - это уже скорость с поправкой наличия
    # (demand_multiplier здесь 1, поэтому avg равен base).
    assert rows[0]["base_avg_daily_sales_qty"] == "0.15"
    assert rows[0]["avg_daily_sales_qty"] == "0.15"
    assert rows[0]["days_in_sale_long"] == "90"


def test_display_auto_order_dry_run_short_history_disables_correction() -> None:
    # Предохранитель 2026-08-09 (MIN_RELIABLE_AVAILABILITY_DAYS = 15): товар
    # был на полке 1 день из 180 - по такой базе о скорости судить нельзя,
    # виртуальные продажи не достраиваются вообще, скорость календарная.
    # Жёсткая формула дала бы 18/1 = 18 шт/день, мягкая без предохранителя -
    # 0.1994.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SOFT-EDGE",
                "name": "Дисплей, был на полке один день",
                "status": "working",
                "status_label": "Поддерживаем",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB-SOFT-EDGE": {"sellable_stock_qty": Decimal("0")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-SOFT-EDGE": {"sales_qty_window": Decimal("18")}},
            "returns": {},
            "days_in_sale": {"RB-SOFT-EDGE": {180: Decimal("1")}},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    assert rows[0]["avg_daily_sales_qty"] == "0.1"


def test_display_auto_order_dry_run_short_history_order_goes_to_manual_review() -> None:
    # Вторая половина предохранителя: если строка с короткой историей наличия
    # всё же получает положительный заказ (здесь - через структурный пол),
    # финальный гейт переводит её в ручную проверку и обнуляет количество.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SHORT-HIST",
                "name": "Дисплей, мало дней наблюдения",
                "status": "working",
                "status_label": "Поддерживаем",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB-SHORT-HIST": {"sellable_stock_qty": Decimal("0")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-SHORT-HIST": {"sales_qty_window": Decimal("18")}},
            "returns": {},
            "days_in_sale": {
                "RB-SHORT-HIST": {30: Decimal("5"), 90: Decimal("10"), 180: Decimal("10")}
            },
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="slow",
                min_group_avg_daily_sales_qty=Decimal("0"),
                review_only=True,
                label_ru="медленная группа",
            ),
        ),
    )

    assert rows[0]["dry_run_decision"] == "manual_review"
    assert rows[0]["recommended_order_qty"] == "0"
    assert "availability_history_too_short" in rows[0]["warnings"]
    assert "Мало данных о наличии" in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_slow_group_flat_despite_availability_is_pension_candidate() -> (
    None
):
    # Решение 2026-07-31 (РБ000029831, "гейт Пенсии"): медленная карточка
    # ниже структурного пола, у которой БЫЛИ честные дни в продаже
    # (days_in_sale_medium >= PENSION_CANDIDATE_MIN_DAYS_IN_SALE) и даже
    # тогда скорость не растёт - это не голодание, автозаказ не даём, уходит
    # на ручную проверку как кандидат на "Пенсию".
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SLOW-FLAT",
                "name": "Дисплей медленный, но был на полке",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB-SLOW-FLAT": {"sellable_stock_qty": Decimal("5")}},
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-SLOW-FLAT": {
                    "sales_qty_window": Decimal("15"),
                    "sales_qty_window_medium": Decimal("6"),
                    "sales_qty_window_short": Decimal("0"),
                }
            },
            "returns": {},
            "days_in_sale": {
                "RB-SLOW-FLAT": {30: Decimal("11"), 90: Decimal("20"), 180: Decimal("50")}
            },
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="slow",
                min_group_avg_daily_sales_qty=Decimal("0"),
                review_only=True,
                label_ru="медленная группа",
            ),
        ),
    )

    assert rows[0]["speed_tier"] == "slow"
    assert rows[0]["sales_speed_trend"] == "flat_or_slowing"
    assert rows[0]["dry_run_decision"] == "manual_review"
    assert rows[0]["recommended_order_qty"] == "0"
    assert "pension_candidate_flat_despite_availability" in rows[0]["warnings"]
    assert "Пенсию" in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_stockout_guard_flags_do_not_order_with_short_runway() -> None:
    # off_schedule_signal_policy.stockout_guard (2026-07-31): тир "fast" с
    # коротким горизонтом (20 дней покрытия + 5 страховки) решает "заказ не
    # нужен" (свободно 50 >= цель 27), но честного остатка времени (50 дней
    # при скорости 1/день) меньше полного цикла довоза (48 путь + 7 полка +
    # 10 буфер = 65 дней) - должна загореться тревога, без изменения самого
    # recommended_order_qty (v1 - сигнал, не автозаказ).
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-FAST-SHORT-RUNWAY",
                "name": "Дисплей ходовой, но горизонт короче пути поставки",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB-FAST-SHORT-RUNWAY": {"sellable_stock_qty": Decimal("50")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-FAST-SHORT-RUNWAY": {"sales_qty_window": Decimal("180")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        supplier_prepare_days=30,
        logistics_days=18,
        distribution_to_shelf_days=7,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="fast",
                min_group_avg_daily_sales_qty=Decimal("0.8"),
                max_effective_target_days=20,
                safety_stock_days=5,
                label_ru="ходовая группа",
            ),
        ),
    )

    assert rows[0]["speed_tier"] == "fast"
    assert rows[0]["dry_run_decision"] == "do_not_order"
    assert rows[0]["recommended_order_qty"] == "0"
    assert rows[0]["stockout_guard_triggered"] == "true"
    assert Decimal(rows[0]["stockout_guard_days_remaining"]) < Decimal(
        rows[0]["stockout_guard_required_days"]
    )
    assert "stockout_guard_triggered" in rows[0]["warnings"]
    assert "ТРЕВОГА (stockout_guard)" in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_slow_group_accelerating_gets_starter_order() -> None:
    # Решение 2026-07-31: медленная карточка с растущей скоростью
    # (sales_speed_trend="accelerating") тоже не зануляется, даже если
    # остаток выше структурного пола - спрос реально растёт, ручной review
    # не должен тормозить заказ.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SLOW-GROWING",
                "name": "Дисплей медленный, но растёт",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "quality_raw": "Medium",
            }
        ],
        facts={
            # Остаток 15 шт - выше структурного пола (13), чтобы проверить
            # именно исключение по растущей скорости, а не по полу.
            "stock": {"RB-SLOW-GROWING": {"sellable_stock_qty": Decimal("15")}},
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-SLOW-GROWING": {
                    # rate_long=18/180=0.1, rate_medium=12/90=0.1333 (+33%),
                    # rate_short=6/30=0.2 (+50%) - оба шага выше порога 20%
                    # (ACCELERATING_MIN_GROWTH_MULTIPLIER).
                    "sales_qty_window": Decimal("18"),
                    "sales_qty_window_medium": Decimal("12"),
                    "sales_qty_window_short": Decimal("6"),
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=90,
        safety_stock_days=14,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="slow",
                min_group_avg_daily_sales_qty=Decimal("0"),
                review_only=True,
                label_ru="медленная группа",
            ),
        ),
    )

    assert rows[0]["speed_tier"] == "slow"
    assert rows[0]["sales_speed_trend"] == "accelerating"
    assert rows[0]["dry_run_decision"] == "order"
    assert Decimal(rows[0]["recommended_order_qty"]) > 0
    assert "speed_tier_accelerating_override" in rows[0]["warnings"]


# test_display_auto_order_dry_run_moves_order_to_better_analog удалён
# 2026-07-31: проверял перенос потребности "проигравшей" карточки на
# "победителя" группы аналогов - ровно та консолидация, что отменена и
# удалена из кода (раздел 4). Каждая карточка теперь считается независимо,
# переноса заказа между карточками больше не существует.


def test_display_auto_order_dry_run_blocks_order_for_review_only_sale_row() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SALE",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный) (Medium)",
                "status": "sale",
                "status_label": "Продажа",
                "auto_order_allowed": False,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A125 Galaxy A12",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {"RB-SALE": {"sellable_stock_qty": Decimal("0")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-SALE": {"sales_qty_window": Decimal("180")}},
            "returns": {},
            "purchase": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        max_order_qty=5,
    )

    assert rows[0]["dry_run_decision"] == "manual_review"
    assert rows[0]["recommended_order_qty"] == "0"
    assert rows[0]["analog_group_recommended_order_qty"] == "5"
    assert "not_auto_order_allowed" in rows[0]["warnings"]


def test_price_batch_applies_independently_to_both_cards_sharing_analog_tokens() -> None:
    # Раньше "поддерживаемые аналоги" (одобрено 2026-07-11) решали проблему
    # "проигравший цветовой вариант обнуляется навсегда, держим ему сетевой
    # минимум" - консолидация по аналогам отменена 2026-07-31 (раздел 4),
    # самой проблемы больше нет: каждая карточка (включая цветовой вариант)
    # считается независимо и никогда не обнуляется группировкой. Найден и
    # исправлен реальный побочный баг: карточки, попадающие в одну группу по
    # токенам (используется другими механизмами - apply_b2b_final_order_
    # policies и т.п.), раньше молча ПРОПУСКАЛИ ценовое округление целиком.
    # Проверяем: обе карточки с общими токенами бренда/модели получают
    # ценовое округление НЕЗАВИСИМО, ни одна не пропущена.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "РБ000041515",
                "name": "Дисплей для Samsung T295 Galaxy Tab A 8.0 + тачскрин (черный)",
                "status": "working",
                "status_label": "Поддерживаем (Рабочий)",
                "auto_order_allowed": True,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung T295 Galaxy Tab A 8.0",
                "quality_raw": "Аналог",
                "price_segment": "mid_low",
            },
            {
                "nomenclature_code": "РБ000041516",
                "name": "Дисплей для Samsung T295 Galaxy Tab A 8.0 + тачскрин (белый)",
                "status": "working",
                "status_label": "Поддерживаем (Рабочий)",
                "auto_order_allowed": True,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung T295 Galaxy Tab A 8.0",
                "quality_raw": "Аналог",
                "price_segment": "mid_low",
            },
        ],
        facts={
            "stock": {
                "РБ000041515": {"sellable_stock_qty": Decimal("0")},
                "РБ000041516": {"sellable_stock_qty": Decimal("0")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "РБ000041515": {"sales_qty_window": Decimal("59")},
                "РБ000041516": {"sales_qty_window": Decimal("13")},
            },
            "returns": {},
            "purchase": {
                "РБ000041515": {"latest_purchase_price": Decimal("53")},
                "РБ000041516": {"latest_purchase_price": Decimal("53")},
            },
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="normal",
                min_group_avg_daily_sales_qty=Decimal("0.25"),
                max_effective_target_days=82,
                safety_stock_days=14,
            ),
        ),
        price_batch_rules=(
            PriceBatchRule(
                speed_tier="normal",
                price_segments=("economy", "mid_low"),
                minimum_batch_qty=10,
                max_automatic_excess_coverage_days=21,
            ),
        ),
        price_batch_applies_to_statuses=("sale", "working"),
        price_batch_applies_to_analog_roles=("single_sku",),
    )

    by_code = {row["nomenclature_code"]: row for row in rows}
    black = by_code["РБ000041515"]
    white = by_code["РБ000041516"]
    assert black["analog_role"] == "single_sku"
    assert white["analog_role"] == "single_sku"
    # Раньше вторая карточка группы молча пропускала блок ценового
    # округления целиком (real bug, см. Changelog 2026-07-31) - здесь
    # достаточно убедиться, что обе карточки реально прошли расчёт (не
    # осталась пустой/нулевой) с собственным, независимым количеством.
    assert black["dry_run_decision"] == "order"
    assert black["recommended_order_qty"] == "28"
    assert black["price_batch_decision"] == "rounded_to_price_minimum"
    assert white["dry_run_decision"] == "order"
    assert white["recommended_order_qty"] == "2"


def test_price_batch_rule_uses_stable_status_codes_not_display_labels() -> None:
    economy_rule = PriceBatchRule(
        speed_tier="normal",
        price_segments=("economy", "mid_low"),
        minimum_batch_qty=10,
        max_automatic_excess_coverage_days=21,
    )
    premium_rule = PriceBatchRule(
        speed_tier="normal",
        price_segments=("mid_high", "premium"),
    )
    rules = (economy_rule, premium_rule)

    sale_rule = _price_batch_rule_for_row(
        {
            "_assortment_status": "sale",
            "status_label": "Растим (ПРОДАЖА)",
            "analog_role": "single_sku",
            "speed_tier": "normal",
            "price_segment": "mid_low",
        },
        rules=rules,
        applies_to_statuses=("sale", "working"),
        applies_to_analog_roles=("single_sku",),
    )
    working_rule = _price_batch_rule_for_row(
        {
            "_assortment_status": "working",
            "status_label": "Поддерживаем (Рабочий)",
            "analog_role": "single_sku",
            "speed_tier": "normal",
            "price_segment": "premium",
        },
        rules=rules,
        applies_to_statuses=("sale", "working"),
        applies_to_analog_roles=("single_sku",),
    )

    assert sale_rule is economy_rule
    assert working_rule is premium_rule


def test_price_batch_excess_above_limit_goes_to_manual_review_with_exact_need() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-PRICE-REVIEW",
                "name": "Дисплей тестовый",
                "status": "working",
                "status_label": "Поддерживаем (Рабочий)",
                "auto_order_allowed": True,
                "quality_raw": "Аналог",
                "price_segment": "mid_low",
            }
        ],
        facts={
            "stock": {"RB-PRICE-REVIEW": {"sellable_stock_qty": Decimal("20")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-PRICE-REVIEW": {"sales_qty_window": Decimal("45")}},
            "returns": {},
        },
        source_errors={},
        target_days=82,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="normal",
                min_group_avg_daily_sales_qty=Decimal("0.25"),
                max_effective_target_days=82,
                safety_stock_days=14,
            ),
        ),
        price_batch_rules=(
            PriceBatchRule(
                speed_tier="normal",
                price_segments=("mid_low",),
                minimum_batch_qty=10,
                max_automatic_excess_coverage_days=21,
            ),
        ),
        price_batch_applies_to_statuses=("working",),
        price_batch_applies_to_analog_roles=("single_sku",),
    )

    row = rows[0]
    assert row["recommended_order_qty_raw"] == "1"
    assert row["recommended_order_qty"] == "1"
    assert row["dry_run_decision"] == "manual_review"
    assert row["price_batch_decision"] == "manual_review_excess"
    assert "price_batch_excess_manual_review" in row["warnings"]


# test_supported_slow_variant_shows_need_but_stays_manual_review удалён
# 2026-07-31: тестировал "поддерживаемые аналоги" (сетевой минимум для
# проигравшего цветового варианта) - механизм убран вместе с консолидацией
# по аналогам (раздел 4), сама проблема, которую он решал, больше не
# существует (см. apply_price_batch_policy).


def test_display_auto_order_dry_run_does_not_group_by_short_marketing_model_only() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-A326",
                "name": "Дисплей для Samsung A326 Galaxy A32 5G + тачскрин (черный) (Medium)",
                "status_label": "Рабочий",
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A326 Galaxy A32 5G",
                "quality_raw": "Medium",
            },
            {
                "nomenclature_code": "RB-A325",
                "name": "Дисплей для Samsung A325 Galaxy A32 4G + тачскрин (черный) (ORIG100)",
                "status_label": "Рабочий",
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A325 Galaxy A32 4G",
                "quality_raw": "ORIG100",
            },
        ],
        facts={
            "stock": {
                "RB-A326": {"sellable_stock_qty": Decimal("5")},
                "RB-A325": {"sellable_stock_qty": Decimal("5")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-A326": {"sales_qty_window": Decimal("30")},
                "RB-A325": {"sales_qty_window": Decimal("30")},
            },
            "returns": {},
            "purchase": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        max_order_qty=5,
    )

    assert {row["analog_role"] for row in rows} == {"single_sku"}
    assert all(row["analog_group_id"] == "" for row in rows)


def test_display_auto_order_dry_run_does_not_group_by_quality_token() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-A125",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный) (ORIG100)",
                "status_label": "Рабочий",
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A125 Galaxy A12",
                "quality_raw": "ORIG100",
            },
            {
                "nomenclature_code": "RB-J810",
                "name": "Дисплей для Samsung J810 Galaxy J8 + тачскрин (черный) (ORIG100)",
                "status_label": "Рабочий",
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung J810 Galaxy J8",
                "quality_raw": "ORIG100",
            },
        ],
        facts={
            "stock": {
                "RB-A125": {"sellable_stock_qty": Decimal("5")},
                "RB-J810": {"sellable_stock_qty": Decimal("5")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-A125": {"sales_qty_window": Decimal("30")},
                "RB-J810": {"sales_qty_window": Decimal("30")},
            },
            "returns": {},
            "purchase": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        max_order_qty=5,
    )

    assert {row["analog_role"] for row in rows} == {"single_sku"}
    assert all("orig100" not in row["analog_model_tokens"].casefold() for row in rows)


def test_display_auto_order_dry_run_extends_target_by_supplier_and_delivery_time() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB3",
                "name": "Display test 3",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
                "price_segment": "middle",
            }
        ],
        facts={
            "stock": {"RB3": {"sellable_stock_qty": Decimal("5")}},
            "reserve": {},
            "incoming": {"RB3": {"incoming_qty": Decimal("4"), "incoming_order_count": 1}},
            "sales": {"RB3": {"sales_qty_window": Decimal("90")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        supplier_assembly_days=7,
        delivery_days=14,
        sales_window_days=180,
        max_order_qty=50,
    )

    assert rows[0]["target_days"] == 14
    assert rows[0]["supplier_assembly_days"] == 7
    assert rows[0]["delivery_days"] == 14
    assert rows[0]["effective_target_days"] == 35
    assert rows[0]["target_stock_qty"] == "18"
    assert rows[0]["recommended_order_qty_raw"] == "9"
    assert rows[0]["recommended_order_qty"] == "9"
    assert "покрытие 14 + сборка 7 + доставка 14" in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_extends_target_by_distribution_to_shelf_days() -> None:
    # Найдено 2026-07-30: дата поступления заказа поставщику (_Fld2493) - это
    # дата приемки на центральном узле, не дата на полке точки продаж; между
    # ними ещё ~7 дней распределения (см. logistics_transfer_assistant_pickup_
    # hold_days в app/core/config.py). Без этого слагаемого цель заказа
    # занижена и в реальности приводит к более долгому простою полки.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-SHELF",
                "name": "Display shelf distribution test",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
                "price_segment": "middle",
            }
        ],
        facts={
            "stock": {"RB-SHELF": {"sellable_stock_qty": Decimal("5")}},
            "reserve": {},
            "incoming": {"RB-SHELF": {"incoming_qty": Decimal("4"), "incoming_order_count": 1}},
            "sales": {"RB-SHELF": {"sales_qty_window": Decimal("90")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        supplier_assembly_days=7,
        delivery_days=14,
        distribution_to_shelf_days=7,
        sales_window_days=180,
        max_order_qty=50,
    )

    assert rows[0]["distribution_to_shelf_days"] == 7
    assert rows[0]["effective_target_days"] == 42
    assert rows[0]["target_stock_qty"] == "21"
    assert rows[0]["recommended_order_qty_raw"] == "12"
    assert "на полку 7" in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_speed_uses_max_of_three_windows_when_accelerating() -> None:
    # Раздел 9.1 (п.2) спеки: карточка разгоняется (30д быстрее 90д быстрее
    # 180д) -> скорость = максимум из трёх окон, а не плоское 180-дневное
    # среднее. Реальный пример: РБ000064965, май 5 -> июнь 6 -> июль 11 шт.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-TREND-UP",
                "name": "Display accelerating trend",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-TREND-UP": {
                    "sales_qty_window": Decimal("90"),
                    "sales_qty_window_medium": Decimal("54"),
                    "sales_qty_window_short": Decimal("30"),
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    assert rows[0]["sales_speed_trend"] == "accelerating"
    assert rows[0]["avg_daily_sales_qty"] == "1"


def test_display_auto_order_dry_run_speed_uses_average_of_three_windows_when_not_accelerating() -> (
    None
):
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-TREND-FLAT",
                "name": "Display flat/slowing trend",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-TREND-FLAT": {
                    "sales_qty_window": Decimal("90"),
                    "sales_qty_window_medium": Decimal("63"),
                    "sales_qty_window_short": Decimal("9"),
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    assert rows[0]["sales_speed_trend"] == "flat_or_slowing"
    assert rows[0]["avg_daily_sales_qty"] == "0.5"


def test_display_auto_order_dry_run_speed_falls_back_to_flat_window_without_trend_data() -> None:
    # Обратная совместимость: старые вызовы/фикстуры без sales_qty_window_
    # medium/short не должны молча занижаться в 3 раза (0 в среднем с двумя
    # нулевыми окнами) - откат на прежнее плоское поведение.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-NO-TREND",
                "name": "No trend data available",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-NO-TREND": {"sales_qty_window": Decimal("90")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    assert rows[0]["sales_speed_trend"] == "n/a_flat_window_fallback"
    assert rows[0]["avg_daily_sales_qty"] == "0.5"


def test_display_auto_order_dry_run_blocks_order_when_no_non_marketplace_demand() -> None:
    # Раздел 2 + procurement-order-auto-order-unified-contour.md ("Разрезы
    # спроса по типу покупателя"): _Reference54._Fld619RRef -> _Reference23
    # "Маркетплейс". Обычного спроса нет (весь спрос - маркетплейс) и есть
    # остаток -> critical_marketplace_refusal_nonliquid_risk, автозаказ
    # останавливается, ручное решение.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-MP-CRIT",
                "name": "Display marketplace-only demand",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-MP-CRIT": {"sellable_stock_qty": Decimal("5"), "total_stock_qty": Decimal("5")}
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-MP-CRIT": {
                    "sales_qty_window": Decimal("100"),
                    "sales_qty_window_marketplace": Decimal("100"),
                    "sales_doc_count_marketplace": 20,
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    row = rows[0]
    assert row["marketplace_risk_code"] == "critical_marketplace_refusal_nonliquid_risk"
    assert row["marketplace_share_pct"] == "100"
    assert row["recommended_order_qty"] == "0"
    assert row["dry_run_decision"] == "manual_review"


def test_display_auto_order_dry_run_blocks_order_when_marketplace_share_is_high() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-MP-HIGH",
                "name": "Display high marketplace share",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-MP-HIGH": {"sellable_stock_qty": Decimal("5"), "total_stock_qty": Decimal("5")}
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-MP-HIGH": {
                    "sales_qty_window": Decimal("100"),
                    "sales_qty_window_marketplace": Decimal("60"),
                    "sales_doc_count_marketplace": 15,
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    row = rows[0]
    assert row["marketplace_risk_code"] == "high_marketplace_refusal_risk"
    assert row["marketplace_share_pct"] == "60"
    assert row["recommended_order_qty"] == "0"
    assert row["dry_run_decision"] == "manual_review"


def test_display_auto_order_dry_run_medium_marketplace_share_labels_but_does_not_block() -> None:
    # 30-50% с минимум 7 продажами маркетплейса: магазинная + маркетплейсная
    # потребность складываются автоматически (уточнение 2026-07-25) - метка
    # только для прозрачности, количество заказа НЕ меняется.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-MP-MED",
                "name": "Display medium marketplace share",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-MP-MED": {"sellable_stock_qty": Decimal("0"), "total_stock_qty": Decimal("0")}
            },
            "reserve": {},
            "incoming": {"RB-MP-MED": {"incoming_qty": Decimal("1"), "incoming_order_count": 1}},
            "sales": {
                "RB-MP-MED": {
                    "sales_qty_window": Decimal("100"),
                    "sales_qty_window_marketplace": Decimal("40"),
                    "sales_doc_count_marketplace": 10,
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    row = rows[0]
    assert row["marketplace_risk_code"] == "medium_channel_split_required"
    assert row["marketplace_share_pct"] == "40"
    assert row["recommended_order_qty_raw"] == "7"
    assert row["recommended_order_qty"] == "7"
    assert row["dry_run_decision"] == "order"


def test_display_auto_order_dry_run_low_marketplace_share_has_no_risk_label() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-MP-LOW",
                "name": "Display low marketplace share",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-MP-LOW": {"sellable_stock_qty": Decimal("0"), "total_stock_qty": Decimal("0")}
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-MP-LOW": {
                    "sales_qty_window": Decimal("100"),
                    "sales_qty_window_marketplace": Decimal("2"),
                    "sales_doc_count_marketplace": 1,
                }
            },
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    row = rows[0]
    assert row["marketplace_risk_code"] == ""
    assert row["marketplace_share_pct"] == "2"
    assert row["dry_run_decision"] == "order"


def test_display_auto_order_dry_run_stops_order_on_suspected_batch_error() -> None:
    # Раздел 5.1: ранний триггер партийной ошибки - >=5 шт возвратов
    # качества "Новый" за 90 дней И доля от продаж за то же окно >=40% ->
    # автозаказ останавливается немедленно, карточка на ручную проверку.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-BATCH-ERR",
                "name": "Display suspected batch error",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-BATCH-ERR": {
                    "sellable_stock_qty": Decimal("20"),
                    "total_stock_qty": Decimal("20"),
                }
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-BATCH-ERR": {
                    "sales_qty_window": Decimal("100"),
                    "sales_qty_window_medium": Decimal("50"),
                    "sales_qty_window_short": Decimal("20"),
                }
            },
            "returns": {"RB-BATCH-ERR": {"batch_error_return_qty": Decimal("25")}},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    row = rows[0]
    assert row["batch_error_suspected"] == "yes"
    assert row["batch_error_return_qty"] == "25"
    assert row["batch_error_share_pct"] == "50"
    assert row["recommended_order_qty"] == "0"
    assert row["dry_run_decision"] == "manual_review"
    assert "ТРЕВОГА" in row["reason_ru"]
    assert "batch_error_suspected" in row["blockers"]


def test_display_auto_order_dry_run_batch_error_needs_both_qty_and_share_thresholds() -> None:
    def _build(return_qty: str, sales_medium: str) -> dict:
        rows = build_dry_run_rows(
            [
                {
                    "nomenclature_code": "RB-BATCH-OK",
                    "name": "Display normal return rate",
                    "status_label": "Рабочий",
                    "quality_raw": "Medium",
                }
            ],
            facts={
                "stock": {
                    "RB-BATCH-OK": {
                        "sellable_stock_qty": Decimal("20"),
                        "total_stock_qty": Decimal("20"),
                    }
                },
                "reserve": {},
                "incoming": {},
                "sales": {
                    "RB-BATCH-OK": {
                        "sales_qty_window": Decimal("100"),
                        "sales_qty_window_medium": Decimal(sales_medium),
                        "sales_qty_window_short": Decimal("20"),
                    }
                },
                "returns": {"RB-BATCH-OK": {"batch_error_return_qty": Decimal(return_qty)}},
            },
            source_errors={},
            target_days=14,
            sales_window_days=180,
        )
        return rows[0]

    # Доля высокая (60%), но штук меньше порога (3 < 5) - не срабатывает.
    below_qty_floor = _build("3", "5")
    assert below_qty_floor["batch_error_suspected"] == ""

    # Штук достаточно (6 >= 5), но доля низкая (6%) - не срабатывает.
    below_share_floor = _build("6", "100")
    assert below_share_floor["batch_error_suspected"] == ""


def _defect_rate_row(
    *,
    defect_qty: str,
    sales_medium: str,
    batch_error_qty: str = "0",
) -> dict:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-DEFECT",
                "name": "Display defect rate",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-DEFECT": {
                    "sellable_stock_qty": Decimal("2"),
                    "total_stock_qty": Decimal("2"),
                }
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-DEFECT": {
                    "sales_qty_window": Decimal("300"),
                    "sales_qty_window_medium": Decimal(sales_medium),
                    "sales_qty_window_short": Decimal("20"),
                }
            },
            "returns": {
                "RB-DEFECT": {
                    "defect_return_qty": Decimal(defect_qty),
                    "batch_error_return_qty": Decimal(batch_error_qty),
                }
            },
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )
    return rows[0]


def test_display_auto_order_dry_run_high_defect_rate_stops_auto_order() -> None:
    # Возвраты качества "Брак" до 2026-08-01 не считались нигде: карточка
    # РБ000059304 с 11 бракованными из 51 проданной показывала
    # batch_error_share_pct = 0%. Раздел 5.1: подтверждённый брак выше порога
    # снимает карточку с автозаказа и уводит в ручную проверку.
    row = _defect_rate_row(defect_qty="6", sales_medium="50")

    assert row["defect_return_qty"] == "6"
    assert row["defect_share_pct"] == "12"
    assert row["defect_rate_suspected"] == "yes"
    assert "defect_rate_above_threshold" in row["warnings"]
    assert "ТРЕВОГА (качество)" in row["reason_ru"]
    assert "defect_rate_suspected" in row["blockers"]
    assert row["recommended_order_qty"] == "0"
    assert row["dry_run_decision"] == "manual_review"


def test_display_auto_order_dry_run_defect_rate_needs_both_thresholds() -> None:
    # Доля высокая (40%), но штук меньше порога (2 < 5) - не срабатывает.
    below_qty_floor = _defect_rate_row(defect_qty="2", sales_medium="5")
    assert below_qty_floor["defect_rate_suspected"] == ""
    assert "defect_rate_suspected" not in below_qty_floor["blockers"]

    # Штук достаточно (5 >= 5), но доля ниже порога (2.5% < 5%) - не срабатывает.
    below_share_floor = _defect_rate_row(defect_qty="5", sales_medium="200")
    assert below_share_floor["defect_rate_suspected"] == ""
    assert "defect_rate_suspected" not in below_share_floor["blockers"]


def test_display_auto_order_dry_run_defect_rate_is_independent_from_batch_error() -> None:
    # Два разных показателя на разном качестве возврата: "Брак" - претензия к
    # товару, "Новый" - признак пересорта. Они не должны перетекать друг в друга.
    defect_only = _defect_rate_row(defect_qty="6", sales_medium="50", batch_error_qty="0")
    assert defect_only["defect_rate_suspected"] == "yes"
    assert defect_only["batch_error_suspected"] == ""
    assert "defect_rate_suspected" in defect_only["blockers"]
    assert "batch_error_suspected" not in defect_only["blockers"]

    batch_error_only = _defect_rate_row(defect_qty="0", sales_medium="50", batch_error_qty="25")
    assert batch_error_only["batch_error_suspected"] == "yes"
    assert batch_error_only["defect_rate_suspected"] == ""
    assert batch_error_only["defect_share_pct"] == "0"
    assert "batch_error_suspected" in batch_error_only["blockers"]
    assert "defect_rate_suspected" not in batch_error_only["blockers"]


def test_display_auto_order_dry_run_speed_tier_does_not_undo_blocker_zeroed_order() -> None:
    # Найдено на реальном прогоне по всему каталогу 2026-07-31:
    # _apply_speed_horizon_rule безусловно пересчитывал recommended_order_qty
    # с нуля даже для строк с уже выставленным блокером (партийная ошибка,
    # маркетплейс-риск) - dry_run_decision оставался "manual_review"
    # правильно, но количество заказа тихо утекало обратно в ненулевое.
    # Проверяем на достаточно быстрой карточке (тир "normal"), чтобы реально
    # пройти через ветку пересчёта, а не review_only-ветку "slow" (там и так
    # принудительный 0, баг был бы незаметен).
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-BLOCKED-FAST",
                "name": "Display fast tier with batch error",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-BLOCKED-FAST": {
                    "sellable_stock_qty": Decimal("5"),
                    "total_stock_qty": Decimal("5"),
                }
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-BLOCKED-FAST": {
                    "sales_qty_window": Decimal("100"),
                    "sales_qty_window_medium": Decimal("50"),
                    "sales_qty_window_short": Decimal("20"),
                }
            },
            "returns": {"RB-BLOCKED-FAST": {"batch_error_return_qty": Decimal("25")}},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="normal",
                min_group_avg_daily_sales_qty=Decimal("0.25"),
                max_effective_target_days=82,
                safety_stock_days=14,
                label_ru="обычная группа",
            ),
        ),
    )

    row = rows[0]
    assert row["speed_tier"] == "normal"
    assert row["batch_error_suspected"] == "yes"
    assert row["recommended_order_qty"] == "0"
    assert row["recommended_order_qty_raw"] == "0"
    assert row["dry_run_decision"] == "manual_review"


def test_display_auto_order_dry_run_blocker_survives_price_batch_step_in_shared_token_group() -> (
    None
):
    # Найдено на реальном прогоне по всему каталогу 2026-07-31: карточка с
    # блокером всё равно утекала ненулевым заказом дальше по конвейеру
    # (в apply_supported_analog_and_price_batch_policies/b2b-шагах, идущих
    # ПОСЛЕ тира скорости). Актуально и после удаления консолидации по
    # аналогам 2026-07-31 - _analog_groups по-прежнему используется другими
    # (не отменёнными) шагами конвейера, финальный барьер в конце
    # build_dry_run_rows должен ловить утечку независимо от того, делит ли
    # карточка токены бренда/модели с другой.
    items = [
        {
            "nomenclature_code": "RB-WINNER-BLOCKED",
            "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный) (ORIG100)",
            "status_label": "Рабочий",
            "brand_compatibility": "Samsung",
            "model_compatibility": "Samsung A125 Galaxy A12",
            "quality_raw": "ORIG100",
        },
        {
            "nomenclature_code": "RB-LOSER-CLEAN",
            "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный) (Medium)",
            "status_label": "Рабочий",
            "brand_compatibility": "Samsung",
            "model_compatibility": "Samsung A125 Galaxy A12",
            "quality_raw": "Medium",
        },
    ]
    rows = build_dry_run_rows(
        items,
        facts={
            "stock": {
                "RB-WINNER-BLOCKED": {
                    "sellable_stock_qty": Decimal("5"),
                    "total_stock_qty": Decimal("5"),
                },
                "RB-LOSER-CLEAN": {
                    "sellable_stock_qty": Decimal("0"),
                    "total_stock_qty": Decimal("0"),
                },
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-WINNER-BLOCKED": {
                    "sales_qty_window": Decimal("360"),
                    "sales_qty_window_medium": Decimal("180"),
                    "sales_qty_window_short": Decimal("60"),
                    "sales_doc_count": 40,
                },
                "RB-LOSER-CLEAN": {
                    "sales_qty_window": Decimal("36"),
                    "sales_qty_window_medium": Decimal("18"),
                    "sales_qty_window_short": Decimal("6"),
                    "sales_doc_count": 4,
                },
            },
            "returns": {
                "RB-WINNER-BLOCKED": {"batch_error_return_qty": Decimal("90")},
            },
            "purchase": {
                "RB-WINNER-BLOCKED": {"latest_purchase_price": Decimal("35")},
                "RB-LOSER-CLEAN": {"latest_purchase_price": Decimal("32")},
            },
        },
        source_errors={},
        target_days=14,
        order_cadence_days=7,
        supplier_prepare_days=18,
        logistics_days=30,
        supplier_delay_buffer_days=3,
        receiving_buffer_days=1,
        safety_stock_days=25,
        sales_window_days=180,
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="super_fast",
                min_group_avg_daily_sales_qty=Decimal("2"),
                max_effective_target_days=60,
                safety_stock_days=7,
                label_ru="супер-ходовая группа",
            ),
        ),
    )

    blocked = next(row for row in rows if row["nomenclature_code"] == "RB-WINNER-BLOCKED")
    assert blocked["analog_role"] == "single_sku"
    assert blocked["batch_error_suspected"] == "yes"
    assert "batch_error_suspected" in blocked["blockers"]
    assert blocked["recommended_order_qty"] == "0"
    assert blocked["recommended_order_qty_raw"] == "0"
    assert blocked["dry_run_decision"] == "manual_review"


def test_display_auto_order_dry_run_zero_sales_without_marketplace_gets_no_marketplace_label() -> (
    None
):
    # Найдено на реальном прогоне по всему каталогу 2026-07-31: карточки без
    # ПРОДАЖ ВООБЩЕ (не только без маркетплейса) попадали под
    # critical_marketplace_refusal_nonliquid_risk просто потому, что
    # non_marketplace_qty <= 0 - хотя маркетплейса там не было ни одной
    # штуки. Это не маркетплейс-риск, а обычный "нет продаж", уже покрытый
    # no_recent_net_sales. Исправлено: маркетплейс-метка требует
    # marketplace_net_sales_qty > 0.
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-NO-SALES",
                "name": "Display with stock but zero sales anywhere",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
            }
        ],
        facts={
            "stock": {
                "RB-NO-SALES": {
                    "sellable_stock_qty": Decimal("5"),
                    "total_stock_qty": Decimal("5"),
                }
            },
            "reserve": {},
            "incoming": {},
            "sales": {"RB-NO-SALES": {"sales_qty_window": Decimal("0")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
    )

    row = rows[0]
    assert row["marketplace_risk_code"] == ""
    assert "no_recent_net_sales" in row["warnings"]


def test_display_auto_order_dry_run_uses_full_regular_order_horizon_and_pipeline() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB4",
                "name": "Display test 4",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
                "price_segment": "middle",
            }
        ],
        facts={
            "stock": {"RB4": {"sellable_stock_qty": Decimal("5")}},
            "reserve": {},
            "incoming": {
                "RB4": {
                    "incoming_qty": Decimal("4"),
                    "incoming_order_count": 2,
                    "latest_expected_receipt_at": "2026-07-15T00:00:00",
                    "pipeline_arriving_10_days_qty": Decimal("1"),
                    "pipeline_arriving_20_days_qty": Decimal("2"),
                    "pipeline_later_qty": Decimal("1"),
                    "pipeline_cargo_handoff_qty": Decimal("3"),
                    "pipeline_supplier_processing_qty": Decimal("1"),
                }
            },
            "sales": {"RB4": {"sales_qty_window": Decimal("90")}},
            "returns": {},
        },
        source_errors={},
        target_days=14,
        order_cadence_days=7,
        supplier_prepare_days=7,
        logistics_days=14,
        supplier_delay_buffer_days=3,
        receiving_buffer_days=1,
        safety_stock_days=2,
        sales_window_days=180,
        max_order_qty=50,
        as_of=date(2026, 7, 4),
    )

    assert rows[0]["lead_time_days"] == 21
    assert rows[0]["effective_target_days"] == 48
    assert rows[0]["forecast_qty"] == "23"
    assert rows[0]["safety_stock_qty"] == "1"
    assert rows[0]["target_stock_qty"] == "24"
    assert rows[0]["recommended_order_qty_raw"] == "15"
    assert rows[0]["pipeline_arriving_10_days_qty"] == "1"
    assert rows[0]["pipeline_arriving_20_days_qty"] == "2"
    assert rows[0]["pipeline_later_qty"] == "1"
    assert rows[0]["pipeline_cargo_handoff_qty"] == "3"
    assert rows[0]["pipeline_supplier_processing_qty"] == "1"
    assert rows[0]["incoming_latest_arrival_days"] == "11"
    assert "график 7 + сборка 7 + доставка 14 + буфер 3 + приемка 1" in rows[0]["reason_ru"]


def test_display_auto_order_dry_run_does_not_deduct_returns_from_demand_base() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-GROSS",
                "name": "Display test gross demand",
                "status_label": "Рабочий",
                "quality_raw": "Medium",
                "price_segment": "middle",
            }
        ],
        facts={
            "stock": {"RB-GROSS": {"sellable_stock_qty": Decimal("0")}},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-GROSS": {"sales_qty_window": Decimal("90")}},
            "returns": {"RB-GROSS": {"return_qty_window": Decimal("40")}},
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        max_order_qty=50,
    )

    assert rows[0]["net_sales_qty_window"] == "90"
    assert rows[0]["return_qty_window"] == "40"
    assert rows[0]["target_stock_qty"] == "7"
    assert rows[0]["recommended_order_qty"] == "7"


def test_load_auto_order_policy_reads_nested_display_policy(tmp_path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(
        """{
          "auto_order_policy": {
            "sales_window_days": 90,
            "target_days": 21,
            "order_cadence_days": 7,
            "supplier_prepare_days": 5,
            "logistics_days": 12,
            "supplier_delay_buffer_days": 2,
            "receiving_buffer_days": 1,
            "safety_stock_days": 3,
            "min_display_qty": 2,
            "min_order_qty": 1,
            "max_order_qty": 8,
            "include_sale_review_candidates": true,
            "order_rounding_rules": [
              {"threshold_gt": 2000, "round_to": 100},
              {"threshold_gt": 1000, "round_to": 50},
              {"threshold_gt": 100, "round_to": 10}
            ],
            "speed_horizon_rules": [
              {
                "tier": "super_fast",
                "label_ru": "супер-ходовая группа",
                "min_group_avg_daily_sales_qty": 2,
                "max_effective_target_days": 60,
                "safety_stock_days": 7
              },
              {
                "tier": "slow",
                "label_ru": "медленная группа",
                "min_group_avg_daily_sales_qty": 0,
                "review_only": true
              }
            ],
            "onec_catalog_analog_candidate_model_tokens": [
              "apple:model:iphone 11"
            ],
            "demand_uplift_rules": [
              {
                "rule_id": "iphone11_uplift",
                "match_any_analog_model_tokens": [
                  "apple:model:iphone 11"
                ],
                "demand_multiplier": 1.25,
                "reason_ru": "тестовое правило"
              }
            ]
          }
        }""",
        encoding="utf-8",
    )

    policy = load_auto_order_policy(path)

    assert policy.sales_window_days == 90
    assert policy.target_days == 21
    assert policy.order_cadence_days == 7
    assert policy.supplier_prepare_days == 5
    assert policy.logistics_days == 12
    assert policy.lead_time_days == 17
    assert policy.effective_target_days == 51
    assert policy.min_display_qty == 2
    assert policy.min_order_qty == 1
    assert policy.max_order_qty == 8
    assert policy.include_sale_review_candidates is True
    assert [rule.round_to for rule in policy.order_rounding_rules] == [100, 50, 10]
    assert [rule.threshold_gt for rule in policy.order_rounding_rules] == [
        Decimal("2000"),
        Decimal("1000"),
        Decimal("100"),
    ]
    assert [rule.tier for rule in policy.speed_horizon_rules] == ["super_fast", "slow"]
    assert policy.speed_horizon_rules[0].max_effective_target_days == 60
    assert policy.speed_horizon_rules[0].safety_stock_days == 7
    assert policy.speed_horizon_rules[1].review_only is True
    assert policy.onec_catalog_analog_candidate_model_tokens == ("apple:model:iphone 11",)
    assert len(policy.demand_uplift_rules) == 1
    assert policy.demand_uplift_rules[0].rule_id == "iphone11_uplift"
    assert policy.demand_uplift_rules[0].demand_multiplier == Decimal("1.25")


def test_versioned_price_batch_policy_uses_stable_status_codes() -> None:
    policy = load_auto_order_policy(DEFAULT_POLICY_JSON)

    assert policy.price_batch_applies_to_statuses == ("sale", "working")


# --- Журнал выпавших карточек (gate audit) ---------------------------------


def _classification_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.services.assortment_lifecycle_classification_store import (
        ASSORTMENT_LIFECYCLE_METADATA,
    )

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    return engine


def _classification_row(code: str, **overrides):
    row = {
        "nomenclature_code": code,
        "name": f"Дисплей {code}",
        "folder": "Товары/дисплеи",
        "status": "sale",
        "status_label": "Растим",
        "article": code[-6:],
        "auto_order_allowed": True,
        "manual_review_required": False,
        "future_ka_mapping_status": "ready",
        "demand_method_code": "available_days_average",
        "last_run_id": 2,
        "source_hash": f"hash-{code}",
        "source": "test",
        "classified_at": datetime(2026, 8, 18, 9, 20),
    }
    row.update(overrides)
    return row


def _insert_classification_rows(engine, rows):
    from app.services.assortment_lifecycle_classification_store import (
        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE as table,
    )

    with engine.begin() as conn:
        conn.execute(table.insert(), list(rows))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ()),
        ({"status": "fruit"}, ("gate_status_not_eligible",)),
        ({"status": "new_item"}, ("gate_status_not_eligible",)),
        ({"demand_method_code": "manual_review"}, ("gate_demand_method_not_supported",)),
        ({"future_ka_mapping_status": "needs_mapping"}, ("gate_future_ka_mapping_not_ready",)),
        ({"manual_review_required": True}, ("gate_manual_review_required",)),
        (
            {"status": "fruit", "demand_method_code": "manual_review"},
            ("gate_status_not_eligible", "gate_demand_method_not_supported"),
        ),
    ],
)
def test_auto_order_scope_gate_reasons_match_previous_sql_gate(overrides, expected) -> None:
    record = _classification_row("РБ000000001", **overrides)

    reasons = auto_order_scope_gate_reasons(record, include_sale_review_candidates=True)

    assert reasons == expected


def test_auto_order_scope_gate_reasons_for_auto_order_allowed_branch() -> None:
    record = _classification_row("РБ000000001", status="sale", auto_order_allowed=False)

    reasons = auto_order_scope_gate_reasons(record, include_sale_review_candidates=False)

    # Ветка без sale-кандидатов повторяет старый SQL: status == "working" и
    # auto_order_allowed is True.
    assert reasons == ("gate_status_not_eligible", "gate_auto_order_not_allowed")


def test_load_auto_order_items_records_every_gate_reason() -> None:
    engine = _classification_engine()
    _insert_classification_rows(
        engine,
        [
            _classification_row("РБ000000001"),
            _classification_row("РБ000000002", status="fruit"),
            _classification_row("РБ000000003", demand_method_code="manual_review"),
            _classification_row("РБ000000004", future_ka_mapping_status="needs_mapping"),
            _classification_row("РБ000000005", manual_review_required=True),
            _classification_row("РБ000000006", last_run_id=1),
            _classification_row("РБ000000007", name="Дисплей биток"),
        ],
    )

    items, run_id, scope_audit, gate_audit = load_auto_order_items_with_scope_audit(
        engine,
        folder="дисплеи",
        include_sale_review_candidates=True,
    )

    assert run_id == 2
    assert [item["nomenclature_code"] for item in items] == ["РБ000000001"]
    assert gate_audit["scope_gate_version"] == "display_auto_order_scope_gate.v1"
    assert gate_audit["previous_run_id"] == 1
    assert gate_audit["source_item_count"] == 7
    assert gate_audit["included_item_count"] == 1
    assert gate_audit["excluded_reason_counts"] == {
        "gate_demand_method_not_supported": 1,
        "gate_future_ka_mapping_not_ready": 1,
        "gate_manual_review_required": 1,
        "gate_missing_in_latest_classification_run": 1,
        "gate_status_not_eligible": 1,
    }
    # Карточка вне скоупа по имени остаётся в своей политике и не дублируется в журнале.
    assert scope_audit["excluded_reason_counts"] == {"excluded_display_name_bitok": 1}
    assert all(row["nomenclature_code"] != "РБ000000007" for row in gate_audit["exclusions"])
    disappeared = next(
        row for row in gate_audit["exclusions"] if row["nomenclature_code"] == "РБ000000006"
    )
    assert disappeared["gate_reason_code"] == "gate_missing_in_latest_classification_run"
    assert disappeared["gate_reason_label_ru"] == "карточки нет в последнем прогоне классификации"


def test_load_auto_order_items_keeps_calculation_scope_unchanged() -> None:
    engine = _classification_engine()
    _insert_classification_rows(
        engine,
        [
            _classification_row("РБ000000001", status="working"),
            _classification_row("РБ000000002", status="sale"),
            _classification_row("РБ000000003", status="pension"),
        ],
    )

    items, _run_id, _scope_audit, gate_audit = load_auto_order_items_with_scope_audit(
        engine,
        folder="дисплеи",
        include_sale_review_candidates=True,
    )

    assert [item["nomenclature_code"] for item in items] == ["РБ000000001", "РБ000000002"]
    assert gate_audit["excluded_status_counts"] == {"pension": 1}


def test_write_scope_gate_csv_lists_reason_per_card(tmp_path) -> None:
    engine = _classification_engine()
    _insert_classification_rows(
        engine,
        [
            _classification_row("РБ000000001"),
            _classification_row(
                "РБ000000002",
                status="fruit",
                status_label="Рассматриваем",
                demand_method_code="manual_review",
            ),
        ],
    )
    _items, _run_id, _scope_audit, gate_audit = load_auto_order_items_with_scope_audit(
        engine,
        folder="дисплеи",
        include_sale_review_candidates=True,
    )

    path = write_scope_gate_csv(tmp_path / "gate.csv", gate_audit["exclusions"])

    import csv as csv_module

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv_module.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["nomenclature_code"] == "РБ000000002"
    assert rows[0]["gate_reason_code"] == "gate_status_not_eligible"
    assert rows[0]["gate_reason_codes"] == (
        "gate_status_not_eligible, gate_demand_method_not_supported"
    )
    assert rows[0]["status_label"] == "Рассматриваем"


def test_build_summary_exposes_scope_gate_audit() -> None:
    gate_audit = build_auto_order_scope_gate_audit(
        [
            {
                "nomenclature_code": "РБ000000002",
                "gate_reason_code": "gate_status_not_eligible",
                "status": "fruit",
            }
        ],
        source_item_count=2,
        included_item_count=1,
        run_id=7,
    )

    summary = build_summary(
        [],
        run_id=7,
        source_errors={},
        scope_gate_audit=gate_audit,
    )

    assert summary["auto_order_scope_gate"]["excluded_item_count"] == 1
    assert summary["auto_order_scope_gate"]["excluded_reason_counts"] == {
        "gate_status_not_eligible": 1
    }


def _rounding_gate_filler_items(
    *,
    prefix: str,
    brand: str,
    name_prefix: str,
    prices: Sequence[str],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    """Карточки-наполнители группы: нужны только чтобы медиана считалась."""

    items: list[dict[str, object]] = []
    purchase: dict[str, dict[str, object]] = {}
    for index, price in enumerate(prices):
        code = f"{prefix}-{index}"
        items.append(
            {
                "nomenclature_code": code,
                "name": f"{name_prefix} {index}",
                "status_label": "Рабочий",
                "quality_raw": "ORIG",
                "price_segment": "mid_low",
                "brand_compatibility": brand,
            }
        )
        purchase[code] = {"latest_purchase_price": Decimal(price)}
    return items, purchase


def test_order_rounding_ladder_from_config_uses_five_ten_fifty_hundred_steps() -> None:
    policy = load_auto_order_policy(DEFAULT_POLICY_JSON)
    rules = policy.order_rounding_rules

    def rounded(value: str) -> Decimal:
        return rounded_order_qty(
            Decimal(value),
            min_order_qty=1,
            max_order_qty=None,
            order_rounding_rules=rules,
        )

    assert rounded("1") == Decimal("5")
    assert rounded("23") == Decimal("25")
    assert rounded("49") == Decimal("50")
    assert rounded("50") == Decimal("50")
    assert rounded("51") == Decimal("60")
    assert rounded("371") == Decimal("380")
    assert rounded("501") == Decimal("510")
    # Границы относятся к верхнему шагу; на самих границах результат совпадает
    # при любом прочтении, потому что 1000 кратно 10 и 50, а 2000 - 50 и 100.
    assert rounded("1000") == Decimal("1000")
    assert rounded("1001") == Decimal("1050")
    assert rounded("2000") == Decimal("2000")
    assert rounded("2001") == Decimal("2100")


def test_order_rounding_price_gate_blocks_card_above_group_median() -> None:
    items, purchase = _rounding_gate_filler_items(
        prefix="RB-FILL",
        brand="Apple",
        name_prefix="Дисплей для Apple iPhone фон",
        prices=["10", "20", "30", "40", "50", "70", "80", "90", "100"],
    )
    items.extend(
        [
            {
                "nomenclature_code": "RB-CHEAP",
                "name": "Дисплей для Apple iPhone 13 + тачскрин",
                "status_label": "Рабочий",
                "quality_raw": "ORIG",
                "price_segment": "mid_low",
                "brand_compatibility": "Apple",
            },
            {
                "nomenclature_code": "RB-EXPENSIVE",
                "name": "Дисплей для Apple iPhone 15 Pro Max + тачскрин",
                "status_label": "Рабочий",
                "quality_raw": "ORIG",
                "price_segment": "mid_low",
                "brand_compatibility": "Apple",
            },
        ]
    )
    purchase["RB-CHEAP"] = {"latest_purchase_price": Decimal("15")}
    purchase["RB-EXPENSIVE"] = {"latest_purchase_price": Decimal("500")}

    rows = build_dry_run_rows(
        items,
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-CHEAP": {"sales_qty_window": Decimal("180")},
                "RB-EXPENSIVE": {"sales_qty_window": Decimal("180")},
            },
            "returns": {},
            "purchase": purchase,
        },
        source_errors={},
        target_days=23,
        sales_window_days=180,
        order_rounding_rules=(
            OrderRoundingRule(threshold_gt=Decimal("49"), round_to=10),
            OrderRoundingRule(threshold_gt=Decimal("0"), round_to=5),
        ),
        order_rounding_price_gate=OrderRoundingPriceGatePolicy(enabled=True, min_group_size=10),
    )
    by_code = {row["nomenclature_code"]: row for row in rows}

    cheap = by_code["RB-CHEAP"]
    assert cheap["recommended_order_qty_raw"] == "23"
    assert cheap["recommended_order_qty"] == "25"
    assert cheap["order_rounding_price_gate"] == "at_or_below_median"
    assert cheap["order_rounding_price_group"] == "apple / телефон"
    assert cheap["order_rounding_group_median_price"] == "50"

    expensive = by_code["RB-EXPENSIVE"]
    assert expensive["recommended_order_qty_raw"] == "23"
    assert expensive["recommended_order_qty"] == "23"
    assert expensive["order_rounding_price_gate"] == "above_median"


def test_order_rounding_price_gate_rounds_small_group_by_ten_without_price_condition() -> None:
    items, purchase = _rounding_gate_filler_items(
        prefix="RB-SMALL",
        brand="Garmin",
        name_prefix="Дисплей для Garmin Forerunner фон",
        prices=["10", "20"],
    )
    items.append(
        {
            "nomenclature_code": "RB-SMALL-MAIN",
            "name": "Дисплей для Garmin Forerunner 965",
            "status_label": "Рабочий",
            "quality_raw": "ORIG",
            "price_segment": "premium",
            "brand_compatibility": "Garmin",
        }
    )
    purchase["RB-SMALL-MAIN"] = {"latest_purchase_price": Decimal("9000")}

    rows = build_dry_run_rows(
        items,
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-SMALL-MAIN": {"sales_qty_window": Decimal("180")}},
            "returns": {},
            "purchase": purchase,
        },
        source_errors={},
        target_days=23,
        sales_window_days=180,
        order_rounding_rules=(
            OrderRoundingRule(threshold_gt=Decimal("49"), round_to=10),
            OrderRoundingRule(threshold_gt=Decimal("0"), round_to=5),
        ),
        order_rounding_price_gate=OrderRoundingPriceGatePolicy(enabled=True, min_group_size=10),
    )
    row = {row["nomenclature_code"]: row for row in rows}["RB-SMALL-MAIN"]

    assert row["order_rounding_price_gate"] == "small_group"
    assert row["order_rounding_group_median_price"] == ""
    assert row["recommended_order_qty_raw"] == "23"
    assert row["recommended_order_qty"] == "30"


def test_order_rounding_price_gate_skips_card_without_purchase_price() -> None:
    items, purchase = _rounding_gate_filler_items(
        prefix="RB-NOPRICE-FILL",
        brand="Samsung",
        name_prefix="Дисплей для Samsung Galaxy фон",
        prices=["10", "20", "30", "40", "50", "60", "70", "80", "90", "100"],
    )
    items.append(
        {
            "nomenclature_code": "RB-NOPRICE",
            "name": "Дисплей для Samsung Galaxy S24 + тачскрин",
            "status_label": "Рабочий",
            "quality_raw": "ORIG",
            "price_segment": "mid_low",
            "brand_compatibility": "Samsung",
        }
    )

    rows = build_dry_run_rows(
        items,
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-NOPRICE": {"sales_qty_window": Decimal("180")}},
            "returns": {},
            "purchase": purchase,
        },
        source_errors={},
        target_days=23,
        sales_window_days=180,
        order_rounding_rules=(
            OrderRoundingRule(threshold_gt=Decimal("49"), round_to=10),
            OrderRoundingRule(threshold_gt=Decimal("0"), round_to=5),
        ),
        order_rounding_price_gate=OrderRoundingPriceGatePolicy(enabled=True, min_group_size=10),
    )
    row = {row["nomenclature_code"]: row for row in rows}["RB-NOPRICE"]

    assert row["order_rounding_price_gate"] == "no_purchase_price"
    assert row["recommended_order_qty_raw"] == "23"
    assert row["recommended_order_qty"] == "23"


def test_order_rounding_group_splits_device_classes_inside_one_brand() -> None:
    gates = order_rounding_price_gates(
        [
            {
                "nomenclature_code": "RB-PHONE",
                "name": "Дисплей для Apple iPhone 15 Pro + тачскрин",
                "brand_compatibility": "Apple",
            },
            {
                "nomenclature_code": "RB-TABLET",
                "name": "Дисплей для Apple iPad mini 7 (A2993/A2995)",
                "brand_compatibility": "Apple",
            },
            {
                "nomenclature_code": "RB-WATCH",
                "name": "Дисплей для Apple Watch Series 9 45 мм",
                "brand_compatibility": "Apple",
            },
            {
                "nomenclature_code": "RB-HUAWEI-TABLET",
                "name": "Дисплей для Huawei MatePad SE 11 (AGS6-W09)",
                "brand_compatibility": "Huawei",
            },
        ],
        purchase_facts={},
        policy=OrderRoundingPriceGatePolicy(enabled=True, min_group_size=10),
    )

    assert gates["RB-PHONE"].group_label == "apple / телефон"
    assert gates["RB-TABLET"].group_label == "apple / планшет"
    assert gates["RB-WATCH"].group_label == "apple / часы"
    # Папка кладёт планшеты Huawei в общую папку бренда; название - нет.
    assert gates["RB-HUAWEI-TABLET"].group_label == "huawei / планшет"


def test_order_rounding_group_takes_first_brand_in_name_for_multibrand_card() -> None:
    gates = order_rounding_price_gates(
        [
            {
                "nomenclature_code": "RB000044988",
                "name": "Дисплей для Tecno Spark 7 (KF6N) / Infinix Hot 10i (X659B) + тачскрин",
                "brand_compatibility": "Infinix",
            },
            {
                "nomenclature_code": "RB000047004",
                "name": "Дисплей для Infinix Hot 10 Play (X688B) / Tecno Pova Neo (LE6)",
                "brand_compatibility": "Tecno",
            },
        ],
        purchase_facts={},
        policy=OrderRoundingPriceGatePolicy(enabled=True, min_group_size=10),
    )

    assert gates["RB000044988"].group_label == "tecno / телефон"
    assert gates["RB000047004"].group_label == "infinix / телефон"


def _price_batch_row_items() -> list[dict[str, object]]:
    return [
        {
            "nomenclature_code": "RB-BATCH",
            "name": "Дисплей для Samsung Galaxy A15",
            "status": "working",
            "status_label": "Поддерживаем (Рабочий)",
            "auto_order_allowed": True,
            "quality_raw": "Аналог",
            "price_segment": "mid_low",
            "brand_compatibility": "Samsung",
        }
    ]


def _price_batch_rows(*, sales_qty: str, target_days: int, excess_days: int) -> dict[str, object]:
    rows = build_dry_run_rows(
        _price_batch_row_items(),
        facts={
            "stock": {},
            "reserve": {},
            "incoming": {},
            "sales": {"RB-BATCH": {"sales_qty_window": Decimal(sales_qty)}},
            "returns": {},
            "purchase": {},
        },
        source_errors={},
        target_days=target_days,
        sales_window_days=180,
        order_rounding_rules=(
            OrderRoundingRule(threshold_gt=Decimal("49"), round_to=10),
            OrderRoundingRule(threshold_gt=Decimal("0"), round_to=5),
        ),
        speed_horizon_rules=(
            SpeedHorizonRule(
                tier="normal",
                min_group_avg_daily_sales_qty=Decimal("0.1"),
                max_effective_target_days=target_days,
                safety_stock_days=0,
            ),
        ),
        price_batch_rules=(
            PriceBatchRule(
                speed_tier="normal",
                price_segments=("mid_low",),
                minimum_batch_qty=12,
                max_automatic_excess_coverage_days=excess_days,
            ),
        ),
        price_batch_applies_to_statuses=("working",),
        price_batch_applies_to_analog_roles=("single_sku",),
    )
    return rows[0]


def test_price_batch_minimum_qty_is_final_and_never_rounded_up() -> None:
    row = _price_batch_rows(sales_qty="90", target_days=6, excess_days=30)

    assert row["recommended_order_qty_raw"] == "3"
    assert row["price_batch_min_qty"] == 12
    # Раньше округление считалось от max(raw, 12) и выдавало 15 - партия
    # раздувалась молча. Решение 2026-08-19: партия финальна.
    assert row["recommended_order_qty"] == "12"
    assert row["price_batch_decision"] == "rounded_to_price_minimum"


def test_price_batch_excess_within_thirty_days_stays_automatic() -> None:
    row = _price_batch_rows(sales_qty="54", target_days=10, excess_days=30)

    assert row["recommended_order_qty"] == "12"
    assert row["price_batch_decision"] == "rounded_to_price_minimum"
    assert row["dry_run_decision"] == "order"


def test_price_batch_excess_above_thirty_days_goes_to_manual_review() -> None:
    row = _price_batch_rows(sales_qty="45", target_days=12, excess_days=30)

    assert row["price_batch_decision"] == "manual_review_excess"
    assert row["dry_run_decision"] == "manual_review"
    assert "price_batch_excess_manual_review" in row["warnings"]


def test_price_segment_schedule_policy_uses_single_thirty_day_excess_threshold() -> None:
    policy = json.loads(DEFAULT_POLICY_JSON.read_text(encoding="utf-8"))
    rules = policy["price_segment_schedule_policy"]["rules"]

    with_threshold = [rule for rule in rules if "max_automatic_excess_coverage_days" in rule]
    assert len(with_threshold) == 3
    assert {rule["max_automatic_excess_coverage_days"] for rule in with_threshold} == {30}
    assert {rule["minimum_batch_qty"] for rule in with_threshold} == {20, 12, 10}

    # Дорогим сегментам поле не заводим: количество там не поднимают до партии.
    expensive = [rule for rule in rules if set(rule["price_segments"]) & {"mid_high", "premium"}]
    assert len(expensive) == 3
    assert all("max_automatic_excess_coverage_days" not in rule for rule in expensive)
    assert all(rule["rounding_mode"] == "exact_need" for rule in expensive)
