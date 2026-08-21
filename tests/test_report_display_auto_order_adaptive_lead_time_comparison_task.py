from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.display_family_registry import ActiveDisplayFamilyMemberContext
from tasks.report_display_auto_order_adaptive_lead_time_comparison import (
    adaptive_reason,
    best_lead_time_row,
    build_comparison_rows,
    build_summary,
    build_sync_ready_rows,
    refresh_sync_ready_family_recommendations,
)


def _supplier_candidate(
    supplier: str,
    *,
    price: str,
    total_days: int,
    currency: str = "USD",
    confidence: str = "high",
    order_count: int = 10,
) -> dict[str, str]:
    return {
        "supplier_name": supplier,
        "latest_purchase_price": price,
        "price_currency_code": currency,
        "recommended_supplier_prepare_days": str(total_days // 2),
        "recommended_logistics_days": str(total_days - total_days // 2),
        "lead_time_confidence": confidence,
        "order_line_count": str(order_count),
        "latest_supplier_order_at": "2026-08-20",
    }


def test_supplier_selection_prefers_price_when_difference_exceeds_three_pct() -> None:
    selected = best_lead_time_row(
        [
            _supplier_candidate("Дешевле", price="100", total_days=30, order_count=3),
            _supplier_candidate("Быстрее", price="110", total_days=10, order_count=30),
        ]
    )

    assert selected is not None
    assert selected["supplier_name"] == "Дешевле"
    assert selected["supplier_selection_reason"] == "price_guard_over_3pct_then_speed"
    assert selected["supplier_cost_tie_pct"] == "3"


def test_supplier_selection_prefers_speed_inside_three_pct_price_corridor() -> None:
    selected = best_lead_time_row(
        [
            _supplier_candidate("Дешевле", price="100", total_days=30, order_count=30),
            _supplier_candidate("Быстрее", price="102", total_days=10, order_count=3),
        ]
    )

    assert selected is not None
    assert selected["supplier_name"] == "Быстрее"
    assert selected["supplier_selection_reason"] == "price_tie_within_3pct_speed"
    assert selected["supplier_selected_purchase_price"] == "102"


def test_supplier_selection_does_not_compare_prices_in_different_currencies() -> None:
    selected = best_lead_time_row(
        [
            _supplier_candidate(
                "USD поставщик",
                price="100",
                total_days=30,
                currency="USD",
                order_count=3,
            ),
            _supplier_candidate(
                "CNY поставщик",
                price="10",
                total_days=10,
                currency="CNY",
                order_count=30,
            ),
        ]
    )

    assert selected is not None
    assert selected["supplier_name"] == "CNY поставщик"
    assert selected["supplier_selection_rule"] == "historical_evidence_fallback"
    assert selected["supplier_selection_reason"] == "comparable_current_prices_unavailable"


def test_adaptive_reason_lists_quantity_and_horizon_as_independent_changes() -> None:
    reason = adaptive_reason(
        current_qty=Decimal("31"),
        adaptive_qty=Decimal("35"),
        current_effective_days=105,
        adaptive_effective_days=82,
        lead_applied=False,
        lead_candidate=None,
        seasonality_adjustment={"total_adjustment_days": 0},
    )

    assert reason.startswith(
        "адаптивный расчет: количество 31 -> 35 шт. (увеличено); " "горизонт 105 -> 82 дней"
    )
    assert "увеличил заказ: горизонт" not in reason


def test_adaptive_lead_time_comparison_reduces_order_when_live_route_is_shorter() -> None:
    dry_row = _dry_row()
    dry_row["distribution_to_shelf_days"] = "7"
    rows = build_comparison_rows(
        [dry_row],
        [
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_name": "Samsung display",
                "responsible_name": "Бочаров Омар",
                "order_line_count": "5",
                "recommended_supplier_prepare_days": "4",
                "recommended_logistics_days": "16",
                "lead_time_confidence": "high",
                "latest_supplier_order_at": "2026-06-01",
            }
        ],
        policy={"order_rounding_rules": [{"threshold_gt": 100, "round_to": 10}]},
        as_of=date(2026, 7, 5),
    )

    row = rows[0]
    assert row["lead_time_applied"] == 1
    assert row["lead_time_source_level"] == "sku"
    assert row["adaptive_effective_target_days"] == 59
    assert row["adaptive_recommended_order_qty"] == "120"
    assert row["qty_delta"] == "0"
    assert row["supplier_name"] == "Samsung display"


def test_adaptive_lead_time_comparison_keeps_fallback_for_low_confidence() -> None:
    dry_row = _dry_row()
    dry_row["distribution_to_shelf_days"] = "7"
    rows = build_comparison_rows(
        [dry_row],
        [
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_name": "Samsung display",
                "responsible_name": "Бочаров Омар",
                "order_line_count": "1",
                "recommended_supplier_prepare_days": "4",
                "recommended_logistics_days": "16",
                "lead_time_confidence": "low",
                "latest_supplier_order_at": "2026-06-01",
            }
        ],
        policy={"order_rounding_rules": [{"threshold_gt": 100, "round_to": 10}]},
        as_of=date(2026, 7, 5),
    )

    row = rows[0]
    assert row["lead_time_applied"] == 0
    assert row["lead_time_source_level"] == "sku_low_confidence"
    assert row["adaptive_effective_target_days"] == 67
    assert row["adaptive_recommended_order_qty"] == "140"
    assert "lead_time_low_confidence_fallback" in row["warnings"]


