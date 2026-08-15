from decimal import Decimal

import pytest

from app.services.display_family_demand import (
    allocate_display_family_rates,
    build_display_family_members,
    display_construction_segment,
    display_quality_segment,
)


def _members():
    return build_display_family_members(
        [
            {
                "nomenclature_code": "SOFT_JK",
                "name": "Дисплей для Apple iPhone 14 Pro Max (JK) (Soft Oled) (площадка под IC)",
                "model_tokens": ("apple:model:iphone 14 pro max",),
            },
            {
                "nomenclature_code": "SOFT_GX",
                "name": "Дисплей для Apple iPhone 14 Pro Max (GX ORIG) (Soft Oled) (площадка под IC)",
                "model_tokens": ("apple:model:iphone 14 pro max",),
            },
            {
                "nomenclature_code": "INCELL",
                "name": "Дисплей для Apple iPhone 14 Pro Max (JK) (In-Cell)",
                "model_tokens": ("apple:model:iphone 14 pro max",),
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
