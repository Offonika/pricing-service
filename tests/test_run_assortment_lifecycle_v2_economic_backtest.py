from datetime import date, timedelta
from decimal import Decimal

from tasks.display_auto_order_backtest_preflight import CarryingCostScenario
from tasks.report_display_auto_order_frozen_backtest import FrozenScenario, Metric, _summary
from tasks.run_assortment_lifecycle_v2_economic_backtest import (
    DemandProfile,
    RepresentationMinimumLookup,
    _demand_state,
    _target_status,
)


def _growth_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "sales_30": "12",
        "sales_90": "18",
        "sales_180": "24",
        "available_days_30": "30",
        "available_days_90": "90",
        "available_days_180": "180",
        "sales_active_days_30": "3",
        "sales_document_count_30": "3",
        "sales_customer_count_30": "3",
        "sales_point_count_30": "3",
        "sales_max_day_share_30": "0.4",
    }
    row.update(overrides)
    return row


def test_candidate_demand_profile_requires_consecutive_confirmation_days() -> None:
    profile = DemandProfile(Decimal("1.5"), 7, Decimal("0.5"), 2)
    started = date(2026, 1, 1)
    state, state_since, _rate = _demand_state(
        _growth_row(),
        profile=profile,
        previous_state=None,
        state_since=None,
        business_date=started,
    )
    assert state == "spike"

    state, state_since, _rate = _demand_state(
        _growth_row(),
        profile=profile,
        previous_state=state,
        state_since=state_since,
        business_date=started + timedelta(days=7),
    )
    assert state == "growing"
    assert state_since == started


def test_candidate_demand_profile_keeps_concentrated_growth_as_spike() -> None:
    state, _state_since, _rate = _demand_state(
        _growth_row(sales_max_day_share_30="0.8"),
        profile=DemandProfile(Decimal("1.2"), 7, Decimal("0.5"), 2),
        previous_state="spike",
        state_since=date(2026, 1, 1),
        business_date=date(2026, 1, 20),
    )
    assert state == "spike"


def test_active_stage_does_not_return_to_sales_start_on_spike() -> None:
    assert (
        _target_status(
            {"status": "working", "first_sale_at": "2025-01-01", "blockers": "[]"},
            demand_state="spike",
            previous_status="working",
        )
        == "working"
    )


def test_representation_lookup_skips_spike_and_uses_selected_bit() -> None:
    regular = (date(2026, 2, 1), "A")
    spike = (date(2026, 2, 1), "B")
    lookup = RepresentationMinimumLookup(
        eligibility_masks={regular: 0b10, spike: 0b10},
        bit=1,
        spike_keys={spike},
    )
    assert lookup.get(regular) == Decimal("13")
    assert lookup.get(spike) is None


def test_frozen_summary_reports_strict_ending_excess_stock() -> None:
    scenario = FrozenScenario(
        scenario_id="test",
        stage_profile="typical",
        kmp4_weight=Decimal("0"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )
    summary = _summary(
        scenario=scenario,
        strategy="model",
        metrics={
            "A": Metric(
                ending_inventory_qty=Decimal("20"),
                ending_target_stock_qty=Decimal("13"),
            ),
            "B": Metric(
                ending_inventory_qty=Decimal("3"),
                ending_target_stock_qty=Decimal("5"),
            ),
        },
        period_days=30,
    )
    assert summary["ending_excess_stock_qty"] == "7"
