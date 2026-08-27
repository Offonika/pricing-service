from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
    ASSORTMENT_LIFECYCLE_METADATA,
)
from tasks import (
    report_procurement_feature_snapshot_quality as procurement_feature_quality_task,
)
from tasks.report_procurement_feature_snapshot_quality import (
    build_summary,
    load_feature_snapshot_rows,
    write_csv,
)


def test_report_procurement_feature_snapshot_quality_filters_and_writes_csv(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE),
            [
                _classification_row(
                    "РБ0001",
                    future_ka_mapping_status="ready",
                    missing_required_attributes=[],
                    data_quality_score="1.00",
                    calculation_unit_level="subject_tag",
                    demand_method_code="available_days_average",
                    auto_order_allowed=True,
                ),
                _classification_row(
                    "РБ0002",
                    future_ka_mapping_status="needs_mapping",
                    missing_required_attributes=["quality_raw", "model_compatibility"],
                    data_quality_score="0.33",
                    calculation_unit_level="sku",
                    demand_method_code="manual_review",
                    manual_review_required=True,
                ),
            ],
        )

    with Session(engine) as db:
        rows = load_feature_snapshot_rows(db, folder="дисплеи", only_missing=False)
        missing_rows = load_feature_snapshot_rows(db, folder="дисплеи", only_missing=True)
    summary = build_summary(rows)
    csv_path = write_csv(tmp_path / "feature-quality.csv", rows)

    assert len(rows) == 2
    assert [row["nomenclature_code"] for row in missing_rows] == ["РБ0002"]
    assert summary["future_ka_mapping_status"] == {"needs_mapping": 1, "ready": 1}
    assert summary["missing_required_attributes"] == {
        "model_compatibility": 1,
        "quality_raw": 1,
    }
    assert summary["calculation_unit_level"] == {"sku": 1, "subject_tag": 1}
    assert summary["demand_method_code"] == {
        "available_days_average": 1,
        "manual_review": 1,
    }
    assert summary["auto_order_allowed"] == 1
    assert summary["manual_review_required"] == 1

    csv_rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
    assert csv_rows[0]["nomenclature_code"] == "РБ0002"
    assert csv_rows[0]["missing_required_attributes"] == ('["quality_raw", "model_compatibility"]')
    assert csv_rows[1]["nomenclature_code"] == "РБ0001"


def test_report_procurement_feature_snapshot_quality_cli_uses_read_only_scope(
    db_session, tmp_path, monkeypatch, capsys
) -> None:
    calls: list[tuple[bool, str | None]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False, database_url: str | None = None):
        calls.append((read_only, database_url))
        yield db_session

    output_csv = tmp_path / "feature-quality.csv"
    monkeypatch.setattr(procurement_feature_quality_task, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        procurement_feature_quality_task,
        "load_feature_snapshot_rows",
        lambda db, **kwargs: [],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_procurement_feature_snapshot_quality",
            "--database-url",
            "sqlite:///override.db",
            "--output-csv",
            str(output_csv),
            "--json",
        ],
    )

    assert procurement_feature_quality_task.main() == 0
    assert calls == [(True, "sqlite:///override.db")]
    assert '"items": 0' in capsys.readouterr().out
    assert output_csv.is_file()


def _classification_row(
    code: str,
    *,
    future_ka_mapping_status: str,
    missing_required_attributes: list[str],
    data_quality_score: str,
    calculation_unit_level: str,
    demand_method_code: str,
    auto_order_allowed: bool = False,
    manual_review_required: bool = False,
) -> dict[str, object]:
    return {
        "nomenclature_code": code,
        "name": f"Дисплей тестовый {code}",
        "folder": "ОБЩИЙ КАТАЛОГ / дисплеи",
        "status": "sale",
        "status_label": "ПРОДАЖА",
        "recommended_status": None,
        "reason_codes": [],
        "reason_text": "",
        "blockers": [],
        "export_blockers": [],
        "auto_order_allowed": auto_order_allowed,
        "manual_review_required": manual_review_required,
        "expensive_profile": None,
        "expensive_profile_label": "",
        "expensive_reason_codes": [],
        "commercial_marks": [],
        "commercial_mark_labels": [],
        "commercial_mark_blockers": [],
        "exclusive_kind": "",
        "exclusive_confidence": "",
        "exclusive_checked_at": None,
        "exclusive_review_at": None,
        "exclusive_reason": "",
        "exclusive_approved_by": "",
        "exclusive_evidence_refs": [],
        "exclusive_min_stock_qty": None,
        "feature_snapshot_schema": "procurement_feature_snapshot.v1",
        "product_ref": f"ref-{code}",
        "article": f"SKU-{code}",
        "kind_1c": "",
        "subject_1c": "Дисплей",
        "category_1c": "",
        "item_tags": [],
        "brand_compatibility": "Apple",
        "model_compatibility": "" if "model_compatibility" in missing_required_attributes else "13",
        "quality_raw": "" if "quality_raw" in missing_required_attributes else "ORIG100",
        "quality_normalized": "original",
        "characteristic_values": {},
        "price_segment": "mid_high",
        "data_quality_score": data_quality_score,
        "missing_required_attributes": missing_required_attributes,
        "future_ka_mapping_status": future_ka_mapping_status,
        "calculation_unit_level": calculation_unit_level,
        "calculation_unit_key": code,
        "calculation_unit_source": "1c_properties",
        "calculation_unit_confidence": data_quality_score,
        "calculation_unit_reason": "",
        "demand_method_code": demand_method_code,
        "demand_method_reason": "",
        "demand_method_confidence": data_quality_score,
        "sales_point_warehouse_codes": [],
        "manager_need_signals": [],
        "source_record": {},
        "source_hash": f"hash-{code}",
        "source": "test",
        "classified_at": datetime(2026, 7, 3, 10, 0, 0),
    }
