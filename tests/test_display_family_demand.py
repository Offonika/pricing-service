from datetime import date
from decimal import Decimal

import pytest

from app.services.display_family_demand import (
    allocate_display_family_order_pool,
    allocate_display_family_rates,
    build_display_family_members,
    build_display_family_profit_protection,
    build_display_family_regular_topup,
    build_regular_topup_delivery_overrides,
    display_construction_segment,
    display_quality_segment,
    freeze_display_family_order_trajectory,
)


def _members():
    return build_display_family_members(
        [
            {
                "nomenclature_code": "SOFT_JK",
                "name": "Дисплей для Apple iPhone 14 Pro Max (JK) (Soft Oled) (площадка под IC)",
                "model_tokens": ("apple:model:iphone 14 pro max",),
                "display_quality": "Copy High",
                "display_has_frame": False,
            },
            {
                "nomenclature_code": "SOFT_GX",
                "name": "Дисплей для Apple iPhone 14 Pro Max (GX ORIG) (Soft Oled) (площадка под IC)",
                "model_tokens": ("apple:model:iphone 14 pro max",),
                "display_quality": "Copy High",
                "display_has_frame": False,
            },
            {
                "nomenclature_code": "INCELL",
                "name": "Дисплей для Apple iPhone 14 Pro Max (JK) (In-Cell)",
                "model_tokens": ("apple:model:iphone 14 pro max",),
                "display_has_frame": False,
            },
        ]
    )


def test_display_family_groups_sim_esim_names_by_physical_model_token() -> None:
    members = build_display_family_members(
        [
            {
                "nomenclature_code": "SIM",
                "name": "Дисплей iPhone 17 Pro Max (SIM + eSIM)",
                "model_tokens": ("apple:model:iphone 17 pro max",),
            },
            {
                "nomenclature_code": "ESIM",
                "name": "Дисплей iPhone 17 Pro Max (eSIM)",
                "model_tokens": ("apple:model:iphone 17 pro max",),
            },
        ]
    )

    assert members["SIM"].family_id == members["ESIM"].family_id


def test_display_family_uses_singleton_when_model_tokens_are_missing() -> None:
    members = build_display_family_members(
        [
            {"nomenclature_code": "A", "name": "Дисплей неизвестный", "model_tokens": ()},
            {"nomenclature_code": "B", "name": "Дисплей неизвестный", "model_tokens": ()},
        ]
    )

    assert members["A"].family_id != members["B"].family_id
    assert members["A"].family_id.startswith("display-singleton-")


def test_display_family_members_exclude_bitok_before_grouping() -> None:
    members = build_display_family_members(
        [
            {
                "nomenclature_code": "BITOK",
                "name": "Дисплей iPhone 17 Pro Max (биток)",
                "model_tokens": ["iphone 17 pro max"],
            },
            {
                "nomenclature_code": "OK",
                "name": "Дисплей iPhone 17 Pro Max",
                "model_tokens": ["iphone 17 pro max"],
            },
        ]
    )

    assert set(members) == {"OK"}


def test_display_family_does_not_merge_transitive_partial_compatibility() -> None:
    members = build_display_family_members(
        [
            {
                "nomenclature_code": "A_ONLY",
                "name": "Дисплей Model A",
                "model_tokens": ("brand:model:a",),
            },
            {
                "nomenclature_code": "A_B",
                "name": "Дисплей Model A / Model B",
                "model_tokens": ("brand:model:a", "brand:model:b"),
            },
            {
                "nomenclature_code": "B_ONLY",
                "name": "Дисплей Model B",
                "model_tokens": ("brand:model:b",),
            },
        ]
    )

    assert len({member.family_id for member in members.values()}) == 3


def test_display_family_ignores_generic_codes_when_model_signature_matches() -> None:
    members = build_display_family_members(
        [
            {
                "nomenclature_code": "WITH_CODE",
                "name": "Дисплей Model A",
                "model_tokens": ("brand:model:a", "brand:code:lx1"),
            },
            {
                "nomenclature_code": "WITHOUT_CODE",
                "name": "Дисплей Model A",
                "model_tokens": ("brand:model:a",),
            },
        ]
    )

    assert members["WITH_CODE"].family_id == members["WITHOUT_CODE"].family_id


def test_display_segments_keep_quality_and_construction_separate() -> None:
    assert display_quality_segment("GX ORIG Soft OLED") == "soft_oled"
    assert display_quality_segment("JK In-Cell") == "in_cell"
    assert (
        display_construction_segment("Дисплей в рамке, площадка под IC, ALS шлейф")
        == "with_frame+ic_pad+als_flex"
    )