def test_adaptive_lead_time_preserves_active_customer_order_need() -> None:
    dry_row = _dry_row(
        current_recommended_order_qty="101",
        current_target_stock_qty="104",
        current_effective_target_days="52",
    )
    dry_row.update(
        {
            "free_stock_qty": "6",
            "active_customer_order_qty": "7",
            "order_available_stock_qty": "3",
        }
    )

    rows = build_comparison_rows(
        [dry_row],
        [
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_name": "Samsung display",
                "responsible_name": "Бочаров Омар",
                "order_line_count": "5",
                "recommended_supplier_prepare_days": "4",
                "recommended_logistics_days": "16",
                "lead_time_confidence": "high",
                "latest_supplier_order_at": "2026-06-01",
            }
        ],
        policy={"order_rounding_rules": []},
        as_of=date(2026, 7, 5),
    )

    row = rows[0]
    assert row["adaptive_target_stock_qty"] == "104"
    assert row["free_stock_qty"] == "6"
    assert row["active_customer_order_qty"] == "7"
    assert row["order_available_stock_qty"] == "3"
    assert row["adaptive_recommended_order_qty_raw"] == "101"
    assert row["adaptive_recommended_order_qty"] == "101"
    assert "Заказов покупателей 7 шт." in row["reason_ru"]


def test_margin_flow_rule_uses_point_metrics_and_minimum_representation() -> None:
    """Правило «Маржинального потока» (канон assortment-lifecycle-policy.md).

    Скорость берётся суммой по точкам, остаток — свободный по точкам, в пути —
    весь открытый остаток заказа поставщику, а цель не опускается ниже
    минимальной представленности 13 шт.
    """

    row = _dry_row(avg_daily_sales_qty="2", current_recommended_order_qty="40")
    row.update(
        {
            "margin_flow_qualifies": "yes",
            "margin_flow_point_rate_sum": "0.1",
            "margin_flow_profitability_pct": "42",
            "margin_flow_free_stock_qty": "2",
            "margin_flow_reliable_incoming_qty": "1",
            "free_stock_qty": "50",
            "incoming_qty": "50",
        }
    )
    rows = build_comparison_rows(
        [row],
        [],
        policy={
            "order_rounding_rules": [{"threshold_gt": 100, "round_to": 10}],
            "margin_flow_policy": {
                "enabled": True,
                "safety_stock_days": 25,
                "minimum_representation_qty": 13,
            },
        },
        as_of=date(2026, 8, 19),
    )

    result = rows[0]
    assert result["margin_flow_rule_applied"] == 1
    assert result["margin_flow_minimum_representation_qty"] == 13
    assert result["adaptive_target_stock_qty"] == "13"
    assert result["adaptive_recommended_order_qty"] == "10"
    assert "margin_flow_rule_applied" in result["warnings"]
    assert "Маржинальный поток" in result["reason_ru"]
    assert "открытый остаток заказа поставщику" in result["reason_ru"]


