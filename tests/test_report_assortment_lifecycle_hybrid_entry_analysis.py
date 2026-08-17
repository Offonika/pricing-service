from datetime import date, timedelta
from decimal import Decimal

from tasks.report_assortment_lifecycle_hybrid_entry_analysis import (
    economic_delta_ranges,
    summarize_trajectory,
)


def _rows(statuses: list[str]) -> list[dict[str, str]]:
    start = date(2026, 1, 1)
    return [
        {
            "business_date": (start + timedelta(days=offset)).isoformat(),
            "nomenclature_code": "SKU-1",
            "status": status,
            "hybrid_entry_signal": (
                "strong_x1.5" if status == "sale" and offset in {1, 5} else "none"
            ),
        }
        for offset, status in enumerate(statuses)
    ]


def test_summarize_trajectory_separates_completed_and_censored_spells() -> None:
    summary = summarize_trajectory(_rows(["working", "sale", "sale", "working", "working", "sale"]))

    assert summary["daily_row_count"] == 6
    assert summary["stage_days"] == {"sale": 3, "working": 3}
    assert summary["active_transitions"]["working_to_sale"]["event_count"] == 2
    assert summary["active_transitions"]["sale_to_working"]["event_count"] == 1
    assert summary["completed_spell_duration"]["sale"]["spell_count"] == 1
    assert summary["completed_spell_duration"]["sale"]["median_days"] == 2
    assert summary["censored_spell_count"] == {"sale": 1, "working": 1}
    assert summary["entry_signal_segments"]["strong_x1.5"] == {
        "event_count": 2,
        "sku_count": 1,
        "short_reverse_proxies": {"1": 0, "3": 1, "7": 1, "14": 1},
    }


def test_economic_delta_ranges_compares_matching_levels() -> None:
    metrics = (
        "served_sales_qty",
        "gross_profit_rub",
        "average_inventory_value_rub",
        "carrying_cost_rub",
        "economic_effect_rub",
        "gmroi",
        "ending_inventory_qty",
        "ending_target_stock_qty",
        "ending_excess_stock_qty",
    )
    baseline = [
        {
            "policy": "base",
            "comparable_group_level": "all_displays",
            **{metric: "10" for metric in metrics},
        }
    ]
    candidate = [
        {
            "policy": "candidate",
            "comparable_group_level": "all_displays",
            **{metric: Decimal("12") for metric in metrics},
        }
    ]

    ranges = economic_delta_ranges(
        candidate_rows=candidate,
        baseline_rows=baseline,
        candidate_policy="candidate",
        baseline_policy="base",
    )

    assert ranges["served_sales_qty"]["min"] == "2"
    assert ranges["served_sales_qty"]["all_positive"] is True
