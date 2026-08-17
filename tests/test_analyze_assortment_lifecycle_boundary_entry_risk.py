from datetime import date

from tasks.analyze_assortment_lifecycle_boundary_entry_risk import (
    BoundaryEpisode,
    RiskRule,
    rule_metrics,
    select_calibration_rule,
    validation_passes,
)


def _episode(*, risky: bool, reverse_days: int | None) -> BoundaryEpisode:
    return BoundaryEpisode(
        business_date=date(2026, 2, 1),
        nomenclature_code="SKU-1",
        name="Тест",
        reverse_days=reverse_days,
        sales_30=3 if risky else 6,
        sales_90=8,
        sales_180=15,
        available_days_30=30,
        available_days_90=90,
        active_days_30=3,
        document_count_30=3,
        customer_count_30=3,
        point_count_30=3,
        max_day_share_30=0.34,
        adjusted_growth_ratio_30_to_90=1.25 if risky else 1.45,
    )


def test_rule_metrics_separates_flagged_and_unflagged_risk() -> None:
    rule = RiskRule("test", "test", lambda row: row.sales_30 <= 3)
    rows = [
        _episode(risky=True, reverse_days=3),
        _episode(risky=True, reverse_days=None),
        _episode(risky=False, reverse_days=None),
        _episode(risky=False, reverse_days=None),
    ]

    metrics = rule_metrics(rows, rule=rule)

    assert metrics["flagged_count"] == 2
    assert metrics["flagged_reverse_within_7_rate"] == 0.5
    assert metrics["unflagged_reverse_within_7_rate"] == 0
    assert metrics["reverse_within_7_capture"] == 1


def test_calibration_selection_and_validation_gates() -> None:
    metrics = [
        {
            "rule_id": "wide",
            "flagged_count": 100,
            "flagged_reverse_within_7_rate": 0.20,
            "reverse_within_7_capture": 0.60,
        },
        {
            "rule_id": "precise",
            "flagged_count": 40,
            "flagged_reverse_within_7_rate": 0.30,
            "reverse_within_7_capture": 0.25,
        },
    ]

    assert select_calibration_rule(metrics) == "precise"
    assert (
        validation_passes(
            {
                "flagged_count": 25,
                "risk_lift_vs_overall": 1.4,
                "reverse_within_7_capture": 0.2,
                "flagged_reverse_within_7_rate": 0.21,
                "unflagged_reverse_within_7_rate": 0.14,
            }
        )
        is True
    )