def test_margin_flow_minimum_representation_is_final_without_rounding() -> None:
    """Решение 2026-08-19: минимум 13 шт — финальное количество.

    Ценовое округление поверх минимальной представленности не применяется, как и
    поверх минимальной партии дешёвых сегментов.
    """

    row = _dry_row(avg_daily_sales_qty="2", current_recommended_order_qty="40")
    row.update(
        {
            "margin_flow_qualifies": "yes",
            "margin_flow_point_rate_sum": "0.1",
            "margin_flow_free_stock_qty": "0",
            "margin_flow_reliable_incoming_qty": "0",
        }
    )
    rows = build_comparison_rows(
        [row],
        [],
        policy={
            "order_rounding_rules": [{"threshold_gt": 0, "round_to": 5}],
            "margin_flow_policy": {
                "enabled": True,
                "safety_stock_days": 25,
                "minimum_representation_qty": 13,
            },
        },
        as_of=date(2026, 8, 19),
    )

    result = rows[0]
    assert result["adaptive_target_stock_qty"] == "13"
    assert result["adaptive_recommended_order_qty"] == "13"
    assert "margin_flow_minimum_representation_final" in result["warnings"]


def test_margin_flow_adds_active_customer_orders_to_need() -> None:
    """Решение 2026-08-19: активные «Заказы покупателей» входят и в это правило."""

    row = _dry_row(avg_daily_sales_qty="2", current_recommended_order_qty="40")
    row.update(
        {
            "margin_flow_qualifies": "yes",
            "margin_flow_point_rate_sum": "0.1",
            "margin_flow_free_stock_qty": "2",
            "margin_flow_reliable_incoming_qty": "1",
            "active_customer_order_qty": "4",
        }
    )
    rows = build_comparison_rows(
        [row],
        [],
        policy={
            "order_rounding_rules": [{"threshold_gt": 0, "round_to": 5}],
            "margin_flow_policy": {
                "enabled": True,
                "safety_stock_days": 25,
                "minimum_representation_qty": 13,
            },
        },
        as_of=date(2026, 8, 19),
    )

    result = rows[0]
    assert result["adaptive_recommended_order_qty"] == "14"
    assert "активные Заказы покупателей 4" in result["reason_ru"]


def test_margin_flow_rule_is_off_when_policy_is_missing() -> None:
    row = _dry_row(avg_daily_sales_qty="2", current_recommended_order_qty="40")
    row.update(
        {
            "margin_flow_qualifies": "yes",
            "margin_flow_point_rate_sum": "0.1",
            "margin_flow_free_stock_qty": "2",
            "margin_flow_reliable_incoming_qty": "1",
        }
    )
    rows = build_comparison_rows(
        [row],
        [],
        policy={"order_rounding_rules": [{"threshold_gt": 100, "round_to": 10}]},
        as_of=date(2026, 8, 19),
    )

    assert rows[0]["margin_flow_rule_applied"] == 0


def test_adaptive_lead_time_comparison_applies_recent_supplier_seasonality() -> None:
    dry_row = _dry_row(
        speed_tier="normal",
        speed_max_effective_target_days="82",
        speed_rule_safety_stock_days="14",
        avg_daily_sales_qty="1",
        current_recommended_order_qty="59",
        current_target_stock_qty="59",
        current_effective_target_days="59",
    )
    dry_row["distribution_to_shelf_days"] = "7"
    rows = build_comparison_rows(
        [dry_row],
        [
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_name": "Samsung display",
                "responsible_name": "Бочаров Омар",
                "order_line_count": "5",
                "recommended_supplier_prepare_days": "4",
                "recommended_logistics_days": "16",
                "lead_time_confidence": "high",
                "latest_supplier_order_at": "2026-06-01",
            }
        ],
        seasonality_rows=[
            {
                "week_start": "2026-06-29",
                "top_supplier_name": "Samsung display",
                "top_responsible_name": "Бочаров Омар",
                "supplier_prepare_delta_days": "0",
                "logistics_delta_days": "10",
                "prepare_delay_signal": "0",
                "road_seasonality_signal": "1",
                "route_risk_level": "high",
            }
        ],
        policy={"order_rounding_rules": []},
        as_of=date(2026, 7, 5),
    )

    row = rows[0]
    assert row["seasonality_adjustment_days"] == 10
    assert row["adaptive_logistics_days"] == 26
    assert row["adaptive_effective_target_days"] == 76
    assert row["adaptive_recommended_order_qty"] == "76"
    assert row["seasonality_signal"] == "road_seasonality"