def test_family_allocation_preserves_total_rate_and_moves_share_inside_segment() -> None:
    members = _members()
    result = allocate_display_family_rates(
        members,
        baseline_rates={
            "SOFT_JK": Decimal("1"),
            "SOFT_GX": Decimal("2"),
            "INCELL": Decimal("3"),
        },
        recent_sales={
            "SOFT_JK": Decimal("9"),
            "SOFT_GX": Decimal("1"),
            "INCELL": Decimal("10"),
        },
        blend=Decimal("1"),
    )

    assert sum((row.allocated_rate for row in result.values()), Decimal("0")) == Decimal("6")
    assert result["SOFT_JK"].allocated_rate == Decimal("2.7")
    assert result["SOFT_GX"].allocated_rate == Decimal("0.3")
    assert result["INCELL"].allocated_rate == Decimal("3")


def test_family_allocation_blends_with_baseline_without_double_counting() -> None:
    members = _members()
    result = allocate_display_family_rates(
        members,
        baseline_rates={
            "SOFT_JK": Decimal("1"),
            "SOFT_GX": Decimal("2"),
            "INCELL": Decimal("3"),
        },
        recent_sales={
            "SOFT_JK": Decimal("9"),
            "SOFT_GX": Decimal("1"),
            "INCELL": Decimal("10"),
        },
        blend=Decimal("0.5"),
    )

    assert sum((row.allocated_rate for row in result.values()), Decimal("0")) == Decimal("6")
    assert result["SOFT_JK"].allocated_rate == Decimal("1.85")
    assert result["SOFT_GX"].allocated_rate == Decimal("1.15")
    assert result["INCELL"].allocated_rate == Decimal("3")


def test_family_allocation_falls_back_to_baseline_when_recent_sales_are_zero() -> None:
    members = _members()
    baseline = {
        "SOFT_JK": Decimal("1"),
        "SOFT_GX": Decimal("2"),
        "INCELL": Decimal("3"),
    }
    result = allocate_display_family_rates(
        members,
        baseline_rates=baseline,
        recent_sales={},
        blend=Decimal("1"),
    )

    assert {code: row.allocated_rate for code, row in result.items()} == baseline


def test_family_allocation_rejects_invalid_blend() -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        allocate_display_family_rates(
            _members(),
            baseline_rates={"SOFT_JK": Decimal("1")},
            recent_sales={},
            blend=Decimal("1.1"),
        )


def _order_row(code: str, quantity: str, cost: str = "100", position: str = "0") -> dict[str, str]:
    return {
        "decision_date": "2026-04-01",
        "nomenclature_code": code,
        "ordinary_recommended_order_qty": quantity,
        "inventory_cost_per_unit_rub": cost,
        "inventory_position_qty": position,
        "expected_arrival_date": "2026-04-20",
    }


def test_family_order_pool_preserves_quantity_and_capital_inside_segment() -> None:
    members = _members()
    overrides, audit = allocate_display_family_order_pool(
        [_order_row("SOFT_JK", "8", "200"), _order_row("SOFT_GX", "2", "100")],
        members=members,
        sales_by_code={
            "SOFT_JK": {date(2026, 3, 31): Decimal("1")},
            "SOFT_GX": {date(2026, 3, 31): Decimal("9")},
        },
        max_share_step=Decimal("0.3"),
        capital_cap_fraction=Decimal("0"),
    )

    assert sum(overrides.values(), Decimal("0")) == Decimal("10")
    assert sum(
        row.allocated_order_qty * row.inventory_cost_per_unit_rub for row in audit
    ) <= Decimal("1800")
    assert overrides[(date(2026, 4, 1), "SOFT_GX")] > Decimal("2")


def test_family_order_pool_does_not_move_between_quality_segments() -> None:
    members = _members()
    overrides, audit = allocate_display_family_order_pool(
        [_order_row("SOFT_JK", "5"), _order_row("INCELL", "5")],
        members=members,
        sales_by_code={"INCELL": {date(2026, 3, 31): Decimal("100")}},
    )

    assert overrides[(date(2026, 4, 1), "SOFT_JK")] == Decimal("5")
    assert overrides[(date(2026, 4, 1), "INCELL")] == Decimal("5")
    assert {row.blocker for row in audit} == {"segment_has_fewer_than_two_skus"}


