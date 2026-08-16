from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, text

from app.services.assortment_lifecycle_facts import (
    DocumentLineMapping,
    _chunks,
    _folder_like_patterns,
    build_assortment_lifecycle_fact_records,
    enrich_nomenclature_rows_with_product_snapshot,
    validate_document_line_mapping,
    validate_warehouse_policy,
)


def _warehouse_policy() -> list[dict[str, object]]:
    return validate_warehouse_policy(
        {
            "warehouses": [
                {"warehouse_code": "shop-1", "sells_systematically": True},
                {"warehouse_code": "central", "is_central": True},
                {"warehouse_code": "defect", "is_defect_warehouse": True},
                {"warehouse_code": "transit", "is_transit": True},
                {"warehouse_code": "rare", "is_non_systematic_sale": True},
            ]
        }
    )


def test_build_facts_from_rows_builds_cargo_receipts_and_overlays() -> None:
    facts, summary = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "name": "Дисплей тестовый A",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                "short_name_1c": "Дисп. тест A (ORIG)",
                "additional_name_1c": "Display test A (ORIG)",
                "vendor_sku_1c": "OEM-DSP-TEST-BLK-OR",
                "article": "SKU-001",
                "subject_1c": "Дисплей",
                "tag": "iPhone, рамка",
                "quality_raw": "ORIG100",
                "brand_compatibility": "Apple",
                "model_compatibility": "iPhone 13",
                "characteristic_values": {"frame": "yes", "ic_pad": "yes"},
                "created_at": "2025-12-20",
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
        supplier_order_rows=[
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
        receipt_rows=[
            {"nomenclature_ref": "0xA", "receipt_date": "2026-01-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-02-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-03-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-04-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-05-10"},
        ],
        warehouse_policy=_warehouse_policy(),
        manual_overrides={
            "РБ0001": {
                "working_confirmed_by_folder_responsible": True,
                "analog_winner_confirmed_by_folder_responsible": True,
                "manual_expensive_profile": "fast_expensive",
            }
        },
        manager_signals={
            "РБ0001": [
                {
                    "manager_id": "manager-1",
                    "quantity": 1,
                    "source": "offline_call",
                    "signal_date": "2026-01-03",
                    "comment": "Клиент спрашивал",
                }
            ]
        },
        history_start=date(2025, 12, 1),
    )

    first = facts[0]
    assert summary["items"] == 2
    assert first["nomenclature_code"] == "РБ0001"
    assert first["card_created_at"] == "2025-12-20"
    assert first["short_name_1c"] == "Дисп. тест A (ORIG)"
    assert first["additional_name_1c"] == "Display test A (ORIG)"
    assert first["vendor_sku_1c"] == "OEM-DSP-TEST-BLK-OR"
    assert first["first_supplier_order_at"] == "2026-01-01"
    assert first["supplier_order_cargo_handoff_dates"] == ["2026-01-05", "2026-02-05"]
    assert first["receipt_dates"] == [
        "2026-01-10",
        "2026-02-10",
        "2026-03-10",
        "2026-04-10",
        "2026-05-10",
    ]
    assert first["has_need_signal"] is True
    assert first["manager_need_signals"][0]["manager_id"] == "manager-1"
    assert first["expensive_item_value"] == "300"
    assert first["expensive_group_values"] == ["300", "100"]
    assert first["expensive_route_days"] == 5
    assert first["working_confirmed_by_folder_responsible"] is True
    assert first["analog_winner_confirmed_by_folder_responsible"] is True
    assert first["manual_expensive_profile"] == "fast_expensive"
    assert first["feature_snapshot_schema"] == "procurement_feature_snapshot.v1"
    assert first["article"] == "SKU-001"
    assert first["subject_1c"] == "Дисплей"
    assert first["item_tags"] == ["iPhone", "рамка"]
    assert first["quality_normalized"] == "original"
    assert first["characteristic_values"] == {"frame": "yes", "ic_pad": "yes"}
    assert first["price_segment"] == "mid_high"
    assert first["missing_required_attributes"] == []
    assert first["data_quality_score"] == "1.00"
    assert first["future_ka_mapping_status"] == "ready"
    assert first["calculation_unit_level"] == "subject_tag"
    assert first["calculation_unit_source"] == "1c_properties"
    assert first["demand_method_code"] == "store_need"
    assert [warehouse["warehouse_code"] for warehouse in first["warehouses"]] == [
        "shop-1",
        "central",
        "defect",
        "transit",
        "rare",
    ]