def test_adaptive_lead_time_uses_only_main_supplier_history() -> None:
    dry_row = _dry_row()
    dry_row.update(
        {
            "main_supplier_ref": "0xcard",
            "main_supplier_name": "Поставщик карточки",
            "main_supplier_source_status": "configured",
            "distribution_to_shelf_days": "7",
        }
    )
    rows = build_comparison_rows(
        [dry_row],
        [
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_ref": "0xother",
                "supplier_name": "Другой поставщик",
                "order_line_count": "50",
                "recommended_supplier_prepare_days": "1",
                "recommended_logistics_days": "1",
                "lead_time_confidence": "high",
            },
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_ref": "0xcard",
                "supplier_name": "Поставщик карточки",
                "order_line_count": "2",
                "recommended_supplier_prepare_days": "4",
                "recommended_logistics_days": "16",
                "lead_time_confidence": "medium",
            },
        ],
        policy={"order_rounding_rules": []},
        as_of=date(2026, 8, 20),
    )

    row = rows[0]
    assert row["lead_time_applied"] == 1
    assert row["lead_time_source_level"] == "sku_main_supplier"
    assert row["supplier_name"] == "Поставщик карточки"
    assert row["adaptive_lead_time_days"] == 20
    assert row["supplier_selection_rule"] == "main_supplier_card"
    assert row["supplier_selection_reason"] == "main_supplier_from_onec_card"


def test_adaptive_lead_time_does_not_substitute_another_supplier() -> None:
    dry_row = _dry_row()
    dry_row.update(
        {
            "main_supplier_ref": "0xcard",
            "main_supplier_name": "Поставщик карточки",
            "main_supplier_source_status": "configured",
            "distribution_to_shelf_days": "7",
        }
    )
    rows = build_comparison_rows(
        [dry_row],
        [
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_ref": "0xother",
                "supplier_name": "Другой поставщик",
                "order_line_count": "50",
                "recommended_supplier_prepare_days": "1",
                "recommended_logistics_days": "1",
                "lead_time_confidence": "high",
            }
        ],
        policy={"order_rounding_rules": []},
        as_of=date(2026, 8, 20),
    )

    row = rows[0]
    assert row["lead_time_applied"] == 0
    assert row["lead_time_source_level"] == "main_supplier_history_missing"
    assert row["supplier_name"] == ""
    assert row["adaptive_lead_time_days"] == 48
    assert "main_supplier_lead_time_missing_fallback" in row["warnings"]


def test_adaptive_lead_time_fails_closed_when_card_snapshot_is_unavailable() -> None:
    dry_row = _dry_row()
    dry_row["main_supplier_source_status"] = "unavailable"
    rows = build_comparison_rows(
        [dry_row],
        [
            {
                "nomenclature_code": "RB1",
                "display_group_key": "samsung a12",
                "supplier_ref": "0xother",
                "supplier_name": "Другой поставщик",
                "order_line_count": "50",
                "recommended_supplier_prepare_days": "1",
                "recommended_logistics_days": "1",
                "lead_time_confidence": "high",
            }
        ],
        policy={"order_rounding_rules": []},
        as_of=date(2026, 8, 20),
    )

    row = rows[0]
    assert row["lead_time_applied"] == 0
    assert row["lead_time_source_level"] == "main_supplier_source_unavailable"
    assert row["supplier_name"] == ""
    assert "main_supplier_source_unavailable_fallback" in row["warnings"]


