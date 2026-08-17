from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_task_builds_frozen_read_only_comparison(tmp_path: Path) -> None:
    policy_path = tmp_path / "warehouse-policy.json"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "shadow.json"
    warehouses = [
        {
            "warehouse_code": f"SHOP-{index}",
            "role": "physical_sales_point",
            "sells_systematically": True,
        }
        for index in range(11)
    ] + [
        {
            "warehouse_code": "РБ0000045",
            "role": "non_systematic",
            "sells_systematically": False,
            "is_non_systematic_sale": True,
        },
        {
            "warehouse_code": "РБ0000010",
            "role": "central_transfer_stock",
            "sells_systematically": False,
            "is_transit": True,
        },
    ]
    policy_path.write_text(
        json.dumps(
            {
                "policy_version": "test-2026-08-17",
                "minimum_representation_policy": {
                    "central_warehouse_code": "РБ0000010",
                    "central_reserve_qty": 2,
                },
                "usable_stock_quality_names": ["Новый"],
                "warehouses": warehouses,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    input_path.write_text(
        json.dumps(
            {
                "registry": {"version_number": 2, "status": "active"},
                "display_codes": ["SKU-1"],
                "sale_rows": [
                    {
                        "product_code": "SKU-1",
                        "occurred_at": "2026-08-15T10:00:00",
                        "document_ref": "SALE-1",
                        "warehouse_code": "SHOP-0",
                        "warehouse_name": "Магазин 0",
                        "demand_channel": "offline",
                        "quantity": "5",
                    }
                ],
                "return_rows": [
                    {
                        "product_code": "SKU-1",
                        "occurred_at": "2026-08-16T10:00:00",
                        "return_ref": "RETURN-1",
                        "source_sale_ref": "SALE-1",
                        "warehouse_code": "SHOP-0",
                        "warehouse_name": "Магазин 0",
                        "demand_channel": "offline",
                        "quantity": "2",
                    }
                ],
                "stock_rows": [
                    {
                        "product_code": "SKU-1",
                        "warehouse_code": "SHOP-0",
                        "warehouse_name": "Магазин 0",
                        "stock_qty": "4",
                    }
                ],
                "reserve_rows": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_display_demand_location_shadow",
            "--as-of",
            "2026-08-17",
            "--warehouse-policy-json",
            str(policy_path),
            "--input-json",
            str(input_path),
            "--output-json",
            str(output_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    stdout = json.loads(result.stdout)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout["status"] == "ready"
    assert payload["policy"]["active_physical_store_count"] == 11
    assert payload["policy"]["minimum_representation_qty"] == 13
    assert payload["comparison"]["current_gross_demand_qty"] == "5"
    assert payload["comparison"]["candidate_net_fulfilled_demand_qty"] == "3"
    assert payload["comparison"]["order_quantity_change_authorized"] is False
    assert payload["safety"] == {
        "read_only": True,
        "writes_to_onec": False,
        "writes_to_application_db": False,
        "creates_orders": False,
        "changes_statuses": False,
        "changes_production_formula": False,
    }
