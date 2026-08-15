from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from tasks.report_iphone17_pro_max_cold_start_backtest import (
    TRANSITION_PROFILES,
    Candidate,
    _largest_remainder,
    _mode,
    analog_profile,
    candidate_grid,
    first_family_batch_quantity,
    generation_from_name,
    quality_segment,
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
