from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.assortment_lifecycle import (
    AssortmentLifecycleDecision,
    AssortmentLifecycleInput,
    AssortmentStatus,
    ExpensiveProfileInput,
    ManagerNeedSignal,
    ProcurementBehaviorProfile,
    WarehouseSalesPointInput,
    build_procurement_profile_property_update_row,
    build_status_property_update_rows,
    classify_expensive_profile,
    decide_assortment_status,
    systemic_sales_point_codes,
    validate_manager_need_signal,
)


def _decision(**kwargs) -> AssortmentLifecycleDecision:
    return decide_assortment_status(AssortmentLifecycleInput(nomenclature_code="РБ0001", **kwargs))


def test_assortment_lifecycle_status_ladder() -> None:
    assert _decision().status == AssortmentStatus.FRUIT
    assert _decision(first_supplier_order_at=date(2026, 1, 10)).status == AssortmentStatus.NEWBORN
    assert (
        _decision(first_supplier_order_at=date(2026, 1, 10), has_need_signal=True).status
        == AssortmentStatus.NEWBORN_NEED
    )
    assert (
        _decision(supplier_order_cargo_handoff_dates=(date(2026, 1, 20),)).status
        == AssortmentStatus.NEW_ITEM
    )
    assert (
        _decision(supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20))).status
        == AssortmentStatus.SALES_START
    )
    assert (
        _decision(
            supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
            receipt_dates=(date(2026, 2, 25),),
        ).status
        == AssortmentStatus.SALE
    )


def test_working_requires_five_receipts_in_180_days_and_folder_confirmation() -> None:
    receipt_dates = (
        date(2026, 1, 25),
        date(2026, 2, 25),
        date(2026, 3, 25),
        date(2026, 4, 25),
        date(2026, 5, 25),
    )

    without_confirmation = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        receipt_dates=receipt_dates,
    )

    assert without_confirmation.status == AssortmentStatus.SALE
    assert without_confirmation.recommended_status == AssortmentStatus.WORKING
    assert without_confirmation.blockers == ("working_confirmation_required",)

    confirmed = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        receipt_dates=receipt_dates,
        working_confirmed_by_folder_responsible=True,
    )

    assert confirmed.status == AssortmentStatus.WORKING
    assert confirmed.auto_order_allowed


def test_exclusive_requires_reason_approver_date_and_manual_min_stock() -> None:
    missing = _decision(
        manual_status=AssortmentStatus.EXCLUSIVE,
        manual_reason="",
        manual_approved_by="",
        manual_changed_at=None,
        exclusive_min_stock_qty=None,
    )

    assert missing.status == AssortmentStatus.EXCLUSIVE
    assert set(missing.blockers) == {
        "manual_reason_required",
        "manual_approved_by_required",
        "manual_changed_at_required",
        "exclusive_min_stock_required",
    }
    assert missing.manual_review_required

    valid = _decision(
        manual_status=AssortmentStatus.EXCLUSIVE,
        manual_reason="Эксклюзивная позиция, держать наличие",
        manual_approved_by="Омар",
        manual_changed_at=date(2026, 6, 25),
        exclusive_min_stock_qty="2",
    )

    assert valid.blockers == ()
    assert valid.exclusive_min_stock_qty == Decimal("2")
    assert valid.exclusive_review_at == date(2026, 7, 25)
    rows = build_status_property_update_rows(valid, changed_at=date(2026, 6, 25))
    properties = {row.property_name: row for row in rows}
    assert properties["Статус ассортимента"].new_value_tag == "exclusive"
    assert properties["Ручной минимальный остаток"].new_value == Decimal("2")
    assert properties["Дата пересмотра правила наличия"].new_value == date(2026, 7, 25)


def test_expensive_profile_uses_top_quartile_and_route_days() -> None:
    group_values = ("100", "200", "300", "400")

    fast = classify_expensive_profile(
        ExpensiveProfileInput(item_value="300", group_values=group_values, route_days=7)
    )
    assert fast.profile == ProcurementBehaviorProfile.FAST_EXPENSIVE
    assert fast.threshold_value == Decimal("300")
    assert not fast.manual_review_required

    slow = classify_expensive_profile(
        ExpensiveProfileInput(item_value="300", group_values=group_values, route_days=8)
    )
    assert slow.profile == ProcurementBehaviorProfile.SLOW_EXPENSIVE
    assert slow.manual_review_required

    not_expensive = classify_expensive_profile(
        ExpensiveProfileInput(item_value="250", group_values=group_values, route_days=3)
    )
    assert not_expensive.profile is None
    assert not not_expensive.is_expensive


def test_expensive_profile_can_be_assigned_manually_and_exported() -> None:
    decision = classify_expensive_profile(
        ExpensiveProfileInput(
            item_value="50",
            group_values=("100", "200", "300", "400"),
            manual_profile="slow_expensive",
        )
    )

    row = build_procurement_profile_property_update_row(
        "РБ0001",
        decision,
        changed_at=date(2026, 6, 25),
        approved_by="Ответственный за папку",
    )

    assert decision.profile == ProcurementBehaviorProfile.SLOW_EXPENSIVE
    assert row is not None
    assert row.property_name == "Профиль закупочного поведения"
    assert row.new_value_name == "Дорогой медленный"
    assert row.new_value_tag == "slow_expensive"


def test_sales_points_exclude_central_defect_transit_and_non_systematic_sales() -> None:
    assert systemic_sales_point_codes(
        (
            WarehouseSalesPointInput("shop-1"),
            WarehouseSalesPointInput("central", is_central=True),
            WarehouseSalesPointInput("defect", is_defect_warehouse=True),
            WarehouseSalesPointInput("transit", is_transit=True),
            WarehouseSalesPointInput("rare", is_non_systematic_sale=True),
            WarehouseSalesPointInput("closed", sells_systematically=False),
        )
    ) == ("shop-1",)


def test_manager_need_signal_collects_facts_and_flags_suspicious_quantity() -> None:
    decision = validate_manager_need_signal(
        ManagerNeedSignal(
            nomenclature_code="РБ0001",
            manager_id="42",
            quantity="9",
            source="offline_call",
            signal_date=date(2026, 6, 25),
            comment="Клиент спрашивал",
        ),
        suspicious_quantity_threshold="5",
    )

    assert decision.accepted
    assert decision.suspicious
    assert decision.issues == ("suspicious_quantity",)
