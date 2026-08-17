from decimal import Decimal

from tasks.report_display_auto_order_hybrid_acceleration_backtest import (
    AccelerationFilter,
    _filter_kwargs,
    _manual_review_rows,
    _period_delta_rows,
    _select_profile,
)


def test_acceleration_filter_requires_forecast_growth() -> None:
    profile = AccelerationFilter("test", 14, 42, Decimal("2"), Decimal("1.5"))

    values = _filter_kwargs(profile)

    assert values["hybrid_gap_acceleration_recent_days"] == 14
    assert values["hybrid_gap_acceleration_baseline_days"] == 42
    assert values["hybrid_gap_acceleration_min_recent_sales"] == Decimal("2")
    assert values["hybrid_gap_acceleration_rate_multiplier"] == Decimal("1.5")
    assert values["hybrid_gap_acceleration_require_forecast_growth"] is True


def test_profile_selection_uses_pre_final_period_only() -> None:
    rows = []
    for role, pre, final in (
        ("p50_acceleration_fast", "10", "100"),
        ("p50_acceleration_balanced", "20", "-5"),
        ("p50_acceleration_strict", "15", "1000"),
    ):
        rows.extend(
            [
                {
                    "scenario_role": role,
                    "period": "pre_july",
                    "economic_contribution_delta_rub": pre,
                    "served_observed_delta_qty": "1",
                },
                {
                    "scenario_role": role,
                    "period": "july",
                    "economic_contribution_delta_rub": final,
                    "served_observed_delta_qty": "1",
                },
            ]
        )

    selected = _select_profile(rows)

    assert selected["scenario_role"] == "p50_acceleration_balanced"
    assert selected["final_month_economic_contribution_delta_rub"] == "-5"
    assert selected["positive_on_holdout"] is False


def test_period_comparison_keeps_every_acceleration_role() -> None:
    rows = []
    roles = ["control_v19", "p50_acceleration_fast", "p50_acceleration_balanced"]
    for role in roles:
        for period, date_from, date_to in (
            ("pre_july", "2026-01-01", "2026-06-30"),
            ("july", "2026-07-01", "2026-07-31"),
        ):
            increment = Decimal("0") if role == "control_v19" else Decimal("2")
            rows.append(
                {
                    "scenario_role": role,
                    "period": period,
                    "strategy": "model",
                    "date_from": date_from,
                    "date_to": date_to,
                    "served_qty": str(10 + increment),
                    "served_observed_qty": str(8 + increment),
                    "gross_profit_rub": str(100 + increment),
                    "average_inventory_value_rub": "50",
                }
            )

    output = _period_delta_rows(
        rows,
        roles=roles[1:],
        annual_rate=Decimal("0.65"),
    )

    assert {(row["scenario_role"], row["period"]) for row in output} == {
        ("p50_acceleration_fast", "pre_july"),
        ("p50_acceleration_fast", "july"),
        ("p50_acceleration_balanced", "pre_july"),
        ("p50_acceleration_balanced", "july"),
    }


def test_manual_queue_separates_fast_shadow_from_manual_only() -> None:
    base = {
        "scenario_role": "hybrid_p50",
        "decision_date": "2026-06-01",
        "nomenclature_code": "SKU-1",
        "status": "sale",
        "hybrid_gap_order_component_qty": "3",
        "hybrid_gap_new_arrival_date": "2026-06-20",
        "hybrid_gap_reliable_arrival_date": "2026-07-01",
    }
    fast = {
        **base,
        "scenario_role": "p50_acceleration_fast",
        "hybrid_gap_order_component_qty": "2",
        "hybrid_gap_acceleration_recent_sales_qty": "4",
        "hybrid_gap_acceleration_baseline_sales_qty": "1",
        "hybrid_gap_acceleration_recent_rate": "0.57",
        "hybrid_gap_acceleration_baseline_rate": "0.04",
    }

    rows = _manual_review_rows(
        [base, fast],
        names={"SKU-1": "Товар"},
        margins={"SKU-1": Decimal("100")},
    )

    assert len(rows) == 1
    assert rows[0]["recommended_contour"] == "shadow_auto_candidate"
    assert rows[0]["fast_filter_passed"] == 1
    assert rows[0]["fast_recommended_gap_qty"] == "2"
    assert rows[0]["estimated_gross_margin_at_risk_rub"] == "300"
