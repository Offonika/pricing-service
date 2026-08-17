from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from tasks.report_iphone17_pro_max_cold_start_backtest import (
    TRANSITION_PROFILES,
    Candidate,
    WordstatHistory,
    WordstatPolicy,
    _largest_remainder,
    _mode,
    _sensitivity_summary,
    analog_profile,
    candidate_grid,
    display_segment_id,
    first_family_batch_quantity,
    generation_from_name,
    lead_time_profile,
    planned_arrival_date,
    quality_segment,
    segment_evidence_days,
    wordstat_policy_grid,
)


def test_family_parser_does_not_split_sim_esim_variants() -> None:
    assert (
        generation_from_name(
            "Дисплей для Apple iPhone 17 Pro Max (SIM + eSIM) / "
            "iPhone 17 Pro Max (eSIM) + тачскрин"
        )
        == 17
    )


def test_quality_segments_are_not_one_full_analog_group() -> None:
    assert quality_segment("Дисплей (ORIG100) (SP)") == "Original"
    assert quality_segment("Дисплей (JK) (Soft Oled)") == "Soft OLED"
    assert quality_segment("Дисплей (F5ENERGY) (Hard Oled)") == "Hard OLED"
    assert quality_segment("Дисплей (F5ENERGY) (In-Cell)") == "In-Cell"


def test_substitution_segment_includes_construction() -> None:
    assert display_segment_id("Дисплей Original в рамке") != display_segment_id(
        "Дисплей Original без рамки"
    )


def test_modeled_arrival_uses_same_p75_as_coverage() -> None:
    assert planned_arrival_date(date(2026, 1, 10), {"p50": 30, "p75": 75}) == date(2026, 3, 26)


def test_mode_availability_requires_every_launched_segment() -> None:
    history = {
        "Original|without_frame": {date(2026, 1, day) for day in range(1, 9)},
        "Soft OLED|without_frame": {date(2026, 1, day) for day in range(1, 31)},
    }

    assert segment_evidence_days(history, history) == 8


class _LeadTimeFixture:
    codes_by_generation = {16: ["analog"], 17: ["target"]}
    orders = [
        {
            "order_hash": "completed",
            "nomenclature_code": "analog",
            "order_date": "2026-01-01",
            "cargo_date": "2026-01-02",
            "quantity": 100,
        },
        {
            "order_hash": "matured-unreceived",
            "nomenclature_code": "analog",
            "order_date": "2026-01-01",
            "cargo_date": "2026-01-02",
            "quantity": 100,
        },
        {
            "order_hash": "recent-unreceived",
            "nomenclature_code": "analog",
            "order_date": "2026-04-01",
            "cargo_date": "2026-04-02",
            "quantity": 1000,
        },
    ]
    receipts = [
        {
            "order_hash": "completed",
            "nomenclature_code": "analog",
            "receipt_date": "2026-01-31",
            "quantity": 100,
        }
    ]


def test_lead_time_reliability_uses_matured_cohorts_and_keeps_failures() -> None:
    profile = lead_time_profile(
        _LeadTimeFixture(),
        target_generation=17,
        decision_date=date(2026, 4, 15),
        mode="cold_start",
    )

    assert profile["matured_sample_count"] == 2
    assert profile["right_censored_count"] == 1
    assert profile["placed_reliability"] == 0.5
    assert profile["cargo_reliability"] == 0.5


def test_skip_grid_sensitivity_has_no_empty_median_crash() -> None:
    candidate = candidate_grid(repair_lags=(0,))[0]

    class _Result:
        lost_sales_qty = 1.0
        gmroi = 1.0
        average_inventory_cost_rub = 1.0
        served_sales_qty = 1.0
        served_sales_ratio = 1.0
        ending_excess_qty = 0.0
        ending_shortfall_qty = 0.0

        def summary_row(self):
            return {}

    summary = _sensitivity_summary(
        [(candidate, _Result())],
        {
            "served_sales_qty": 1,
            "average_inventory_cost_rub": 1,
            "gmroi": 1,
            "ending_excess_qty": 0,
            "ending_shortfall_qty": 0,
        },
    )

    assert summary["early_cap_vs_zero"]["0.25"]["comparison_count"] == 0
    assert summary["early_cap_vs_zero"]["0.25"]["median_served_sales_delta_qty"] is None


def test_family_allocation_sums_once_instead_of_copying_demand_to_every_sku() -> None:
    allocation = _largest_remainder(
        11,
        {
            "original": 0.45,
            "soft": 0.40,
            "hard": 0.10,
            "incell": 0.05,
        },
    )

    assert sum(allocation.values()) == 11
    assert max(allocation.values()) < 11


def test_candidate_grid_includes_zero_early_repeat_control() -> None:
    training = candidate_grid(repair_lags=(0,))
    holdout = candidate_grid(repair_lags=(0, 30, 60, 90))

    assert len(training) == 432
    assert len(holdout) == 1728
    assert {row.early_reorder_cap_ratio for row in training} == {0.0, 0.25, 0.5, 1.0}


def test_wordstat_policy_grid_has_disabled_control_and_fitted_caps() -> None:
    policies = wordstat_policy_grid()

    assert len(policies) == 73
    assert sum(policy.max_uplift_ratio == 0 for policy in policies) == 1
    assert {policy.max_uplift_ratio for policy in policies} == {0.0, 0.1, 0.2}


