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