def test_build_facts_excludes_bitok_before_events_and_summary() -> None:
    facts, summary = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "KEEP",
                "name": "Дисплей обычный",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            },
            {
                "nomenclature_ref": "0xB",
                "nomenclature_code": "DROP",
                "name": "Дисплей (БИТОК)",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            },
        ],
        supplier_order_rows=[{"nomenclature_ref": "0xB", "order_date": "2026-08-01"}],
        receipt_rows=[{"nomenclature_ref": "0xB", "receipt_date": "2026-08-10"}],
        warehouse_policy=_warehouse_policy(),
    )

    assert [fact["nomenclature_code"] for fact in facts] == ["KEEP"]
    assert summary["scope_policy"]["excluded_reason_counts"] == {"excluded_display_name_bitok": 1}


def test_build_facts_marks_history_truncated_when_first_event_hits_boundary() -> None:
    facts, summary = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            }
        ],
        supplier_order_rows=[
            {
                "nomenclature_ref": "0xA",
                "order_date": "2025-01-01",
                "cargo_handoff_date": "2025-01-05",
            }
        ],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        history_start=date(2025, 1, 1),
    )

    assert facts[0]["warnings"] == ["history_truncated"]
    assert summary["warnings"] == {"history_truncated": 1}


def test_manual_override_maps_legacy_exclusive_status_to_commercial_mark() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        manual_overrides={
            "РБ0001": {
                "manual_status": "exclusive",
                "manual_reason": "Товар есть только у нас",
                "manual_approved_by": "Омар",
                "manual_changed_at": "2026-06-27",
            }
        },
    )

    assert "manual_status" not in facts[0]
    assert facts[0]["commercial_marks"] == ["exclusive"]
    assert facts[0]["exclusive_reason"] == "Товар есть только у нас"
    assert facts[0]["exclusive_approved_by"] == "Омар"
    assert facts[0]["exclusive_checked_at"] == "2026-06-27"


def test_manual_do_not_order_override_blocks_demand_formula() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        manual_overrides={
            "РБ0001": {
                "manual_status": "do_not_order",
                "manual_reason": "Родился мертвым",
                "manual_approved_by": "chat",
                "manual_changed_at": "2026-07-03",
            }
        },
    )

    assert facts[0]["manual_status"] == "do_not_order"
    assert facts[0]["demand_method_code"] == "manual_review"
    assert facts[0]["demand_method_confidence"] == "0.00"
    assert (
        facts[0]["demand_method_reason"]
        == "Есть ручной стоп или статус, обычную формулу не применяем."
    )


def test_feature_snapshot_infers_display_subject_and_model_without_quality() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ000030751",
                "name": (
                    "Дисплей для LeEco Le 2 (X520/X526/X527) / Le 2 (X620) "
                    "(в сборе с тачскрином) (розовый)"
                ),
                "folder_path": "ОБЩИЙ КАТАЛОГ / Дисплеи для LeEco",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
    )

    assert facts[0]["subject_1c"] == "дисплей"
    assert facts[0]["brand_compatibility"] == "LeEco"
    assert facts[0]["model_compatibility"] == "LeEco Le 2 (X520/X526/X527) / Le 2 (X620)"
    assert facts[0]["missing_required_attributes"] == ["quality_raw"]
    assert facts[0]["data_quality_score"] == "0.67"
    assert facts[0]["future_ka_mapping_status"] == "needs_mapping"
    assert facts[0]["calculation_unit_level"] == "property_group"