def test_family_order_pool_ignores_current_and_future_sales() -> None:
    members = _members()
    rows = [_order_row("SOFT_JK", "5"), _order_row("SOFT_GX", "5")]
    control, _audit = allocate_display_family_order_pool(
        rows,
        members=members,
        sales_by_code={},
    )
    candidate, _audit = allocate_display_family_order_pool(
        rows,
        members=members,
        sales_by_code={
            "SOFT_GX": {
                date(2026, 4, 1): Decimal("100"),
                date(2026, 4, 2): Decimal("100"),
            }
        },
    )

    assert candidate == control


def test_family_order_pool_does_not_send_order_to_sku_covered_by_pipeline() -> None:
    members = _members()
    overrides, _audit = allocate_display_family_order_pool(
        [
            _order_row("SOFT_JK", "10", position="0"),
            _order_row("SOFT_GX", "0", position="200"),
        ],
        members=members,
        sales_by_code={
            "SOFT_JK": {date(2026, 3, 31): Decimal("100")},
            "SOFT_GX": {date(2026, 3, 31): Decimal("100")},
        },
        max_share_step=Decimal("1"),
    )

    assert overrides[(date(2026, 4, 1), "SOFT_GX")] == Decimal("0")
    assert overrides[(date(2026, 4, 1), "SOFT_JK")] == Decimal("10")


def test_family_order_pool_blocks_second_redistribution_while_lot_is_open() -> None:
    members = _members()
    first = [_order_row("SOFT_JK", "8"), _order_row("SOFT_GX", "2")]
    second = [
        {**row, "decision_date": "2026-04-08", "ordinary_recommended_order_qty": "5"}
        for row in first
    ]
    overrides, audit = allocate_display_family_order_pool(
        first + second,
        members=members,
        sales_by_code={"SOFT_GX": {date(2026, 3, 31): Decimal("20")}},
        max_share_step=Decimal("0.3"),
    )

    assert overrides[(date(2026, 4, 8), "SOFT_JK")] == Decimal("5")
    assert overrides[(date(2026, 4, 8), "SOFT_GX")] == Decimal("5")
    assert {row.blocker for row in audit if row.decision_date == date(2026, 4, 8)} == {
        "family_lot_still_open"
    }


def test_family_order_pool_can_correct_sales_rate_for_available_days() -> None:
    members = _members()
    rows = [_order_row("SOFT_JK", "5"), _order_row("SOFT_GX", "5")]
    sales = {
        "SOFT_JK": {date(2026, 3, 31): Decimal("5")},
        "SOFT_GX": {date(2026, 3, 31): Decimal("5")},
    }
    raw, _audit = allocate_display_family_order_pool(
        rows,
        members=members,
        sales_by_code=sales,
        max_share_step=Decimal("1"),
    )
    corrected, audit = allocate_display_family_order_pool(
        rows,
        members=members,
        sales_by_code=sales,
        available_dates_by_code={
            "SOFT_JK": {date(2026, 3, 31)},
            "SOFT_GX": {date(2026, 3, day) for day in range(2, 32)},
        },
        availability_corrected=True,
        max_share_step=Decimal("1"),
    )

    assert raw[(date(2026, 4, 1), "SOFT_JK")] == Decimal("5")
    assert corrected[(date(2026, 4, 1), "SOFT_JK")] > Decimal("5")
    assert {row.allocation_source for row in audit} == {"availability_corrected_sales_rate_30_90"}


def test_profit_safety_buffer_uses_hurdle_and_one_open_lot() -> None:
    first = {
        **_order_row("SOFT_JK", "5"),
        "forecast_rate_sales": "1",
        "lead_time_p75_days": "30",
        "gross_margin_per_unit_rub": "50",
        "acceleration_gross_projected_shortage_to_p75_qty": "3",
    }
    second = {**first, "decision_date": "2026-04-08"}
    overrides, audit = build_display_family_profit_protection(
        [first, second],
        base_overrides={
            (date(2026, 4, 1), "SOFT_JK"): Decimal("5"),
            (date(2026, 4, 8), "SOFT_JK"): Decimal("5"),
        },
        mode="safety",
        annual_carrying_rate=Decimal("0.65"),
        max_units_per_decision=2,
    )

    assert overrides[(date(2026, 4, 1), "SOFT_JK")] == Decimal("7")
    assert overrides[(date(2026, 4, 8), "SOFT_JK")] == Decimal("5")
    assert audit[1].blocker == "profit_protection_lot_still_open"


