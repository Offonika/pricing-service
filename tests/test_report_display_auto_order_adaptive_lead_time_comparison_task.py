from __future__ import annotations

from datetime import date
from decimal import Decimal

from tasks.report_display_auto_order_adaptive_lead_time_comparison import (
    build_comparison_rows,
    build_summary,
    build_sync_ready_rows,
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


def test_adaptive_lead_time_preserves_quality_blocker() -> None:
    dry_row = _dry_row(current_recommended_order_qty="0")
    dry_row["dry_run_decision"] = "manual_review"
    dry_row["blockers"] = "batch_error_suspected"

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
        as_of=date(2026, 8, 3),
    )

    assert rows[0]["adaptive_recommended_order_qty"] == "0"
    assert rows[0]["adaptive_recommended_order_qty_raw"] == "0"
    assert rows[0]["adaptive_decision"] == "manual_review"
    assert "manual_blocker_preserved" in rows[0]["warnings"]


def test_sync_ready_never_restores_order_for_source_blocker() -> None:
    dry_row = _dry_row(current_recommended_order_qty="0")
    dry_row["dry_run_decision"] = "manual_review"
    dry_row["blockers"] = "defect_rate_suspected"
    comparison_row = {
        "adaptive_decision": "order",
        "adaptive_recommended_order_qty": "63",
        "adaptive_recommended_order_qty_raw": "63",
        "adaptive_target_stock_qty": "63",
        "adaptive_effective_target_days": "70",
        "adaptive_lead_time_days": "48",
        "adaptive_supplier_prepare_days": "18",
        "adaptive_logistics_days": "30",
        "adaptive_safety_stock_days": "7",
        "adaptive_forecast_qty": "56",
        "adaptive_safety_stock_qty": "7",
        "free_stock_qty": "0",
        "incoming_qty": "0",
        "lead_time_applied": "1",
        "reason_ru": "адаптивный расчет увеличил заказ",
        "warnings": "adaptive_lead_time_applied",
    }

    rows = build_sync_ready_rows([dry_row], [comparison_row])

    assert rows[0]["recommended_order_qty"] == "0"
    assert rows[0]["recommended_order_qty_raw"] == "0"
    assert rows[0]["dry_run_decision"] == "manual_review"


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