def test_matrix_folder_is_treated_as_display_scope() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ000042811",
                "name": "Матрица для Lenovo ThinkPad T480 14.0 Slim 30 pin",
                "folder_path": "ОБЩИЙ КАТАЛОГ / Запчасти для ноутбуков / Матрицы",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
    )

    assert facts[0]["subject_1c"] == "дисплей"
    assert facts[0]["model_compatibility"] == "Lenovo ThinkPad T480 14.0 Slim 30 pin"
    assert facts[0]["brand_compatibility"] == "Lenovo"
    assert facts[0]["missing_required_attributes"] == ["quality_raw"]
    assert _folder_like_patterns("дисплеи") == ("%дисплеи%", "%Матриц%")


def test_enrich_nomenclature_rows_with_product_snapshot_adds_product_attributes() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE product ("
                "id INTEGER PRIMARY KEY, article TEXT, fact_sku TEXT, code_1c TEXT, "
                "info_system_code TEXT, name TEXT, brand TEXT, category TEXT, "
                "subject TEXT, subject_1c TEXT, vid_nomenklatury_1c TEXT, "
                "quality_raw TEXT, display_quality_raw TEXT, quality TEXT, display_quality TEXT, "
                "display_type TEXT, display_construction TEXT, display_refresh_rate_hz INTEGER, "
                "display_screen_kit TEXT, display_has_frame BOOLEAN, display_has_touch BOOLEAN, "
                "display_has_ic_pad BOOLEAN, display_has_binding_no_solder BOOLEAN, "
                "display_backlight TEXT, display_matrix_tags TEXT, display_diagonal TEXT, "
                "display_resolution TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE productcompatibility (" "product_id INTEGER, value TEXT, source TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO product "
                "(id, article, fact_sku, code_1c, info_system_code, name, category, "
                "subject_1c, quality_raw, quality, display_quality, display_type, "
                "display_has_frame) "
                "VALUES "
                "(1, '022904', 'OEM-DSP-IPD34-OR', 'РБ000006737', 'abc', "
                "'Дисплей тестовый', 'Дисплеи для планшетов', 'дисплей', "
                "'ORIG', 'Original', 'Original', 'In-Cell', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO productcompatibility (product_id, value, source) "
                "VALUES (1, 'Apple iPad 3', 'onec'), (1, 'Apple iPad 4', 'onec')"
            )
        )

    enriched = enrich_nomenclature_rows_with_product_snapshot(
        engine,
        [
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ000006737",
                "name": "Дисплей тестовый",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                "item_value": "300",
            }
        ],
    )
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=enriched,
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
    )

    assert enriched[0]["subject_1c"] == "дисплей"
    assert enriched[0]["quality_raw"] == "ORIG"
    assert enriched[0]["model_compatibility"] == "Apple iPad 3 / Apple iPad 4"
    assert enriched[0]["brand_compatibility"] == "Apple"
    assert enriched[0]["characteristic_values"] == {
        "display_type": "In-Cell",
        "display_has_frame": False,
    }
    assert facts[0]["future_ka_mapping_status"] == "ready"
    assert facts[0]["missing_required_attributes"] == []
    assert facts[0]["calculation_unit_level"] == "property_group"


def test_validate_document_line_mapping_reports_missing_receipt_mapping_parts() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE receipt_doc (_IDRRef TEXT, _Date_Time TEXT)"))

    issues = validate_document_line_mapping(
        engine,
        DocumentLineMapping(
            document_table="receipt_doc",
            line_table="receipt_lines",
            line_document_column="_DocumentRRef",
            line_nomenclature_column="_FldNom",
        ),
    )

    assert "table_missing:receipt_lines" in issues
    assert "column_missing:receipt_doc._Marked" in issues
    assert "column_missing:receipt_doc._Posted" in issues


def test_chunks_preserve_all_refs_for_sqlserver_parameter_limit() -> None:
    refs = [f"0x{idx:032X}" for idx in range(5)]

    chunks = list(_chunks(refs, 2))

    assert chunks == [refs[0:2], refs[2:4], refs[4:5]]
