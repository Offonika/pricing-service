from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, insert

from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
    ASSORTMENT_LIFECYCLE_METADATA,
)
from tasks.build_missing_display_quality_updates import (
    QUALITY_PROPERTY_NAME,
    build_missing_display_quality_update_rows,
    build_reference_quality_by_model,
    load_missing_display_quality_candidates,
    load_quality_overrides,
    load_status_review_exclusions,
    write_candidates_csv,
    write_rows_json,
)


def test_build_missing_display_quality_update_rows_uses_manual_map() -> None:
    result = build_missing_display_quality_update_rows(
        [
            _candidate("РБ0001", "Apple", "Apple iPhone 13"),
            _candidate("РБ0002", "Apple", "Apple iPhone 14"),
        ],
        quality_overrides={
            "РБ0001": {
                "quality_raw": "Medium",
                "reason": "Проверено менеджером папки.",
                "approved_by": "Омар",
            }
        },
        quality_catalog_values={"Medium", "ORIG100"},
        run_date=date(2026, 7, 3),
    )

    assert len(result.rows) == 1
    assert result.rows[0].idempotency_key == "nom-prop:РБ0001:Качество:2026-07-03:r1"
    assert result.rows[0].property_name == QUALITY_PROPERTY_NAME
    assert result.rows[0].value_type == "property_value"
    assert result.rows[0].new_value_name == "Medium"
    assert result.rows[0].approved_by == "Омар"
    assert result.candidates[0]["update_ready"] is True
    assert result.candidates[1]["skip_reason"] == "quality_not_suggested"
    assert result.source_counts == {"manual_map": 1}
    assert result.quality_counts == {"Medium": 1}


def test_same_model_quality_is_report_hint_until_explicitly_allowed() -> None:
    reference_quality = build_reference_quality_by_model(
        [
            {
                "brand_compatibility": "Apple",
                "model_compatibility": "Apple iPhone 13",
                "quality_raw": "ORIG100",
            }
        ]
    )

    result = build_missing_display_quality_update_rows(
        [_candidate("РБ0001", "Apple", "Apple iPhone 13")],
        reference_quality_by_key=reference_quality,
        run_date=date(2026, 7, 3),
    )

    assert result.rows == ()
    assert result.candidates[0]["suggested_quality_raw"] == "ORIG100"
    assert result.candidates[0]["suggestion_source"] == "same_model_snapshot"
    assert result.candidates[0]["update_ready"] is False
    assert result.candidates[0]["skip_reason"] == "needs_manual_quality_approval"


def test_same_model_quality_can_be_exported_when_allowed() -> None:
    reference_quality = {("apple", "apple iphone 13"): "ORIG100"}

    result = build_missing_display_quality_update_rows(
        [_candidate("РБ0001", "Apple", "Apple iPhone 13")],
        reference_quality_by_key=reference_quality,
        allow_reference_updates=True,
        run_date=date(2026, 7, 3),
    )

    assert len(result.rows) == 1
    assert result.rows[0].new_value_name == "ORIG100"
    assert result.candidates[0]["update_ready"] is True


def test_name_token_quality_is_canonicalized_for_1c_raw_value() -> None:
    candidate = _candidate("РБ0001", "Huawei", "Huawei Watch GT 2e")
    candidate["name"] = "Дисплей для Huawei Watch GT 2e (ORIG)"
    candidate["source_record"] = {"card_created_at": "2026-06-01"}

    result = build_missing_display_quality_update_rows(
        [candidate],
        run_date=date(2026, 7, 3),
    )

    assert len(result.rows) == 1
    assert result.rows[0].new_value_name == "ORIG"
    assert result.candidates[0]["suggestion_source"] == "name_token"


def test_name_token_quality_uses_display_quality_normalization() -> None:
    candidates = [
        {
            **_candidate("РБ0001", "Huawei", "Huawei P20"),
            "name": "Дисплей для Huawei P20 - Оптима",
            "source_record": {"card_created_at": "2026-06-01"},
        },
        {
            **_candidate("РБ0002", "Huawei", "Huawei P30"),
            "name": "Дисплей для Huawei P30 - Стандарт",
            "source_record": {"card_created_at": "2026-06-01"},
        },
        {
            **_candidate("РБ0003", "Huawei", "Huawei P40"),
            "name": "Дисплей для Huawei P40 Premium Quality",
            "source_record": {"card_created_at": "2026-06-01"},
        },
    ]

    result = build_missing_display_quality_update_rows(
        candidates,
        run_date=date(2026, 7, 3),
    )

    assert [row.new_value_name for row in result.rows] == ["Optima", "Medium", "Premium"]


