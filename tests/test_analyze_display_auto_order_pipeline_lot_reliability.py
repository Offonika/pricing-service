from decimal import Decimal

from tasks.analyze_display_auto_order_pipeline_lot_reliability import (
    PROFILES,
    attach_profile_scores,
    economic_incremental_buffers,
    reconstruct_lot_age_features,
    score_profile,
    select_economic_strategy,
    top_share_buffers,
)


def _opportunity(day: str, *, incoming: str = "0") -> dict[str, str]:
    return {
        "opportunity_id": f"SKU-1:{day}",
        "nomenclature_code": "SKU-1",
        "decision_date": day,
        "period": "pre_final_month",
        "lead_time_p50_days": "1",
        "as_of_lead_time_p75_days": "2",
        "as_of_free_incoming_qty": incoming,
        "as_of_free_stock_qty": "0",
        "forecast_demand_qty": "10",
        "as_of_sales_30": "0",
        "forecast_rate_sales": "0",
        "horizon_days": "10",
        "shortage_expected_qty": "0",
        "shortage_risk_probability": "0.2",
        "risk_training_sufficient": "1",
    }


def test_reconstruct_lot_age_keeps_opening_left_censored_and_fifo_closes() -> None:
    opportunities = [
        _opportunity("2026-02-02", incoming="14"),
        _opportunity("2026-02-04", incoming="2"),
    ]
    daily_rows = [
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "gross_incoming_qty": "10",
        },
        {
            "business_date": "2026-02-02",
            "nomenclature_code": "SKU-1",
            "gross_incoming_qty": "14",
        },
        {
            "business_date": "2026-02-03",
            "nomenclature_code": "SKU-1",
            "gross_incoming_qty": "8",
        },
        {
            "business_date": "2026-02-04",
            "nomenclature_code": "SKU-1",
            "gross_incoming_qty": "2",
        },
    ]

    rows, quality = reconstruct_lot_age_features(opportunities, daily_rows)

    assert rows[0]["lot_unknown_age_pipeline_qty"] == Decimal("10")
    assert rows[0]["lot_known_age_pipeline_qty"] == Decimal("4")
    assert rows[1]["lot_unknown_age_pipeline_qty"] == Decimal("0")
    assert rows[1]["lot_known_age_pipeline_qty"] == Decimal("2")
    assert rows[1]["lot_p50_to_p75_pipeline_qty"] == Decimal("2")
    assert quality["unmatched_fifo_close_qty"] == "0"
    assert quality["max_reconstructed_balance_difference_qty"] == "0"


def test_future_pipeline_close_does_not_change_past_feature() -> None:
    opportunities = [_opportunity("2026-02-02", incoming="4")]
    rows, _ = reconstruct_lot_age_features(
        opportunities,
        [
            {
                "business_date": "2026-02-01",
                "nomenclature_code": "SKU-1",
                "gross_incoming_qty": "0",
            },
            {
                "business_date": "2026-02-02",
                "nomenclature_code": "SKU-1",
                "gross_incoming_qty": "4",
            },
            {
                "business_date": "2026-02-10",
                "nomenclature_code": "SKU-1",
                "gross_incoming_qty": "0",
            },
        ],
    )

    assert rows[0]["lot_known_age_pipeline_qty"] == Decimal("4")
    assert rows[0]["lot_oldest_known_age_days"] == 0


def test_quantile_profile_discounts_lot_and_keeps_acceleration_separate() -> None:
    row = {
        **_opportunity("2026-02-04", incoming="8"),
        "lot_total_pipeline_qty": "8",
        "lot_unknown_age_pipeline_qty": "2",
        "lot_within_p50_pipeline_qty": "2",
        "lot_p50_to_p75_pipeline_qty": "2",
        "lot_over_p75_pipeline_qty": "2",
        "as_of_sales_30": "60",
        "forecast_rate_sales": "1",
    }
    quantile = next(profile for profile in PROFILES if profile.name == "lot_quantile_mass")
    acceleration = next(
        profile for profile in PROFILES if profile.name == "lot_quantile_mass_acceleration_all"
    )

    lot_only = score_profile(row, quantile)
    with_acceleration = score_profile(row, acceleration)

    assert lot_only["reliable_free_incoming_qty"] == Decimal("5.5")
    assert lot_only["lot_shortage_qty"] == Decimal("4.5")
    assert lot_only["acceleration_addon_qty"] == Decimal("0")
    assert with_acceleration["acceleration_addon_qty"] == Decimal("10")
    assert with_acceleration["score"] == Decimal("14.5")


def test_acceleration_gate_requires_an_aged_known_lot() -> None:
    row = {
        **_opportunity("2026-02-04", incoming="8"),
        "lot_total_pipeline_qty": "8",
        "lot_unknown_age_pipeline_qty": "8",
        "lot_within_p50_pipeline_qty": "0",
        "lot_p50_to_p75_pipeline_qty": "0",
        "lot_over_p75_pipeline_qty": "0",
        "as_of_sales_30": "60",
        "forecast_rate_sales": "1",
    }
    gated = next(
        profile for profile in PROFILES if profile.name == "lot_quantile_mass_acceleration_p50"
    )

    assert score_profile(row, gated)["acceleration_addon_qty"] == Decimal("0")

    row["lot_unknown_age_pipeline_qty"] = "6"
    row["lot_p50_to_p75_pipeline_qty"] = "2"
    assert score_profile(row, gated)["acceleration_addon_qty"] == Decimal("10")