def test_adaptive_lead_time_comparison_summary_totals() -> None:
    rows = [
        {
            "current_recommended_order_qty": "120",
            "adaptive_recommended_order_qty": "110",
            "qty_delta": "-10",
            "estimated_purchase_value_delta": "-100",
            "adaptive_decision": "order",
            "lead_time_source_level": "sku",
            "lead_time_confidence": "high",
            "lead_time_applied": 1,
            "seasonality_adjustment_days": 0,
        }
    ]
    summary = build_summary(
        rows,
        dry_run_csv=__file__,
        lead_time_csv=__file__,
        seasonality_csv=None,
        as_of=date(2026, 7, 5),
    )

    assert summary["current_total_recommended_order_qty"] == "120"
    assert summary["adaptive_total_recommended_order_qty"] == "110"
    assert summary["qty_delta"] == "-10"
    assert summary["lead_time_applied_rows"] == 1


def test_build_sync_ready_rows_applies_adaptive_quantities_for_bitrix_sync() -> None:
    dry_row = _dry_row()
    comparison_row = {
        "adaptive_decision": "order",
        "adaptive_recommended_order_qty": "110",
        "adaptive_recommended_order_qty_raw": "109",
        "adaptive_target_stock_qty": "110",
        "adaptive_effective_target_days": "52",
        "adaptive_lead_time_days": "20",
        "adaptive_supplier_prepare_days": "4",
        "adaptive_logistics_days": "16",
        "adaptive_safety_stock_days": "7",
        "adaptive_forecast_qty": "103",
        "adaptive_safety_stock_qty": "7",
        "free_stock_qty": "0",
        "incoming_qty": "0",
        "lead_time_applied": "1",
        "reason_ru": "адаптивный расчет уменьшил заказ",
        "warnings": "adaptive_lead_time_applied",
    }

    rows = build_sync_ready_rows([dry_row], [comparison_row])

    assert rows[0]["dry_run_decision"] == "order"
    assert rows[0]["recommended_order_qty"] == "110"
    assert rows[0]["recommended_order_qty_raw"] == "109"
    assert rows[0]["target_stock_qty"] == "110"
    assert rows[0]["lead_time_days"] == "20"
    assert rows[0]["supplier_prepare_days"] == "4"
    assert rows[0]["logistics_days"] == "16"
    assert rows[0]["reason_ru"] == "адаптивный расчет уменьшил заказ"
    assert "adaptive_lead_time_sync_ready" in rows[0]["warnings"]
    assert "local:adaptive_lead_time" in rows[0]["data_sources"]


def test_sync_ready_removes_minimum_batch_signal_for_plain_rounding() -> None:
    dry_row = _dry_row(current_recommended_order_qty="10")
    dry_row.update(
        {
            "price_batch_min_qty": "10",
            "warnings": "price_batch_minimum_applied; order_qty_rounded_to_multiple",
        }
    )
    comparison_row = {
        "adaptive_decision": "order",
        "adaptive_recommended_order_qty": "35",
        "adaptive_recommended_order_qty_raw": "31",
        "warnings": "",
    }

    row = build_sync_ready_rows([dry_row], [comparison_row])[0]

    assert row["recommended_order_qty_raw"] == "31"
    assert row["recommended_order_qty"] == "35"
    assert "price_batch_minimum_applied" not in row["warnings"]


def test_sync_ready_removes_stale_minimum_batch_signal_when_qty_is_unchanged() -> None:
    dry_row = _dry_row(current_recommended_order_qty="20")
    dry_row.update(
        {
            "price_batch_min_qty": "20",
            "warnings": "price_batch_minimum_applied",
        }
    )
    comparison_row = {
        "adaptive_decision": "order",
        "adaptive_recommended_order_qty": "160",
        "adaptive_recommended_order_qty_raw": "160",
        "warnings": "",
    }

    row = build_sync_ready_rows([dry_row], [comparison_row])[0]

    assert row["recommended_order_qty_raw"] == "160"
    assert row["recommended_order_qty"] == "160"
    assert "price_batch_minimum_applied" not in row["warnings"]


