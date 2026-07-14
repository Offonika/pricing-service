from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import create_engine, select

from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
    ASSORTMENT_LIFECYCLE_METADATA,
    ASSORTMENT_LIFECYCLE_RUN_TABLE,
)
from tasks.refresh_assortment_lifecycle_classification import (
    _default_history_months,
    _default_limit,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_refresh_assortment_lifecycle_classification_task_reads_history_months_from_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS", "48")

    assert _default_history_months() == 48


def test_refresh_assortment_lifecycle_classification_task_uses_safe_default_limit(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ASSORTMENT_LIFECYCLE_LIMIT", raising=False)
    assert _default_limit() == 3000

    monkeypatch.setenv("ASSORTMENT_LIFECYCLE_LIMIT", "1200")
    assert _default_limit() == 1200


def test_refresh_assortment_lifecycle_classification_task_upserts_current_rows(
    tmp_path: Path,
) -> None:
    facts_path = tmp_path / "facts.json"
    db_path = tmp_path / "classification.db"
    database_url = f"sqlite:///{db_path}"

    facts_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ0001",
                        "name": "Дисплей тестовый A",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "feature_snapshot_schema": "procurement_feature_snapshot.v1",
                        "product_ref": "0xA",
                        "article": "SKU-001",
                        "subject_1c": "Дисплей",
                        "item_tags": ["iPhone", "рамка"],
                        "brand_compatibility": "Apple",
                        "model_compatibility": "iPhone 13",
                        "quality_raw": "ORIG100",
                        "quality_normalized": "original",
                        "characteristic_values": {"frame": "yes", "ic_pad": "yes"},
                        "price_segment": "mid_high",
                        "data_quality_score": "1.00",
                        "missing_required_attributes": [],
                        "future_ka_mapping_status": "ready",
                        "calculation_unit_level": "subject_tag",
                        "calculation_unit_key": "дисплей|iphone|рамка",
                        "calculation_unit_source": "1c_properties",
                        "calculation_unit_confidence": "1.00",
                        "calculation_unit_reason": "Есть надежные предмет и tag.",
                        "demand_method_code": "available_days_average",
                        "demand_method_reason": "Есть повторные поступления.",
                        "demand_method_confidence": "1.00",
                        "first_supplier_order_at": "2026-01-01",
                        "supplier_order_cargo_handoff_dates": ["2026-01-05", "2026-02-05"],
                        "receipt_dates": [
                            "2026-01-10",
                            "2026-02-10",
                            "2026-03-10",
                            "2026-04-10",
                            "2026-05-10",
                        ],
                        "expensive_item_value": "300",
                        "expensive_group_values": ["100", "200", "300", "400"],
                        "expensive_route_days": 5,
                        "warehouses": [{"warehouse_code": "shop-1", "sells_systematically": True}],
                    },
                    {
                        "nomenclature_code": "РБ0002",
                        "name": "Дисплей тестовый B",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "commercial_marks": ["exclusive"],
                        "exclusive_kind": "only_in_country",
                        "exclusive_checked_at": "2026-06-27",
                        "exclusive_reason": "Единственный товар на рынке",
                        "exclusive_approved_by": "Омар",
                        "exclusive_evidence_refs": ["parser:2026-06-27"],
                        "exclusive_min_stock_qty": "2",
                        "warehouses": [{"warehouse_code": "shop-1", "sells_systematically": True}],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    engine = create_engine(database_url)
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    engine.dispose()

    first = _run_refresh(
        facts_path=facts_path,
        database_url=database_url,
        run_key="classification-test-1",
        classified_at="2026-06-27T10:00:00",
    )
    second = _run_refresh(
        facts_path=facts_path,
        database_url=database_url,
        run_key="classification-test-2",
        classified_at="2026-06-27T11:00:00",
    )

    assert first["written_items"] == 2
    assert first["summary"]["statuses"] == {"fruit": 1, "sale": 1}
    assert first["summary"]["commercial_marks"] == {"exclusive": 1}
    assert first["summary"]["feature_snapshot_ready"] == 1
    assert second["written_items"] == 2

    engine = create_engine(database_url)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE).order_by(
                    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code
                )
            )
            .mappings()
            .all()
        )
        runs = conn.execute(select(ASSORTMENT_LIFECYCLE_RUN_TABLE)).mappings().all()
    engine.dispose()

    assert len(rows) == 2
    assert len(runs) == 2
    assert rows[0]["status"] == "sale"
    assert rows[0]["recommended_status"] == "working"
    assert rows[0]["manual_review_required"] is True
    assert rows[0]["feature_snapshot_schema"] == "procurement_feature_snapshot.v1"
    assert rows[0]["subject_1c"] == "Дисплей"
    assert rows[0]["item_tags"] == ["iPhone", "рамка"]
    assert rows[0]["quality_normalized"] == "original"
    assert rows[0]["characteristic_values"] == {"frame": "yes", "ic_pad": "yes"}
    assert rows[0]["future_ka_mapping_status"] == "ready"
    assert rows[0]["calculation_unit_level"] == "subject_tag"
    assert rows[0]["demand_method_code"] == "available_days_average"
    assert rows[1]["status"] == "fruit"
    assert rows[1]["commercial_marks"] == ["exclusive"]
    assert rows[1]["exclusive_kind"] == "only_in_country"
    assert rows[1]["exclusive_min_stock_qty"] == "2"
    assert rows[1]["auto_order_allowed"] is False
    assert rows[1]["classified_at"].isoformat() == "2026-06-27T11:00:00"