def test_pipeline_topup_requires_open_family_lot_block() -> None:
    row = {
        **_order_row("SOFT_JK", "5"),
        "forecast_rate_sales": "1",
        "lead_time_p75_days": "30",
        "gross_margin_per_unit_rub": "50",
        "acceleration_gross_projected_shortage_to_p75_qty": "3",
    }
    blocked, audit = build_display_family_profit_protection(
        [row],
        base_overrides={(date(2026, 4, 1), "SOFT_JK"): Decimal("5")},
        mode="pipeline_topup",
        annual_carrying_rate=Decimal("0.65"),
        max_units_per_decision=2,
        open_lot_keys=set(),
    )
    eligible, _audit = build_display_family_profit_protection(
        [row],
        base_overrides={(date(2026, 4, 1), "SOFT_JK"): Decimal("5")},
        mode="pipeline_topup",
        annual_carrying_rate=Decimal("0.65"),
        max_units_per_decision=2,
        open_lot_keys={(date(2026, 4, 1), "SOFT_JK")},
    )

    assert blocked[(date(2026, 4, 1), "SOFT_JK")] == Decimal("5")
    assert audit[0].blocker == "no_open_family_lot_block"
    assert eligible[(date(2026, 4, 1), "SOFT_JK")] == Decimal("7")


def test_profit_protection_can_require_unit_gmroi() -> None:
    row = {
        **_order_row("SOFT_JK", "5", cost="1000"),
        "forecast_rate_sales": "1",
        "lead_time_p75_days": "100",
        "gross_margin_per_unit_rub": "100",
        "acceleration_gross_projected_shortage_to_p75_qty": "3",
    }
    overrides, audit = build_display_family_profit_protection(
        [row],
        base_overrides={(date(2026, 4, 1), "SOFT_JK"): Decimal("5")},
        mode="safety",
        annual_carrying_rate=Decimal("0.1"),
        max_units_per_decision=2,
        gmroi_hurdle=Decimal("2"),
    )

    assert overrides[(date(2026, 4, 1), "SOFT_JK")] == Decimal("5")
    assert audit[0].blocker == "gmroi_hurdle_not_passed"


def test_regular_topup_covers_fraction_of_shortage_after_base_order() -> None:
    row = {
        **_order_row("SOFT_JK", "2", position="20"),
        "forecast_rate_sales": "1",
        "lead_time_p75_days": "30",
        "gross_margin_per_unit_rub": "50",
        "acceleration_gross_projected_shortage_to_p75_qty": "10",
    }

    overrides, audit = build_display_family_regular_topup(
        [row],
        base_overrides={(date(2026, 4, 1), "SOFT_JK"): Decimal("2")},
        focus_codes={"SOFT_JK"},
        annual_carrying_rate=Decimal("0.65"),
        shortage_coverage_fraction=Decimal("0.5"),
    )

    assert overrides[(date(2026, 4, 1), "SOFT_JK")] == Decimal("6")
    assert audit[0].shortage_after_base_order_qty == Decimal("8")
    assert audit[0].target_protection_qty == Decimal("4")
    assert audit[0].expected_arrival_date == date(2026, 5, 1)


def test_regular_topup_opens_only_incremental_ordinary_lots() -> None:
    first = {
        **_order_row("SOFT_JK", "2", position="20"),
        "forecast_rate_sales": "1",
        "lead_time_p75_days": "30",
        "gross_margin_per_unit_rub": "50",
        "acceleration_gross_projected_shortage_to_p75_qty": "10",
    }
    second = {
        **first,
        "decision_date": "2026-04-08",
        "acceleration_gross_projected_shortage_to_p75_qty": "14",
    }
    after_arrivals = {**second, "decision_date": "2026-05-09"}

    overrides, audit = build_display_family_regular_topup(
        [first, second, after_arrivals],
        base_overrides={
            (date(2026, 4, 1), "SOFT_JK"): Decimal("2"),
            (date(2026, 4, 8), "SOFT_JK"): Decimal("2"),
            (date(2026, 5, 9), "SOFT_JK"): Decimal("2"),
        },
        focus_codes={"SOFT_JK"},
        annual_carrying_rate=Decimal("0.65"),
        shortage_coverage_fraction=Decimal("0.5"),
    )

    assert overrides[(date(2026, 4, 1), "SOFT_JK")] == Decimal("6")
    assert overrides[(date(2026, 4, 8), "SOFT_JK")] == Decimal("4")
    assert audit[1].open_protection_qty == Decimal("4")
    assert audit[1].added_order_qty == Decimal("2")
    assert overrides[(date(2026, 5, 9), "SOFT_JK")] == Decimal("8")
    assert audit[2].open_protection_qty == Decimal("0")


