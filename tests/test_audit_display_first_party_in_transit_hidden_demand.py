from datetime import date
from decimal import Decimal

from tasks.audit_display_first_party_in_transit_hidden_demand import (
    FirstPartyMilestone,
    HistoricalProfile,
    _valid_cargo_date,
    build_queue_episodes,
    calculate_daily_audit,
)


def _milestone(*, quantity: str = "10") -> FirstPartyMilestone:
    return FirstPartyMilestone(
        nomenclature_code="SKU-1",
        name="Товар",
        first_supplier_order_at=date(2026, 1, 1),
        first_cargo_at=date(2026, 1, 5),
        first_physical_inflow_at=date(2026, 2, 10),
        first_sale_at=None,
        first_party_qty=Decimal(quantity),
        first_party_qty_known=True,
    )


def _profile(*, p75: int = 45) -> HistoricalProfile:
    return HistoricalProfile(
        business_date=date(2026, 1, 1),
        launch_typical_min_qty=Decimal("10"),
        launch_typical_max_qty=Decimal("20"),
        lead_time_p75_days=p75,
        launch_profile_known=True,
        lead_time_known=True,
    )


def _fact(**overrides):
    row = {
        "business_date": "2026-01-10",
        "status": "newborn",
        "physical_stock_qty": "0",
        "effective_reserve_qty": "0",
        "free_incoming_qty": "10",
        "kmp4_open_qty": "4",
        "site_order_open_qty": "0",
        "site_cart_open_qty": "0",
        "reserve_backlog_open_qty": "0",
        "site_soft_trigger_count": "0",
    }
    row.update(overrides)
    return row


def test_technical_cargo_date_is_not_operational_fact() -> None:
    assert _valid_cargo_date("1753-01-01") is None
    assert _valid_cargo_date("2026-01-05") == date(2026, 1, 5)


def test_hidden_need_is_added_once_after_first_party_covers_launch() -> None:
    row = calculate_daily_audit(fact=_fact(), milestone=_milestone(), profile=_profile())

    assert Decimal(row["weighted_hidden_demand_qty"]) == Decimal("2")
    assert row["reliable_first_party_qty"] == "10"
    assert row["uncovered_start_need_qty"] == "0"
    assert Decimal(row["uncovered_hidden_need_qty"]) == Decimal("2")
    assert row["top_up_queue_open"] == 1
    assert row["top_up_review_qty"] == "2"


def test_old_cargo_is_not_counted_as_reliable_coverage() -> None:
    row = calculate_daily_audit(
        fact=_fact(business_date="2026-02-06"),
        milestone=_milestone(),
        profile=_profile(p75=30),
    )

    assert row["first_party_reliable_by_age"] == 0
    assert row["reliable_first_party_qty"] == "0"
    assert Decimal(row["uncovered_combined_need_qty"]) == Decimal("12")
    assert "cargo_older_than_p75" in row["blocker_codes"]


def test_soft_signal_does_not_open_quantity_queue() -> None:
    row = calculate_daily_audit(
        fact=_fact(kmp4_open_qty="0", site_soft_trigger_count="3", free_incoming_qty="0"),
        milestone=_milestone(),
        profile=_profile(),
    )

    assert row["uncovered_combined_need_qty"] == "10.00"
    assert row["top_up_queue_open"] == 0
    assert row["top_up_review_qty"] == "0"


def test_queue_episode_reports_calculation_gap_over_five_days() -> None:
    rows = []
    for day in range(1, 8):
        rows.append(
            {
                "business_date": f"2026-01-{day:02d}",
                "nomenclature_code": "SKU-1",
                "name": "Товар",
                "top_up_queue_open": 1,
                "top_up_review_qty": "2",
                "strong_signal_sources": "kmp4",
            }
        )

    episodes = build_queue_episodes(rows, calculation_dates={"SKU-1": {date(2026, 1, 1)}})

    assert len(episodes) == 1
    assert episodes[0]["maximum_calculation_gap_days"] == 6
    assert episodes[0]["calculation_cadence_over_5_days"] == 1
