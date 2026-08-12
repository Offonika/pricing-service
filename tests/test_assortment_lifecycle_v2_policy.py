from datetime import date
from decimal import Decimal

from app.services.assortment_lifecycle_v2_policy import (
    load_assortment_lifecycle_v2_policy,
)


def test_versioned_display_v2_policy_is_shadow_only_and_contains_full_grid() -> None:
    policy = load_assortment_lifecycle_v2_policy()
    assert policy.live_enabled is False
    assert policy.demand.growth_multiplier == Decimal("1.5")
    assert policy.demand.confirmation_days == 14
    assert policy.comparable_group_min_size == 8
    assert policy.reliable_incoming_basis == "cargo_handoff"
    assert policy.spike_quantity_policy == "ordinary_demand_only"
    assert policy.backtest_grid.growth_multipliers == (
        Decimal("1.2"),
        Decimal("1.5"),
        Decimal("2.0"),
    )
    assert policy.backtest_grid.confirmation_days == (7, 14, 21)
    assert policy.backtest_grid.spike_quantity_policies == (
        "ordinary_demand_only",
        "projected_shortage_cap",
        "one_open_lot_projected_shortage_cap",
    )
    assert policy.backtest_grid.comparable_group_min_sizes == (8, 12, 20)
    assert policy.periods.training_from == date(2026, 2, 1)
    assert policy.periods.training_to == date(2026, 6, 30)
    assert policy.periods.holdout_from == date(2026, 7, 1)
    assert policy.periods.holdout_to == date(2026, 7, 31)
    assert policy.acceptance_criteria == (
        "served_sales_not_worse",
        "gross_profit_not_worse",
        "economic_effect_non_negative",
        "gmroi_not_worse",
        "ending_excess_stock_not_worse",
    )