def test_regular_topup_blocks_arrival_outside_evaluation_window() -> None:
    row = {
        **_order_row("SOFT_JK", "0", position="20"),
        "decision_date": "2026-06-01",
        "forecast_rate_sales": "1",
        "lead_time_p75_days": "30",
        "gross_margin_per_unit_rub": "50",
    }

    overrides, audit = build_display_family_regular_topup(
        [row],
        base_overrides={(date(2026, 6, 1), "SOFT_JK"): Decimal("0")},
        focus_codes={"SOFT_JK"},
        annual_carrying_rate=Decimal("0.65"),
        shortage_coverage_fraction=Decimal("0.5"),
        latest_evaluable_arrival_date=date(2026, 6, 30),
    )

    assert overrides[(date(2026, 6, 1), "SOFT_JK")] == Decimal("0")
    assert audit[0].expected_arrival_date == date(2026, 7, 1)
    assert audit[0].blocker == "arrival_outside_evaluation_window"


def test_regular_topup_respects_procurement_cadence() -> None:
    first = {
        **_order_row("SOFT_JK", "0", position="20"),
        "forecast_rate_sales": "1",
        "lead_time_p75_days": "30",
        "gross_margin_per_unit_rub": "50",
    }
    next_day = {**first, "decision_date": "2026-04-02", "inventory_position_qty": "10"}

    overrides, audit = build_display_family_regular_topup(
        [first, next_day],
        base_overrides={
            (date(2026, 4, 1), "SOFT_JK"): Decimal("0"),
            (date(2026, 4, 2), "SOFT_JK"): Decimal("0"),
        },
        focus_codes={"SOFT_JK"},
        annual_carrying_rate=Decimal("0.65"),
        shortage_coverage_fraction=Decimal("0.5"),
        minimum_days_between_topups=7,
    )

    assert overrides[(date(2026, 4, 1), "SOFT_JK")] == Decimal("5")
    assert overrides[(date(2026, 4, 2), "SOFT_JK")] == Decimal("0")
    assert audit[1].blocker == "regular_topup_cadence_block"


def test_regular_topup_freezes_baseline_and_exports_p75_delivery() -> None:
    rows = [
        {
            "decision_date": "2026-04-01",
            "nomenclature_code": "SKU-1",
            "ordinary_family_allocated_order_qty": "7",
        },
        {
            "decision_date": "2026-04-08",
            "nomenclature_code": "SKU-1",
            "ordinary_family_allocated_order_qty": "11",
        },
    ]
    frozen = freeze_display_family_order_trajectory(rows)
    assert frozen == {
        (date(2026, 4, 1), "SKU-1"): Decimal("7"),
        (date(2026, 4, 8), "SKU-1"): Decimal("11"),
    }

    _, audit = build_display_family_regular_topup(
        [
            {
                **rows[0],
                "lead_time_p75_days": "30",
                "forecast_rate_sales": "1",
                "inventory_position_qty": "20",
                "acceleration_gross_projected_shortage_to_p75_qty": "10",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "80",
            }
        ],
        base_overrides={(date(2026, 4, 1), "SKU-1"): Decimal("7")},
        focus_codes={"SKU-1"},
        annual_carrying_rate=Decimal("0.2"),
        shortage_coverage_fraction=Decimal("1"),
    )
    quantities, arrivals = build_regular_topup_delivery_overrides(audit)
    assert quantities == {(date(2026, 4, 1), "SKU-1"): Decimal("3")}
    assert arrivals == {(date(2026, 4, 1), "SKU-1"): date(2026, 5, 1)}


def test_frozen_family_order_trajectory_rejects_conflicting_duplicates() -> None:
    with pytest.raises(ValueError, match="conflicting quantities"):
        freeze_display_family_order_trajectory(
            [
                {
                    "decision_date": "2026-04-01",
                    "nomenclature_code": "SKU-1",
                    "ordinary_family_allocated_order_qty": "7",
                },
                {
                    "decision_date": "2026-04-01",
                    "nomenclature_code": "SKU-1",
                    "ordinary_family_allocated_order_qty": "8",
                },
            ]
        )
