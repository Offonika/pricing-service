from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.display_family_registry import ActiveDisplayFamilyMemberContext
from tasks.report_display_auto_order_adaptive_lead_time_comparison import (
    build_comparison_rows,
    build_summary,
    build_sync_ready_rows,
    refresh_sync_ready_family_recommendations,
)


def test_adaptive_lead_time_comparison_reduces_order_when_live_route_is_shorter() -> None:
    rows = build_comparison_rows(
        [_dry_row()],
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
    assert row["adaptive_effective_target_days"] == 52
    assert row["adaptive_recommended_order_qty"] == "110"
    assert row["qty_delta"] == "-10"
    assert row["supplier_name"] == "Samsung display"


def test_adaptive_lead_time_comparison_keeps_fallback_for_low_confidence() -> None:
    rows = build_comparison_rows(
        [_dry_row()],
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
    assert row["adaptive_effective_target_days"] == 60
    assert row["adaptive_recommended_order_qty"] == "120"
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
    assert row["adaptive_effective_target_days"] == 69
    assert row["adaptive_recommended_order_qty"] == "69"
    assert row["seasonality_signal"] == "road_seasonality"


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
