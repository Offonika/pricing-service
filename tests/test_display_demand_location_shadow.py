from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.services.display_demand_location_shadow import (
    build_display_demand_location_shadow,
    build_display_stock_location_shadow,
)


def test_demand_shadow_preserves_point_channel_and_causal_return_key() -> None:
    sales = [
        {
            "product_code": "SKU-1",
            "occurred_at": datetime(2026, 8, 15, 10),
            "document_ref": "SALE-1",
            "warehouse_code": "SHOP-1",
            "warehouse_name": "Магазин 1",
            "demand_channel": "offline",
            "raw_line_count": 2,
            "quantity": "5",
        },
        {
            "product_code": "SKU-1",
            "occurred_at": datetime(2026, 8, 16, 10),
            "document_ref": "SALE-2",
            "warehouse_code": "SITE",
            "warehouse_name": "Сайт",
            "demand_channel": "online",
            "raw_line_count": 1,
            "quantity": "2",
        },
        {
            "product_code": "SKU-1",
            "occurred_at": datetime(2026, 6, 1, 10),
            "document_ref": "SALE-OLD",
            "warehouse_code": "SHOP-1",
            "warehouse_name": "Магазин 1",
            "demand_channel": "offline",
            "raw_line_count": 1,
            "quantity": "4",
        },
    ]
    returns = [
        {
            "product_code": "SKU-1",
            "occurred_at": datetime(2026, 8, 16, 12),
            "return_ref": "RETURN-1",
            "source_sale_ref": "SALE-1",
            "warehouse_code": "SHOP-1",
            "warehouse_name": "Магазин 1",
            "demand_channel": "offline",
            "raw_line_count": 1,
            "quantity": "1",
        },
        {
            "product_code": "SKU-1",
            "occurred_at": datetime(2026, 8, 16, 12),
            "return_ref": "RETURN-1",
            "source_sale_ref": "SALE-X",
            "warehouse_code": "SHOP-1",
            "warehouse_name": "Магазин 1",
            "demand_channel": "offline",
            "raw_line_count": 1,
            "quantity": "1",
        },
    ]

    result = build_display_demand_location_shadow(
        sales,
        returns,
        date_to=date(2026, 8, 17),
    )

    totals_30 = result["totals_by_window"]["30"]
    totals_90 = result["totals_by_window"]["90"]
    assert totals_30 == {
        "gross_sale_qty": Decimal("7"),
        "return_qty": Decimal("2"),
        "net_fulfilled_sale_qty": Decimal("5"),
    }
    assert totals_90["gross_sale_qty"] == Decimal("11")
    assert totals_90["net_fulfilled_sale_qty"] == Decimal("9")
    assert result["quality"]["return_legacy_key_collision_count"] == 1
    assert result["quality"]["return_duplicate_causal_key_count"] == 0
    assert result["quality"]["return_missing_source_sale_ref_row_count"] == 0
    online = next(row for row in result["facts_by_point"] if row["demand_channel"] == "online")
    assert online["attribution_method"] == "site_order_link"
    assert online["gross_sale_qty_30d"] == Decimal("2")


def test_stock_shadow_clamps_each_point_before_network_sum() -> None:
    result = build_display_stock_location_shadow(
        [
            {
                "product_code": "SKU-1",
                "warehouse_code": "SHOP-1",
                "warehouse_name": "Магазин 1",
                "stock_qty": "10",
            },
            {
                "product_code": "SKU-1",
                "warehouse_code": "SHOP-2",
                "warehouse_name": "Магазин 2",
                "stock_qty": "0",
            },
        ],
        [
            {
                "product_code": "SKU-1",
                "warehouse_code": "SHOP-2",
                "warehouse_name": "Магазин 2",
                "reserved_qty": "5",
            }
        ],
    )

    assert result["network"]["naive_net_qty"] == Decimal("5")
    assert result["network"]["point_safe_free_qty"] == Decimal("10")
    assert result["network"]["uncovered_qty"] == Decimal("5")
    assert result["quality"]["negative_point_count"] == 1