def test_refresh_assortment_lifecycle_classification_task_dry_run_skips_db_write(
    tmp_path: Path,
) -> None:
    facts_path = tmp_path / "facts.json"
    db_path = tmp_path / "classification.db"
    database_url = f"sqlite:///{db_path}"
    facts_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ0001",
                        "name": "Дисплей тестовый A",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "warehouses": [{"warehouse_code": "shop-1", "sells_systematically": True}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    engine = create_engine(database_url)
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    engine.dispose()

    result = _run_refresh(
        facts_path=facts_path,
        database_url=database_url,
        run_key="classification-dry-run",
        classified_at="2026-06-27T10:00:00",
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["written_items"] == 0
    assert result["summary"]["statuses"] == {"fruit": 1}

    engine = create_engine(database_url)
    with engine.connect() as conn:
        count = len(conn.execute(select(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE)).all())
    engine.dispose()

    assert count == 0


def test_refresh_assortment_lifecycle_classification_applies_fact_status_decisions(
    tmp_path: Path,
) -> None:
    facts_path = tmp_path / "facts.json"
    decisions_path = tmp_path / "fact-status-decisions.json"
    db_path = tmp_path / "classification.db"
    database_url = f"sqlite:///{db_path}"
    facts_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ0001",
                        "name": "Дисплей тестовый A",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "warehouses": [{"warehouse_code": "shop-1", "sells_systematically": True}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    decisions_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ0001",
                        "target_status": "sales_start",
                        "fact_lifecycle_relation": "cargo_handoff_confirmed",
                        "reason": "Есть cargo/передачи.",
                        "decided_at": "2026-07-09",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    engine = create_engine(database_url)
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    engine.dispose()

    result = _run_refresh(
        facts_path=facts_path,
        database_url=database_url,
        run_key="classification-fact-status-decision",
        classified_at="2026-07-09T10:00:00",
        extra_args=["--fact-status-decisions-json", str(decisions_path)],
    )

    assert result["summary"]["statuses"] == {"sales_start": 1}
    assert result["property_update_rows"] == 0

    engine = create_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(select(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE)).mappings().one()
    engine.dispose()

    assert row["status"] == "sales_start"
    assert row["status_label"] == "СП / Старт продаж"
    assert row["export_blockers"] == [
        "ut103_export_blocked",
        "fact_status_decision_requires_1c_approval",
    ]


def test_refresh_assortment_lifecycle_classification_task_writes_ut103_export(
    tmp_path: Path,
) -> None:
    facts_path = tmp_path / "facts.json"
    db_path = tmp_path / "classification.db"
    exchange_root = tmp_path / "exchange"
    database_url = f"sqlite:///{db_path}"
    facts_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ0001",
                        "name": "Дисплей тестовый A",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "warehouses": [{"warehouse_code": "shop-1", "sells_systematically": True}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    engine = create_engine(database_url)
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    engine.dispose()

    result = _run_refresh(
        facts_path=facts_path,
        database_url=database_url,
        run_key="classification-export-test",
        classified_at="2026-06-27T10:00:00",
        extra_args=[
            "--write-ready",
            "--allow-empty",
            "--export-mode",
            "apply",
            "--approved-by",
            "pricing-service-nightly",
            "--message-id",
            "assortment-lifecycle-export-test-001",
            "--exchange-root",
            str(exchange_root),
        ],
    )

    assert result["property_update_message_id"] == "assortment-lifecycle-export-test-001"
    assert result["property_update_mode"] == "apply"
    assert result["property_update_rows"] == 4

    output_path = Path(str(result["property_update_path"]))
    assert output_path == (
        exchange_root
        / "to_1c"
        / "new"
        / "nomenclature_properties_assortment-lifecycle-export-test-001.ready.xml"
    )
    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Header/Schema") == "nomenclature_property_updates.v1"
    assert root.findtext("Header/Mode") == "apply"
    assert root.findtext("Header/ApprovedBy") == "pricing-service-nightly"
    assert root.findtext("Items/Item/NomenclatureCode") == "РБ0001"
    assert root.findtext("Items/Item/PropertyName") == "Статус ассортимента"
    assert root.findtext("Items/Item/ValueType") == "property_value"
    assert root.findtext("Items/Item/NewValueName") == "Плод"
    assert root.findtext("Items/Item/NewValueTag") == "fruit"


def _run_refresh(
    *,
    facts_path: Path,
    database_url: str,
    run_key: str,
    classified_at: str,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
) -> dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "tasks.refresh_assortment_lifecycle_classification",
        "--facts-json",
        str(facts_path),
        "--database-url",
        database_url,
        "--run-key",
        run_key,
        "--classified-at",
        classified_at,
        "--json",
    ]
    if dry_run:
        command.append("--dry-run")
    if extra_args:
        command.extend(extra_args)

    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        text=True,
    )
    return json.loads(result.stdout)
