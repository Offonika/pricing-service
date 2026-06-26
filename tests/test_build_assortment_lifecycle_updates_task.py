from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _display_lifecycle_fact() -> dict[str, object]:
    return {
        "nomenclature_code": "РБ000074721",
        "name": "Дисплей тестовый",
        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
        "first_supplier_order_at": "2026-06-10",
        "supplier_order_cargo_handoff_dates": ["2026-06-20"],
        "expensive_item_value": "300",
        "expensive_group_values": ["100", "200", "300", "400"],
        "expensive_route_days": 7,
        "folder_responsible": "Омар",
        "warehouses": [
            {"warehouse_code": "shop-1", "sells_systematically": True},
            {"warehouse_code": "central", "is_central": True},
            {"warehouse_code": "defect", "is_defect_warehouse": True},
        ],
        "manager_need_signals": [
            {
                "manager_id": "manager-1",
                "quantity": 9,
                "source": "offline_call",
                "signal_date": "2026-06-24",
                "comment": "Спрашивали в магазине",
            }
        ],
    }


def test_build_assortment_lifecycle_updates_task_writes_export_rows(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "facts.json"
    output_path = tmp_path / "property-updates.json"
    input_path.write_text(
        json.dumps({"items": [_display_lifecycle_fact()]}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_updates",
            "--input-json",
            str(input_path),
            "--folder",
            "дисплеи",
            "--changed-at",
            "2026-06-25",
            "--suspicious-quantity-threshold",
            "5",
            "--output-json",
            str(output_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    item = summary["items"][0]
    assert summary["rows"] == 5
    assert item["status"] == "new_item"
    assert item["expensive_profile"] == "fast_expensive"
    assert item["sales_point_warehouse_codes"] == ["shop-1"]
    assert item["manager_need_signals"][0]["accepted"] is True
    assert item["manager_need_signals"][0]["suspicious"] is True

    rows = json.loads(output_path.read_text(encoding="utf-8"))["items"]
    assert [row["property_name"] for row in rows] == [
        "Статус ассортимента",
        "Причина статуса ассортимента",
        "Дата изменения статуса ассортимента",
        "Источник статуса ассортимента",
        "Профиль закупочного поведения",
    ]
    assert rows[0]["new_value_tag"] == "new_item"
    assert rows[-1]["new_value_tag"] == "fast_expensive"


def test_build_assortment_lifecycle_updates_task_prints_dry_run_xml(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "facts.json"
    input_path.write_text(
        json.dumps([_display_lifecycle_fact()], ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_updates",
            "--input-json",
            str(input_path),
            "--message-id",
            "assortment-lifecycle-test-001",
            "--changed-at",
            "2026-06-25",
            "--print-xml",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "<Schema>nomenclature_property_updates.v1</Schema>" in result.stdout
    assert "<MessageId>assortment-lifecycle-test-001</MessageId>" in result.stdout
    assert "<NewValueTag>new_item</NewValueTag>" in result.stdout
    assert "<NewValueTag>fast_expensive</NewValueTag>" in result.stdout


def test_build_assortment_lifecycle_updates_blocks_incomplete_exclusive(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "facts.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "nomenclature_code": "РБ000000777",
                    "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                    "manual_status": "exclusive",
                    "manual_reason": "Редкий товар для витрины",
                    "manual_approved_by": "Омар",
                    "manual_changed_at": "2026-06-25",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_updates",
            "--input-json",
            str(input_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    assert summary["rows"] == 0
    assert summary["items"][0]["status"] == "exclusive"
    assert summary["items"][0]["export_blockers"] == ["exclusive_min_stock_required"]
