from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from tasks.build_assortment_lifecycle_facts import _default_history_months, _default_limit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_assortment_lifecycle_facts_task_reads_history_months_from_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS", "48")

    assert _default_history_months() == 48


def test_build_assortment_lifecycle_facts_task_uses_safe_default_limit(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ASSORTMENT_LIFECYCLE_LIMIT", raising=False)
    assert _default_limit() == 3000

    monkeypatch.setenv("ASSORTMENT_LIFECYCLE_LIMIT", "1200")
    assert _default_limit() == 1200


def test_build_assortment_lifecycle_facts_task_feeds_updates_task(tmp_path: Path) -> None:
    raw_path = tmp_path / "source-rows.json"
    warehouse_path = tmp_path / "warehouse-policy.json"
    signals_path = tmp_path / "manager-signals.json"
    facts_path = tmp_path / "assortment-lifecycle-facts.json"
    updates_path = tmp_path / "nomenclature-property-updates.json"

    raw_path.write_text(
        json.dumps(
            {
                "nomenclature_rows": [
                    {
                        "nomenclature_ref": "0xA",
                        "nomenclature_code": "РБ0001",
                        "name": "Дисплей тестовый A",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "item_value": "300",
                    },
                    {
                        "nomenclature_ref": "0xB",
                        "nomenclature_code": "РБ0002",
                        "name": "Дисплей тестовый B",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "item_value": "100",
                    },
                ],
                "supplier_order_rows": [
                    {
                        "nomenclature_ref": "0xA",
                        "order_date": "2026-01-01",
                        "cargo_handoff_date": "2026-01-05",
                        "line_price": "300",
                    },
                    {
                        "nomenclature_ref": "0xA",
                        "order_date": "2026-02-01",
                        "cargo_handoff_date": "2026-02-05",
                        "line_price": "320",
                    },
                ],
                "receipt_rows": [
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-01-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-02-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-03-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-04-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-05-10"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    warehouse_path.write_text(
        json.dumps(
            {
                "warehouses": [
                    {"warehouse_code": "shop-1", "sells_systematically": True},
                    {"warehouse_code": "central", "is_central": True},
                    {"warehouse_code": "defect", "is_defect_warehouse": True},
                    {"warehouse_code": "transit", "is_transit": True},
                    {"warehouse_code": "rare", "is_non_systematic_sale": True},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    signals_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ0001",
                        "manager_id": "manager-1",
                        "quantity": 1,
                        "source": "offline_call",
                        "signal_date": "2026-01-03",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    facts_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_facts",
            "--input-json",
            str(raw_path),
            "--warehouse-policy-json",
            str(warehouse_path),
            "--manager-signals-json",
            str(signals_path),
            "--today",
            "2026-06-25",
            "--output-json",
            str(facts_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        text=True,
    )

    facts_summary = json.loads(facts_result.stdout)
    assert facts_summary["status"] == "ready"
    assert facts_summary["items"] == 2
    assert facts_path.exists()
    facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
    assert facts_payload["meta"]["schema"] == "assortment_lifecycle_facts.v1"
    assert facts_payload["items"][0]["receipt_dates"][-1] == "2026-05-10"

    updates_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_updates",
            "--input-json",
            str(facts_path),
            "--folder",
            "дисплеи",
            "--changed-at",
            "2026-06-25",
            "--output-json",
            str(updates_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        text=True,
    )

    updates_summary = json.loads(updates_result.stdout)
    item = updates_summary["items"][0]
    # 2026-08-02: 5 поступлений за 180 дней дают Рабочий сразу, без
    # подтверждения ответственного (решение 2026-07-20 доведено до кода).
    assert item["status"] == "working"
    assert item["recommended_status"] is None
    assert item["blockers"] == []
    assert item["sales_point_warehouse_codes"] == ["shop-1"]
    assert item["expensive_profile"] == "fast_expensive"
    assert updates_summary["rows"] > 0


def test_build_assortment_lifecycle_facts_task_blocks_without_receipt_mapping(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "onec.db"
    warehouse_path = tmp_path / "warehouse-policy.json"
    supplier_mapping_path = tmp_path / "supplier-mapping.json"
    receipt_mapping_path = tmp_path / "receipt-mapping.json"

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE supplier_doc "
                "(_IDRRef TEXT, _Date_Time TEXT, _Posted TEXT, _Marked TEXT)"
            )
        )
        conn.execute(
            text("CREATE TABLE supplier_lines " "(_DocumentRRef TEXT, _NomenclatureRRef TEXT)")
        )

    warehouse_path.write_text(
        json.dumps({"warehouses": [{"warehouse_code": "shop-1"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    supplier_mapping_path.write_text(
        json.dumps(
            {
                "document_table": "supplier_doc",
                "line_table": "supplier_lines",
                "line_document_column": "_DocumentRRef",
                "line_nomenclature_column": "_NomenclatureRRef",
            }
        ),
        encoding="utf-8",
    )
    receipt_mapping_path.write_text(
        json.dumps(
            {
                "document_table": "receipt_doc",
                "line_table": "receipt_lines",
                "line_document_column": "_DocumentRRef",
                "line_nomenclature_column": "_NomenclatureRRef",
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_facts",
            "--onec-database-url",
            f"sqlite:///{db_path}",
            "--warehouse-policy-json",
            str(warehouse_path),
            "--supplier-order-mapping-json",
            str(supplier_mapping_path),
            "--receipt-mapping-json",
            str(receipt_mapping_path),
            "--json",
        ],
        capture_output=True,
        cwd=PROJECT_ROOT,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["error"].startswith("receipt_mapping_unresolved:")
    assert "table_missing:receipt_doc" in payload["error"]