def test_display_type_token_is_not_exported_as_quality() -> None:
    candidate = _candidate("РБ0001", "Vsmart", "Vsmart Live")
    candidate["name"] = "Дисплей для Vsmart Live (OLED)"

    result = build_missing_display_quality_update_rows(
        [candidate],
        run_date=date(2026, 7, 3),
    )

    assert result.rows == ()
    assert result.candidates[0]["suggested_quality_raw"] == ""
    assert result.candidates[0]["skip_reason"] == "quality_not_suggested"


def test_quality_can_be_detected_from_onec_additional_names() -> None:
    candidate = _candidate("РБ0001", "Huawei", "Huawei Watch GT 2e")
    candidate["name"] = "Дисплей для Huawei Watch GT 2e"
    candidate["source_record"] = {
        "card_created_at": "2026-06-01",
        "short_name_1c": "Дисп. HUA WTCH GT 2e (ORIG)",
        "additional_name_1c": "Display for Huawei Watch GT 2e (ORIG)",
    }

    result = build_missing_display_quality_update_rows(
        [candidate],
        run_date=date(2026, 7, 3),
    )

    assert len(result.rows) == 1
    assert result.rows[0].new_value_name == "ORIG"
    assert result.candidates[0]["evidence_field"] == "source_record.short_name_1c"
    assert "ORIG" in result.candidates[0]["evidence_text"]


def test_vendor_sku_quality_uses_suffix_not_oem_prefix() -> None:
    ready_candidate = _candidate("РБ0001", "Huawei", "Huawei Watch GT 2e")
    ready_candidate["name"] = "Дисплей для Huawei Watch GT 2e"
    ready_candidate["source_record"] = {
        "card_created_at": "2026-06-01",
        "vendor_sku_1c": "OEM-DSP-HWE-WGT2E-BLK-OR",
    }
    blocked_candidate = _candidate("РБ0002", "Samsung", "Samsung R800 Galaxy Watch")
    blocked_candidate["name"] = "Дисплей для Samsung R800 Galaxy Watch"
    blocked_candidate["source_record"] = {"vendor_sku_1c": "OEM-DSP-SMG-R800-BLK"}

    result = build_missing_display_quality_update_rows(
        [ready_candidate, blocked_candidate],
        run_date=date(2026, 7, 3),
    )

    assert len(result.rows) == 1
    assert result.rows[0].new_value_name == "ORIG"
    assert result.candidates[0]["suggestion_source"] == "vendor_sku_suffix"
    assert result.candidates[1]["skip_reason"] == "quality_not_suggested"


def test_card_quality_is_blocked_when_card_is_older_than_six_months() -> None:
    candidate = _candidate("РБ0001", "Huawei", "Huawei Watch GT 2e")
    candidate["name"] = "Дисплей для Huawei Watch GT 2e (ORIG)"
    candidate["source_record"] = {"card_created_at": "2025-12-09"}

    result = build_missing_display_quality_update_rows(
        [candidate],
        run_date=date(2026, 7, 3),
    )

    assert result.rows == ()
    assert result.candidates[0]["suggested_quality_raw"] == "ORIG"
    assert result.candidates[0]["update_ready"] is False
    assert result.candidates[0]["skip_reason"] == "card_older_than_6_months"
    assert result.candidates[0]["card_created_at"] == "2025-12-09"
    assert result.candidates[0]["card_age_days"] == "206"


def test_card_quality_is_blocked_when_card_age_is_unknown() -> None:
    candidate = _candidate("РБ0001", "Huawei", "Huawei Watch GT 2e")
    candidate["name"] = "Дисплей для Huawei Watch GT 2e (ORIG)"

    result = build_missing_display_quality_update_rows(
        [candidate],
        run_date=date(2026, 7, 3),
    )

    assert result.rows == ()
    assert result.candidates[0]["suggested_quality_raw"] == "ORIG"
    assert result.candidates[0]["skip_reason"] == "card_age_unknown"


def test_quality_catalog_rejects_unknown_value() -> None:
    result = build_missing_display_quality_update_rows(
        [_candidate("РБ0001", "Apple", "Apple iPhone 13")],
        quality_overrides={"РБ0001": {"quality_raw": "Unknown"}},
        quality_catalog_values={"Medium"},
        run_date=date(2026, 7, 3),
    )

    assert result.rows == ()
    assert result.candidates[0]["skip_reason"] == "quality_catalog_value_missing"


def test_load_missing_display_quality_candidates_skips_do_not_order_by_default() -> None:
    engine = create_engine("sqlite:///:memory:")
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE),
            [
                _classification_row("РБ0001", status="sale", status_label="ПРОДАЖА"),
                _classification_row(
                    "РБ0002",
                    status="do_not_order",
                    status_label="Не закупать",
                ),
            ],
        )

    rows = load_missing_display_quality_candidates(engine)
    audit_rows = load_missing_display_quality_candidates(engine, include_do_not_order=True)

    assert [row["nomenclature_code"] for row in rows] == ["РБ0001"]
    assert [row["nomenclature_code"] for row in audit_rows] == ["РБ0001", "РБ0002"]


