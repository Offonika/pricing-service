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
    # Решение 2026-08-18: стадия лестницы в 1С не уходит, остаётся только
    # профиль закупочного поведения.
    assert summary["rows"] == 1
    assert item["status"] == "new_item"
    assert item["export_blockers"] == ["lifecycle_stage_not_exported"]
    assert item["expensive_profile"] == "fast_expensive"
    assert item["sales_point_warehouse_codes"] == ["shop-1"]
    assert item["manager_need_signals"][0]["accepted"] is True
    assert item["manager_need_signals"][0]["suspicious"] is True

    rows = json.loads(output_path.read_text(encoding="utf-8"))["items"]
    assert [row["property_name"] for row in rows] == ["Профиль закупочного поведения"]
    assert rows[-1]["new_value_tag"] == "fast_expensive"


def test_build_assortment_lifecycle_updates_ignores_legacy_fact_status_decision(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "facts.json"
    output_path = tmp_path / "property-updates.json"
    input_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ000016562",
                        "name": "Дисплей старый",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "fact_status_decision": {
                            "target_status": "sales_start",
                            "fact_lifecycle_relation": "cargo_handoff_confirmed",
                            "reason": "Есть cargo/передачи: 56.",
                            "decided_at": "2026-07-09",
                            "approved_by": "chat_2026-07-09",
                        },
                    }
                ]
            },
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
            "--output-json",
            str(output_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    assert summary["rows"] == 0
    item = summary["items"][0]
    assert item["status"] == "fruit"
    assert item["status_label"] == "Рассматриваем"
    assert item["reason_codes"] == ["product_created"]
    assert item["export_blockers"] == ["lifecycle_stage_not_exported"]
    assert json.loads(output_path.read_text(encoding="utf-8"))["items"] == []


def test_build_assortment_lifecycle_updates_display_folder_includes_laptop_matrices(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "facts.json"
    input_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ000042811",
                        "name": "Матрица для Lenovo ThinkPad T480 14.0 Slim 30 pin",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / Запчасти для ноутбуков / Матрицы",
                    }
                ]
            },
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
            "--folder",
            "дисплеи",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    assert summary["items"][0]["nomenclature_code"] == "РБ000042811"
    assert summary["items"][0]["status"] == "fruit"
    # Стадия «Рассматриваем» в 1С не уходит, профиль закупки у карточки не задан.
    assert summary["rows"] == 0


def test_build_assortment_lifecycle_updates_excludes_bitok_before_statuses(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "facts.json"
    input_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "DROP",
                        "name": "Дисплей (биток)",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                    }
                ]
            },
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
    assert summary["items"] == []
    assert summary["rows"] == 0


def test_build_assortment_lifecycle_updates_task_rejects_ut103_xml(
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
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Lifecycle property export to UT 10.3 is retired" in result.stderr
    assert result.stdout == ""


def test_build_assortment_lifecycle_updates_keeps_status_and_blocks_incomplete_exclusive_mark(
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
    assert summary["items"][0]["status"] == "fruit"
    assert summary["items"][0]["commercial_marks"] == ["exclusive"]
    assert "lifecycle_stage_not_exported" in summary["items"][0]["export_blockers"]
    assert set(summary["items"][0]["export_blockers"]) >= {
        "exclusive_kind_required",
        "exclusive_evidence_required",
        "exclusive_min_stock_required",
    }


def test_build_assortment_lifecycle_updates_exports_complete_exclusive_mark(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "facts.json"
    output_path = tmp_path / "property-updates.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "nomenclature_code": "РБ000000777",
                    "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                    "supplier_order_cargo_handoff_dates": ["2026-06-20"],
                    "commercial_marks": ["exclusive"],
                    "exclusive_kind": "only_in_country",
                    "exclusive_checked_at": "2026-06-25",
                    "exclusive_reason": "Товар публично не найден у конкурентов",
                    "exclusive_approved_by": "Омар",
                    "exclusive_evidence_refs": ["parser:2026-06-25"],
                    "exclusive_min_stock_qty": "2",
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
            "--changed-at",
            "2026-06-25",
            "--output-json",
            str(output_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    assert summary["items"][0]["status"] == "new_item"
    assert summary["items"][0]["commercial_marks"] == ["exclusive"]
    assert summary["items"][0]["export_blockers"] == ["lifecycle_stage_not_exported"]

    rows = json.loads(output_path.read_text(encoding="utf-8"))["items"]
    property_names = [row["property_name"] for row in rows]
    assert "Статус ассортимента" not in property_names
    assert "Коммерческие признаки" in property_names
    assert "Тип эксклюзивности" in property_names
    assert "Ручной минимальный остаток" in property_names
