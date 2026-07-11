from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.procurement_b2b_customer_demand import (
    infer_profile_as_of_exclusive,
    load_b2b_customer_demand_profiles,
)


def test_load_b2b_customer_demand_profiles_aggregates_active_and_passive(tmp_path) -> None:
    path = tmp_path / "display-b2b-customer-sku-demand-2026-07-10.csv"
    path.write_text(
        "counterparty_ref,activity_status,sku,units_270,units_recent_90,"
        "units_previous_90,expected_customer_purchase_date,"
        "recency_weighted_daily_rate,dependency_reading,"
        "active_high_tier_share_pct\n"
        "C1,Активный,RB1,12,5,4,2026-07-18,0.12,"
        "Клиенты 3/4/5 формируют основную часть,75\n"
        "C2,Активный,RB1,8,3,2,2026-09-01,0.08,"
        "Клиенты 3/4/5 формируют основную часть,75\n"
        "C3,Пассивный,RB1,6,2,1,2026-06-01,0.04,"
        "Клиенты 3/4/5 формируют основную часть,75\n",
        encoding="utf-8-sig",
    )

    profiles = load_b2b_customer_demand_profiles(path)
    profile = profiles["RB1"]

    assert profile.profile_as_of_exclusive == date(2026, 7, 10)
    assert profile.managed_sales_qty_180 == Decimal("17")
    assert profile.managed_sales_qty_270 == Decimal("26")
    assert profile.active_customer_count == 2
    assert profile.passive_customer_count == 1
    assert profile.due_customer_count(as_of=date(2026, 7, 10), horizon_days=30) == 1
    assert profile.active_daily_rate_due(
        as_of=date(2026, 7, 10),
        horizon_days=30,
    ) == Decimal("0.12")
    assert profile.active_daily_rate_due(
        as_of=date(2026, 7, 10),
        horizon_days=60,
    ) == Decimal("0.20")


def test_infer_profile_as_of_requires_date_in_filename(tmp_path) -> None:
    assert infer_profile_as_of_exclusive(
        tmp_path / "display-b2b-customer-sku-demand-2026-07-10.csv"
    ) == date(2026, 7, 10)