def test_first_family_batch_excludes_future_quality_launches() -> None:
    orders = {
        "first": {"order_date": "2025-11-05", "quantity": 20},
        "second_quality": {"order_date": "2026-01-04", "quantity": 20},
        "third_quality": {"order_date": "2026-02-26", "quantity": 50},
    }

    assert first_family_batch_quantity(orders, date(2025, 11, 5)) == 20


def test_mode_transition_depends_on_evidence_not_elapsed_days() -> None:
    profile = TRANSITION_PROFILES["balanced"]

    assert _mode(served_qty=7, sale_days=5, available_days=200, profile=profile) == "cold_start"
    assert _mode(served_qty=8, sale_days=5, available_days=10, profile=profile) == "hybrid"
    assert _mode(served_qty=36, sale_days=14, available_days=28, profile=profile) == "own_history"


class _AnalogFixture:
    def __init__(self) -> None:
        self.codes_by_generation = {16: ["sku16"]}
        self.first_family_available = {16: date(2025, 3, 19)}
        self.family_daily = {16: {}}
        for offset in range(180):
            business_date = date(2025, 3, 19) + timedelta(days=offset)
            self.family_daily[16][business_date.isoformat()] = {
                "available": True,
                "sales_qty": 1.0,
                "sales_by_segment": defaultdict(float, {"Original": 1.0}),
            }


def test_launch_plan_uses_only_analog_dates_before_decision() -> None:
    candidate = Candidate(
        analog_pool="previous_one",
        analog_window_days=14,
        repair_lag_days=90,
        hybrid_prior_days=14,
        early_reorder_cap_ratio=0.0,
        temporary_buffer_days=7,
        transition_profile="strict",
    )

    rate, mix, evidence = analog_profile(
        _AnalogFixture(),
        target_generation=17,
        decision_date=date(2025, 11, 5),
        target_first_available=None,
        candidate=candidate,
    )

    profile = evidence["profiles"][0]
    assert rate == 1.0
    assert mix == {"Original": 1.0}
    assert profile["method"] == "launch_plan"
    assert profile["window_from"] == "2025-06-17"
    assert profile["window_to"] == "2025-06-30"
    assert date.fromisoformat(profile["window_to"]) < date(2025, 11, 5)


class _SegmentedAnalogFixture:
    def __init__(self) -> None:
        self.codes_by_generation = {16: ["original", "soft"]}
        self.first_family_available = {16: date(2025, 3, 19)}
        self.family_daily = {16: {}}
        for offset in range(10):
            business_date = date(2025, 6, 17) + timedelta(days=offset)
            soft_available = offset < 2
            self.family_daily[16][business_date.isoformat()] = {
                "available": True,
                "sales_qty": 1.0 + float(soft_available),
                "sales_by_segment": defaultdict(
                    float,
                    {
                        "Original|without_frame": 1.0,
                        "Soft OLED|without_frame": float(soft_available),
                    },
                ),
                "available_by_segment": defaultdict(
                    bool,
                    {
                        "Original|without_frame": True,
                        "Soft OLED|without_frame": soft_available,
                    },
                ),
            }


def test_analog_rate_uses_availability_of_each_quality_construction_segment() -> None:
    candidate = Candidate(
        analog_pool="previous_one",
        analog_window_days=10,
        repair_lag_days=90,
        hybrid_prior_days=14,
        early_reorder_cap_ratio=0.0,
        temporary_buffer_days=7,
        transition_profile="strict",
    )

    rate, mix, _ = analog_profile(
        _SegmentedAnalogFixture(),
        target_generation=17,
        decision_date=date(2025, 11, 5),
        target_first_available=None,
        candidate=candidate,
    )

    assert rate == 2.0
    assert mix == {
        "Original|without_frame": 0.5,
        "Soft OLED|without_frame": 0.5,
    }


def test_wordstat_signal_uses_only_completed_available_months_and_never_sums_counts() -> None:
    queries = []
    shares = {
        "display": (1.0, 1.1, 2.0),
        "screen": (1.0, 0.9, 2.0),
        "repair": (1.0, 1.2, 1.0),
    }
    for phrase_key, values in shares.items():
        queries.append(
            {
                "generation": 17,
                "phrase_key": phrase_key,
                "phrase": f"fixture {phrase_key}",
                "response": {
                    "results": [
                        {
                            "date": f"2025-{month:02d}-01T00:00:00Z",
                            "count": str(100 * month),
                            "share": value,
                        }
                        for month, value in zip((10, 11, 12), values, strict=True)
                    ]
                },
            }
        )
    history = WordstatHistory(
        {
            "schema": "iphone17_pro_max_wordstat_history.v1",
            "queries": queries,
        }
    )
    policy = WordstatPolicy(
        availability_lag_days=7,
        lookback_months=1,
        min_confirming_phrases=2,
        min_growth_ratio=0.05,
        max_uplift_ratio=0.20,
    )

    before_december_is_available = history.signal_at(
        generation=17,
        decision_date=date(2026, 1, 5),
        policy=policy,
    )
    after_december_is_available = history.signal_at(
        generation=17,
        decision_date=date(2026, 1, 12),
        policy=policy,
    )

    assert before_december_is_available["latest_period"] == "2025-11-01"
    assert after_december_is_available["latest_period"] == "2025-12-01"
    assert after_december_is_available["status"] == "confirmed_uptrend"
    assert after_december_is_available["modifier"] == 1.2
    assert after_december_is_available["counts_aggregated"] is False
    assert "total_count" not in after_december_is_available