def test_sync_ready_recomputes_minimum_batch_signal_after_adaptive_overlay() -> None:
    dry_row = _dry_row(current_recommended_order_qty="35")
    dry_row.update({"price_batch_min_qty": "10", "warnings": ""})
    comparison_row = {
        "adaptive_decision": "order",
        "adaptive_recommended_order_qty": "10",
        "adaptive_recommended_order_qty_raw": "7",
        "warnings": "",
    }

    row = build_sync_ready_rows([dry_row], [comparison_row])[0]

    assert "price_batch_minimum_applied" in row["warnings"]


def test_sync_ready_does_not_claim_minimum_when_final_qty_stays_below_it() -> None:
    dry_row = _dry_row(current_recommended_order_qty="10")
    dry_row.update(
        {
            "price_batch_min_qty": "10",
            "warnings": "price_batch_minimum_applied",
        }
    )
    comparison_row = {
        "adaptive_decision": "order",
        "adaptive_recommended_order_qty": "5",
        "adaptive_recommended_order_qty_raw": "1",
        "warnings": "price_batch_minimum_applied",
    }

    row = build_sync_ready_rows([dry_row], [comparison_row])[0]

    assert "price_batch_minimum_applied" not in row["warnings"]


def test_sync_ready_family_overlay_is_rebuilt_after_adaptive_quantity() -> None:
    row = _dry_row(current_recommended_order_qty="4")
    row.update(
        {
            "latest_purchase_price": "100",
            "sales_qty_window_short": "5",
            "sales_qty_window_medium": "5",
            "sales_qty_window": "5",
            "free_stock_qty": "0",
            "incoming_qty": "0",
            "display_family_baseline_order_qty": "10",
            "display_family_allocated_order_qty": "10",
            "display_family_recommendation_status": "allocated_shadow",
        }
    )
    context = ActiveDisplayFamilyMemberContext(
        registry_version_id=2,
        registry_version_number=2,
        registry_inventory_checksum="a" * 64,
        family_record_id=10,
        family_key="family-1",
        family_label="Apple iPhone Test",
        family_member_count=1,
        family_review_member_count=0,
        family_matching_review_member_count=0,
        family_warning_codes=(),
        product_id=1,
        segment_id="premium|soft_oled",
        quality_segment="premium",
        construction_segment="soft_oled",
        requires_manual_review=False,
        member_warning_codes=(),
        matching_evidence={},
    )

    summary = refresh_sync_ready_family_recommendations(
        [row],
        membership_by_code={"RB1": context},
    )

    assert row["display_family_baseline_order_qty"] == "4"
    assert row["display_family_allocated_order_qty"] == "4"
    assert summary["baseline_order_qty"] == "4"
    assert summary["allocated_order_qty"] == "4"


def _dry_row(
    *,
    speed_tier: str = "super_fast",
    speed_max_effective_target_days: str = "60",
    speed_rule_safety_stock_days: str = "7",
    avg_daily_sales_qty: str = "2",
    current_recommended_order_qty: str = "120",
    current_target_stock_qty: str = "120",
    current_effective_target_days: str = "60",
) -> dict[str, str]:
    return {
        "nomenclature_code": "RB1",
        "name": "Дисплей для Samsung A12 + тачскрин",
        "analog_group_id": "",
        "analog_role": "single_sku",
        "speed_tier": speed_tier,
        "speed_max_effective_target_days": speed_max_effective_target_days,
        "speed_rule_safety_stock_days": speed_rule_safety_stock_days,
        "speed_rule_action": "cap_coverage_days",
        "dry_run_decision": "order",
        "recommended_order_qty": current_recommended_order_qty,
        "target_stock_qty": current_target_stock_qty,
        "effective_target_days": current_effective_target_days,
        "lead_time_days": "48",
        "supplier_prepare_days": "18",
        "logistics_days": "30",
        "target_days": "14",
        "order_cadence_days": "7",
        "supplier_delay_buffer_days": "3",
        "receiving_buffer_days": "1",
        "safety_stock_days": speed_rule_safety_stock_days,
        "avg_daily_sales_qty": avg_daily_sales_qty,
        "free_stock_qty": "0",
        "incoming_qty": "0",
        "latest_purchase_price": str(Decimal("10")),
        "warnings": "",
    }
