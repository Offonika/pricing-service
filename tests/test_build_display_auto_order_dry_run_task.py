from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.procurement_b2b_customer_demand import (
    B2BCustomerDemandComponent,
    B2BSkuDemandProfile,
)
from tasks.build_display_auto_order_dry_run import (
    DemandUpliftRule,
    OrderRoundingRule,
    PriceBatchRule,
    SpeedHorizonRule,
    SupportedAnalogPolicy,
    apply_supported_analog_and_price_batch_policies,
    build_dry_run_rows,
    build_summary,
    fetch_reserved_totals,
    fetch_stock_totals,
    load_auto_order_policy,
    load_warehouse_policy,
    rounded_order_qty,
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


def test_stock_query_filters_by_onec_quality_not_warehouse(tmp_path) -> None:
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
    assert "sellable_codes" not in stock_sql
    assert "sellable_codes" not in reserve_sql


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


def test_display_auto_order_b2b_customer_demand_consolidates_analog_group() -> None:
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

    winner = next(row for row in rows if row["analog_role"] == "primary_analog")
    loser = next(row for row in rows if row["analog_role"] == "transition_to_better_analog")
    assert winner["recommended_order_qty"] == "28"
    assert winner["b2b_replacement_target_stock_qty"] == "20"
    assert winner["b2b_replacement_recommended_order_qty"] == "20"
    assert winner["b2b_order_delta_qty"] == "-8"
    assert loser["recommended_order_qty"] == "0"
    assert loser["b2b_replacement_recommended_order_qty"] == "0"
    assert "перенесена в SKU-победитель" in loser["b2b_reason_ru"]


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


def test_display_auto_order_dry_run_caps_group_horizon_by_speed_tier() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-MED",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный) (Medium)",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A125 Galaxy A12",
                "quality_raw": "Medium",
            },
            {
                "nomenclature_code": "RB-ORIG",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный) (ORIG100)",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A125 Galaxy A12",
                "quality_raw": "ORIG100",
            },
        ],
        facts={
            "stock": {
                "RB-MED": {"sellable_stock_qty": Decimal("0")},
                "RB-ORIG": {"sellable_stock_qty": Decimal("0")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB-MED": {"sales_qty_window": Decimal("360"), "sales_doc_count": 36},
                "RB-ORIG": {"sales_qty_window": Decimal("180"), "sales_doc_count": 18},
            },
            "returns": {},
            "purchase": {
                "RB-MED": {"latest_purchase_price": Decimal("35")},
                "RB-ORIG": {"latest_purchase_price": Decimal("32")},
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

    primary = next(row for row in rows if row["analog_role"] == "primary_analog")

    assert primary["speed_tier"] == "super_fast"
    assert primary["speed_group_avg_daily_sales_qty"] == "3"
    assert primary["effective_target_days"] == 60
    assert primary["safety_stock_days"] == 7
    assert primary["analog_group_target_stock_qty"] == "180"
    assert primary["recommended_order_qty"] == "180"
    assert "speed_horizon_rule_applied" in primary["warnings"]


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
            "stock": {"RB-SLOW": {"sellable_stock_qty": Decimal("0")}},
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


def test_display_auto_order_dry_run_moves_order_to_better_analog() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB1",
                "name": "Дисплей для Samsung A125 Galaxy A12 / A127 Galaxy A12 Nacho + тачскрин (черный) (Medium)",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A127 Galaxy A12 Nacho",
                "quality_raw": "Medium",
                "quality_normalized": "medium",
            },
            {
                "nomenclature_code": "RB2",
                "name": "Дисплей для Samsung A127 Galaxy A12 Nacho + тачскрин (черный) (ORIG100)",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung A127 Galaxy A12 Nacho",
                "quality_raw": "ORIG100",
                "quality_normalized": "orig100",
            },
        ],
        facts={
            "stock": {
                "RB1": {"sellable_stock_qty": Decimal("0")},
                "RB2": {"sellable_stock_qty": Decimal("10")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "RB1": {"sales_qty_window": Decimal("100"), "sales_doc_count": 10},
                "RB2": {"sales_qty_window": Decimal("200"), "sales_doc_count": 20},
            },
            "returns": {},
            "purchase": {
                "RB1": {"latest_purchase_price": Decimal("34")},
                "RB2": {"latest_purchase_price": Decimal("33.5")},
            },
        },
        source_errors={},
        target_days=14,
        sales_window_days=180,
        max_order_qty=5,
    )

    by_code = {row["nomenclature_code"]: row for row in rows}

    assert by_code["RB1"]["analog_role"] == "transition_to_better_analog"
    assert by_code["RB1"]["preferred_replacement_code"] == "RB2"
    assert by_code["RB1"]["recommended_order_qty"] == "0"
    assert by_code["RB1"]["dry_run_decision"] == "do_not_order"
    assert "analog_transition_to_better_item" in by_code["RB1"]["warnings"]

    assert by_code["RB2"]["analog_role"] == "primary_analog"
    assert by_code["RB2"]["dry_run_decision"] == "order"
    assert by_code["RB2"]["recommended_order_qty"] == "5"
    assert by_code["RB2"]["analog_group_recommended_order_qty_raw"] == "14"
    assert "заказ переносим сюда" in by_code["RB2"]["analog_decision_reason_ru"]


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


def test_supported_analog_gets_network_minimum_then_low_price_batch() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "РБ000041515",
                "name": "Дисплей для Samsung T295 Galaxy Tab A 8.0 + тачскрин (черный)",
                "status": "working",
                "status_label": "Рабочий",
                "auto_order_allowed": True,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung T295 Galaxy Tab A 8.0",
                "quality_raw": "Аналог",
                "price_segment": "mid_low",
            },
            {
                "nomenclature_code": "РБ000041516",
                "name": "Дисплей для Samsung T295 Galaxy Tab A 8.0 + тачскрин (белый)",
                "status": "sale",
                "status_label": "ПРОДАЖА",
                "auto_order_allowed": False,
                "brand_compatibility": "Samsung",
                "model_compatibility": "Samsung T295 Galaxy Tab A 8.0",
                "quality_raw": "Аналог",
                "price_segment": "mid_low",
            },
        ],
        facts={
            "stock": {
                "РБ000041515": {"sellable_stock_qty": Decimal("23")},
                "РБ000041516": {"sellable_stock_qty": Decimal("8")},
            },
            "reserve": {},
            "incoming": {},
            "sales": {
                "РБ000041515": {
                    "sales_qty_window": Decimal("59"),
                    "sales_doc_count": 68,
                    "sales_warehouse_count": 12,
                    "last_sale_at": date(2026, 7, 10),
                },
                "РБ000041516": {
                    "sales_qty_window": Decimal("13"),
                    "sales_doc_count": 17,
                    "sales_warehouse_count": 9,
                    "last_sale_at": date(2026, 6, 18),
                },
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
        supported_analog_policy=SupportedAnalogPolicy(
            enabled=True,
            applies_to_statuses=("ПРОДАЖА", "Рабочий"),
            active_store_count=11,
            site_reserve_qty=1,
            min_network_stock_qty=12,
            min_recent_sales_pct_of_store_count=Decimal("10"),
            max_days_since_last_sale=180,
        ),
        price_batch_rules=(
            PriceBatchRule(
                speed_tier="normal",
                price_segments=("economy", "mid_low"),
                minimum_batch_qty=10,
                max_automatic_excess_coverage_days=21,
            ),
        ),
        price_batch_applies_to_statuses=("ПРОДАЖА", "Рабочий"),
        price_batch_applies_to_analog_roles=(
            "single_sku",
            "primary_analog",
            "supported_analog",
        ),
        as_of=date(2026, 7, 11),
    )

    by_code = {row["nomenclature_code"]: row for row in rows}
    black = by_code["РБ000041515"]
    white = by_code["РБ000041516"]
    assert black["analog_role"] == "primary_analog"
    assert black["recommended_order_qty"] == "0"
    assert white["analog_role"] == "supported_analog"
    assert white["supported_analog_min_stock_qty"] == 12
    assert white["supported_analog_floor_need_qty"] == "4"
    assert white["recommended_order_qty_raw"] == "4"
    assert white["recommended_order_qty"] == "10"
    assert white["dry_run_decision"] == "order"
    assert white["price_batch_decision"] == "rounded_to_price_minimum"
    assert white["price_batch_excess_qty"] == "6"
    assert white["price_batch_excess_coverage_days"] == "15"
    assert white["analog_group_recommended_order_qty"] == "10"


def test_price_batch_excess_above_limit_goes_to_manual_review_with_exact_need() -> None:
    rows = build_dry_run_rows(
        [
            {
                "nomenclature_code": "RB-PRICE-REVIEW",
                "name": "Дисплей тестовый",
                "status": "working",
                "status_label": "Рабочий",
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
        price_batch_applies_to_statuses=("Рабочий",),
        price_batch_applies_to_analog_roles=("single_sku",),
    )

    row = rows[0]
    assert row["recommended_order_qty_raw"] == "1"
    assert row["recommended_order_qty"] == "1"
    assert row["dry_run_decision"] == "manual_review"
    assert row["price_batch_decision"] == "manual_review_excess"
    assert "price_batch_excess_manual_review" in row["warnings"]


def test_supported_slow_variant_shows_need_but_stays_manual_review() -> None:
    rows = [
        {
            "nomenclature_code": "PRIMARY",
            "name": "Дисплей для Samsung T295 + тачскрин (черный)",
            "status_label": "Рабочий",
            "_assortment_status": "working",
            "_auto_order_allowed": True,
            "quality_raw": "Аналог",
            "analog_model_tokens": "samsung:code:t295",
            "analog_group_id": "analog-t295",
            "analog_role": "primary_analog",
            "analog_group_recommended_order_qty_raw": "4",
            "analog_group_target_stock_qty": "35",
            "analog_group_free_stock_qty": "31",
            "analog_group_incoming_qty": "0",
            "free_stock_qty": "23",
            "incoming_qty": "0",
            "net_sales_qty_window": "59",
            "last_sale_at": "2026-07-10",
            "speed_tier": "slow",
            "speed_group_avg_daily_sales_qty": "0.1",
            "blockers": "",
            "warnings": "",
            "data_sources": "",
        },
        {
            "nomenclature_code": "SUPPORTED",
            "name": "Дисплей для Samsung T295 + тачскрин (белый)",
            "status_label": "ПРОДАЖА",
            "_assortment_status": "sale",
            "_auto_order_allowed": False,
            "quality_raw": "Аналог",
            "analog_model_tokens": "samsung:code:t295",
            "analog_group_id": "analog-t295",
            "analog_role": "transition_to_better_analog",
            "analog_group_recommended_order_qty_raw": "4",
            "analog_group_target_stock_qty": "35",
            "analog_group_free_stock_qty": "31",
            "analog_group_incoming_qty": "0",
            "free_stock_qty": "8",
            "incoming_qty": "0",
            "net_sales_qty_window": "13",
            "last_sale_at": "2026-06-18",
            "speed_tier": "slow",
            "speed_group_avg_daily_sales_qty": "0.1",
            "blockers": "",
            "warnings": "",
            "data_sources": "",
        },
    ]

    apply_supported_analog_and_price_batch_policies(
        rows,
        supported_analog_policy=SupportedAnalogPolicy(
            enabled=True,
            applies_to_statuses=("ПРОДАЖА", "Рабочий"),
            active_store_count=11,
            site_reserve_qty=1,
            min_network_stock_qty=12,
            min_recent_sales_pct_of_store_count=Decimal("10"),
            max_days_since_last_sale=180,
        ),
        price_batch_rules=(),
        price_batch_applies_to_statuses=(),
        price_batch_applies_to_analog_roles=(),
        as_of=date(2026, 7, 11),
        min_order_qty=1,
        max_order_qty=None,
        order_rounding_rules=(),
    )

    supported = next(row for row in rows if row["nomenclature_code"] == "SUPPORTED")
    assert supported["analog_role"] == "supported_analog"
    assert supported["recommended_order_qty"] == "4"
    assert supported["dry_run_decision"] == "manual_review"
    assert supported["price_batch_decision"] == "manual_review_slow"


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