def test_top_share_ranking_uses_profile_score() -> None:
    profile = next(profile for profile in PROFILES if profile.name == "lot_quantile_mass")
    rows = attach_profile_scores(
        [
            {
                **_opportunity("2026-02-04"),
                "opportunity_id": "low",
                "lot_total_pipeline_qty": "0",
                "shortage_expected_qty": "1",
                "as_of_free_stock_qty": "9",
            },
            {
                **_opportunity("2026-02-04"),
                "opportunity_id": "high",
                "lot_total_pipeline_qty": "0",
                "shortage_expected_qty": "3",
                "as_of_free_stock_qty": "7",
            },
        ]
    )

    buffers = top_share_buffers(rows, profile)

    assert buffers == {"high": Decimal("3")}


def _economic_row(
    opportunity_id: str,
    *,
    baseline: str,
    challenger: str,
    probability: str = "0.5",
    cost: str = "100",
    margin: str = "100",
) -> dict[str, str]:
    return {
        "opportunity_id": opportunity_id,
        "nomenclature_code": opportunity_id,
        "decision_date": "2026-06-01",
        "risk_training_sufficient": "1",
        "shortage_risk_probability": probability,
        "inventory_cost_per_unit_rub": cost,
        "gross_margin_per_unit_rub": margin,
        "baseline_v19_score": baseline,
        "lot_quantile_mass_acceleration_p50_w75_score": challenger,
    }


def test_economic_ranking_never_reduces_baseline_service_buffers() -> None:
    challenger = next(
        profile for profile in PROFILES if profile.name == "lot_quantile_mass_acceleration_p50_w75"
    )
    rows = [
        _economic_row("a", baseline="10", challenger="10"),
        _economic_row("b", baseline="8", challenger="1"),
        _economic_row("c", baseline="1", challenger="9"),
        _economic_row("d", baseline="0", challenger="0"),
    ]

    baseline = top_share_buffers(
        rows,
        next(profile for profile in PROFILES if profile.name == "baseline_v19"),
    )
    buffers, allocation = economic_incremental_buffers(
        rows,
        challenger=challenger,
        incremental_share=Decimal("0.25"),
    )

    assert all(buffers[key] >= quantity for key, quantity in baseline.items())
    assert buffers["b"] == Decimal("8")
    assert buffers["c"] == Decimal("3")
    assert {row["opportunity_id"] for row in allocation} == {"c"}


def test_economic_ranking_applies_only_to_extra_and_keeps_unknown_cost_last() -> None:
    challenger = next(
        profile for profile in PROFILES if profile.name == "lot_quantile_mass_acceleration_p50_w75"
    )
    rows = [
        _economic_row("base-a", baseline="10", challenger="10"),
        _economic_row("base-b", baseline="9", challenger="0"),
        _economic_row("base-c", baseline="8", challenger="0"),
        _economic_row("known", baseline="0", challenger="4", cost="100", margin="200"),
        _economic_row("unknown", baseline="0", challenger="6", cost="0", margin="200"),
        _economic_row("zero", baseline="0", challenger="0"),
    ]

    buffers, allocation = economic_incremental_buffers(
        rows,
        challenger=challenger,
        incremental_share=Decimal("0.50"),
    )

    assert [row["opportunity_id"] for row in allocation] == ["known", "unknown"]
    assert [row["allocated_extra_qty"] for row in allocation] == ["4", "1"]
    assert buffers["known"] == Decimal("4")
    assert buffers["unknown"] == Decimal("1")
    assert buffers["base-b"] == Decimal("9")


def test_incremental_shares_are_deterministic_whole_quantities() -> None:
    challenger = next(
        profile for profile in PROFILES if profile.name == "lot_quantile_mass_acceleration_p50_w75"
    )
    rows = [
        _economic_row("a", baseline="10", challenger="10"),
        _economic_row("b", baseline="8", challenger="0"),
        _economic_row("extra", baseline="0", challenger="8"),
        _economic_row("zero", baseline="0", challenger="0"),
    ]

    allocated = []
    for share in (Decimal("0.25"), Decimal("0.50"), Decimal("0.75"), Decimal("1")):
        _, rows_for_share = economic_incremental_buffers(
            rows,
            challenger=challenger,
            incremental_share=share,
        )
        allocated.append(rows_for_share[0]["allocated_extra_qty"])

    assert allocated == ["2", "4", "6", "8"]


def test_economic_strategy_selection_does_not_use_july_holdout() -> None:
    pre_rows = [
        {
            "period": "pre_final_month",
            "strategy": "economic_extra_0.25",
            "incremental_share": "0.25",
            "covered_loss_delta_qty": "2",
            "covered_margin_delta_proxy_rub": "200",
            "incremental_margin_per_excess_value": "2",
        },
        {
            "period": "pre_final_month",
            "strategy": "economic_extra_0.50",
            "incremental_share": "0.50",
            "covered_loss_delta_qty": "4",
            "covered_margin_delta_proxy_rub": "400",
            "incremental_margin_per_excess_value": "1",
        },
    ]
    july_a = [
        {
            "period": "final_month_exposed",
            "strategy": "economic_extra_0.25",
            "incremental_share": "0.25",
            "covered_loss_delta_qty": "0",
            "covered_margin_delta_proxy_rub": "0",
            "incremental_margin_per_excess_value": "0",
        }
    ]
    july_b = [
        {
            "period": "final_month_exposed",
            "strategy": "economic_extra_0.50",
            "incremental_share": "0.50",
            "covered_loss_delta_qty": "999",
            "covered_margin_delta_proxy_rub": "999999",
            "incremental_margin_per_excess_value": "999",
        }
    ]

    selected_a = select_economic_strategy(pre_rows + july_a)
    selected_b = select_economic_strategy(pre_rows + july_b)

    assert selected_a is not None
    assert selected_b is not None
    assert selected_a["strategy"] == "economic_extra_0.25"
    assert selected_b["strategy"] == "economic_extra_0.25"
