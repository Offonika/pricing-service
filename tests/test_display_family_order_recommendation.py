from __future__ import annotations

from app.services.display_family_order_recommendation import (
    apply_display_family_order_recommendations,
    display_family_order_recommendation_summary,
    reset_display_family_order_recommendations,
)
from app.services.display_family_registry import ActiveDisplayFamilyMemberContext


def _context(
    code: str,
    *,
    family: str = "family-1",
    segment: str = "premium|soft_oled",
    quality: str = "premium",
    construction: str = "soft_oled",
) -> ActiveDisplayFamilyMemberContext:
    return ActiveDisplayFamilyMemberContext(
        registry_version_id=2,
        registry_version_number=2,
        registry_inventory_checksum="a" * 64,
        family_record_id=10,
        family_key=family,
        family_label="Apple iPhone Test",
        family_member_count=3,
        family_review_member_count=0,
        family_matching_review_member_count=0,
        family_warning_codes=(),
        product_id=int(code.removeprefix("SKU")),
        segment_id=segment,
        quality_segment=quality,
        construction_segment=construction,
        requires_manual_review=False,
        member_warning_codes=(),
        matching_evidence={},
    )


def _row(
    code: str,
    *,
    order: str,
    short_sales: str,
    medium_sales: str,
    price: str,
) -> dict[str, object]:
    return {
        "nomenclature_code": code,
        "recommended_order_qty": order,
        "dry_run_decision": "order" if order != "0" else "do_not_order",
        "blockers": "",
        "_auto_order_allowed": True,
        "sales_qty_window_short": short_sales,
        "sales_qty_window_medium": medium_sales,
        "sales_qty_window": medium_sales,
        "free_stock_qty": "0",
        "incoming_qty": "0",
        "latest_purchase_price": price,
    }


def test_family_order_pool_preserves_quantity_and_does_not_increase_capital() -> None:
    rows = [
        _row("SKU1", order="10", short_sales="0", medium_sales="0", price="100"),
        _row("SKU2", order="0", short_sales="30", medium_sales="90", price="90"),
        _row("SKU3", order="4", short_sales="30", medium_sales="90", price="50"),
    ]
    membership = {
        "SKU1": _context("SKU1"),
        "SKU2": _context("SKU2"),
        "SKU3": _context(
            "SKU3",
            segment="original|soft_oled",
            quality="original",
        ),
    }

    apply_display_family_order_recommendations(rows, membership_by_code=membership)

    by_code = {str(row["nomenclature_code"]): row for row in rows}
    assert by_code["SKU1"]["display_family_allocated_order_qty"] == "9"
    assert by_code["SKU2"]["display_family_allocated_order_qty"] == "1"
    assert by_code["SKU3"]["display_family_allocated_order_qty"] == "4"
    assert sum(int(str(row["display_family_allocated_order_qty"])) for row in rows) == 14
    assert 9 * 100 + 1 * 90 <= 10 * 100
    assert all(row["display_family_manual_approval_required"] == "yes" for row in rows)
    assert by_code["SKU1"]["recommended_order_qty"] == "10"
    assert by_code["SKU2"]["recommended_order_qty"] == "0"

    summary = display_family_order_recommendation_summary(rows)
    assert summary["baseline_order_qty"] == "14"
    assert summary["allocated_order_qty"] == "14"
    assert summary["reallocated_row_count"] == 2
    assert summary["manual_approval_required"] is True


def test_family_order_pool_never_moves_unconfirmed_segment() -> None:
    rows = [
        _row("SKU1", order="5", short_sales="0", medium_sales="0", price="100"),
        _row("SKU2", order="0", short_sales="30", medium_sales="90", price="90"),
    ]
    membership = {code: _context(code, quality="unknown") for code in ("SKU1", "SKU2")}

    apply_display_family_order_recommendations(rows, membership_by_code=membership)

    assert [row["display_family_allocated_order_qty"] for row in rows] == ["5", "0"]
    assert {row["display_family_recommendation_status"] for row in rows} == {
        "review_unconfirmed_segment"
    }


def test_family_order_pool_fails_closed_when_registry_is_unavailable() -> None:
    rows = [_row("SKU1", order="5", short_sales="5", medium_sales="5", price="100")]

    apply_display_family_order_recommendations(
        rows,
        membership_by_code={},
        registry_error="database unavailable",
    )

    assert rows[0]["display_family_recommendation_status"] == "blocked_registry_unavailable"
    assert rows[0]["display_family_allocated_order_qty"] == "5"
    assert rows[0]["display_family_confidence"] == "none"


def test_reset_family_order_pool_removes_stale_values() -> None:
    rows = [_row("SKU1", order="4", short_sales="5", medium_sales="5", price="100")]
    rows[0]["display_family_baseline_order_qty"] = "10"
    rows[0]["display_family_allocated_order_qty"] = "10"
    rows[0]["display_family_recommendation_status"] = "allocated_shadow"

    reset_display_family_order_recommendations(rows)

    assert rows[0]["display_family_baseline_order_qty"] == ""
    assert rows[0]["display_family_allocated_order_qty"] == ""
    assert rows[0]["display_family_recommendation_status"] == ""