def test_load_missing_display_quality_candidates_skips_status_review_exclusions() -> None:
    engine = create_engine("sqlite:///:memory:")
    ASSORTMENT_LIFECYCLE_METADATA.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE),
            [
                _classification_row("РБ0001", status="sale", status_label="ПРОДАЖА"),
                _classification_row("РБ0002", status="sale", status_label="ПРОДАЖА"),
            ],
        )

    rows = load_missing_display_quality_candidates(
        engine,
        excluded_status_review_codes={"РБ0001"},
    )

    assert [row["nomenclature_code"] for row in rows] == ["РБ0002"]


def test_load_quality_overrides_accepts_mapping_and_items(tmp_path: Path) -> None:
    mapping_path = tmp_path / "quality-map.json"
    mapping_path.write_text('{"РБ0001": "Medium"}', encoding="utf-8")
    assert load_quality_overrides(mapping_path) == {
        "РБ0001": {"quality_raw": "Medium", "reason": "manual_map"}
    }

    items_path = tmp_path / "quality-items.json"
    items_path.write_text(
        '{"items":[{"nomenclature_code":"РБ0002","quality_raw":"ORIG100","reason":"ok"}]}',
        encoding="utf-8",
    )
    assert load_quality_overrides(items_path)["РБ0002"]["quality_raw"] == "ORIG100"


def test_load_status_review_exclusions_accepts_items_and_strings(tmp_path: Path) -> None:
    path = tmp_path / "status-review.json"
    path.write_text(
        """
        {
          "items": [
            "РБ0001",
            {"nomenclature_code": "РБ0002", "exclude_from_quality_queue": true},
            {"nomenclature_code": "РБ0003", "exclude_from_quality_queue": false}
          ]
        }
        """,
        encoding="utf-8",
    )

    assert load_status_review_exclusions(path) == {"РБ0001", "РБ0002"}


def test_write_report_and_rows_json(tmp_path: Path) -> None:
    result = build_missing_display_quality_update_rows(
        [_candidate("РБ0001", "Apple", "Apple iPhone 13")],
        quality_overrides={"РБ0001": {"quality_raw": "Medium"}},
        run_date=date(2026, 7, 3),
    )
    csv_path = tmp_path / "report.csv"
    rows_path = tmp_path / "rows.json"

    write_candidates_csv(csv_path, result.candidates)
    write_rows_json(rows_path, result.rows)

    assert "suggested_quality_raw" in csv_path.read_text(encoding="utf-8-sig")
    assert '"property_name": "Качество"' in rows_path.read_text(encoding="utf-8")


def _candidate(code: str, brand: str, model: str) -> dict[str, object]:
    return {
        "nomenclature_code": code,
        "name": f"Дисплей для {model}",
        "folder": f"Дисплеи для {brand}",
        "status": "fruit",
        "status_label": "Плод",
        "brand_compatibility": brand,
        "model_compatibility": model,
        "data_quality_score": "0.67",
        "calculation_unit_level": "property_group",
        "demand_method_code": "manual_review",
        "missing_required_attributes": ["quality_raw"],
    }


def _classification_row(
    code: str,
    *,
    status: str,
    status_label: str,
) -> dict[str, object]:
    return {
        "nomenclature_code": code,
        "name": f"Дисплей тестовый {code}",
        "folder": "ОБЩИЙ КАТАЛОГ / дисплеи",
        "status": status,
        "status_label": status_label,
        "recommended_status": None,
        "reason_codes": [],
        "reason_text": "",
        "blockers": [],
        "export_blockers": [],
        "auto_order_allowed": False,
        "manual_review_required": False,
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
        "model_compatibility": "Apple Watch",
        "quality_raw": "",
        "quality_normalized": "",
        "characteristic_values": {},
        "price_segment": "",
        "data_quality_score": "0.67",
        "missing_required_attributes": ["quality_raw"],
        "future_ka_mapping_status": "needs_mapping",
        "calculation_unit_level": "property_group",
        "calculation_unit_key": code,
        "calculation_unit_source": "1c_properties",
        "calculation_unit_confidence": "0.67",
        "calculation_unit_reason": "",
        "demand_method_code": "manual_review",
        "demand_method_reason": "",
        "demand_method_confidence": "0.67",
        "sales_point_warehouse_codes": [],
        "manager_need_signals": [],
        "source_record": {},
        "source_hash": f"hash-{code}",
        "source": "test",
        "classified_at": datetime(2026, 7, 3, 10, 0, 0),
    }
