from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, text

from app.services.assortment_lifecycle_facts import (
    DocumentLineMapping,
    _chunks,
    _folder_like_patterns,
    _sales_distribution_from_rows,
    build_assortment_lifecycle_fact_records,
    enrich_nomenclature_rows_with_product_snapshot,
    fetch_onec_item_inventory_costs,
    is_display_assortment_record,
    validate_document_line_mapping,
    validate_warehouse_policy,
)


class _InventoryCostRows:
    def mappings(self) -> _InventoryCostRows:
        return self

    def __iter__(self):
        return iter(
            [
                {
                    "nomenclature_code": "SKU-1",
                    "party_quantity": Decimal("2"),
                    "party_amount": Decimal("300"),
                }
            ]
        )


class _InventoryCostConnection:
    def __init__(self, calls: list[tuple[str, dict[str, object]]]) -> None:
        self.calls = calls

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        return _InventoryCostRows()


class _InventoryCostEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @contextmanager
    def connect(self):
        yield _InventoryCostConnection(self.calls)


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
        stock_inflow_bounds={"РБ0001": (date(2025, 12, 25), date(2026, 5, 10))},
        as_of=date(2026, 5, 20),
    )

    first = facts[0]
    assert summary["items"] == 2
    assert first["nomenclature_code"] == "РБ0001"
    assert first["card_created_at"] == "2025-12-20"
    assert first["short_name_1c"] == "Дисп. тест A (ORIG)"
    assert first["additional_name_1c"] == "Display test A (ORIG)"
    assert first["vendor_sku_1c"] == "OEM-DSP-TEST-BLK-OR"
    assert first["first_supplier_order_at"] == "2026-01-01"
    assert first["first_stock_inflow_at"] == "2025-12-25"
    assert first["last_stock_inflow_at"] == "2026-05-10"
    assert first["history_age_days"] == (date(2026, 5, 20) - date(2026, 1, 10)).days
    assert first["supplier_order_cargo_handoff_dates"] == ["2026-01-05", "2026-02-05"]
    assert first["receipt_dates"] == [
        "2026-01-10",
        "2026-02-10",
        "2026-03-10",
        "2026-04-10",
        "2026-05-10",
    ]
    assert first["has_need_signal"] is True
    assert first["has_external_need_signal"] is True
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


def test_internal_warehouse_signal_is_not_external_customer_need() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "name": "Дисплей тестовый A",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        manager_signals={
            "РБ0001": [
                {
                    "manager_id": "warehouse_internal",
                    "signal_type": "internal_warehouse_need",
                    "quantity": 1,
                    "source": "warehouse_transfer",
                    "signal_date": "2026-01-03",
                }
            ]
        },
    )

    assert facts[0]["has_need_signal"] is True
    assert facts[0]["has_external_need_signal"] is False


def test_complete_sale_history_turns_missing_sale_row_into_proven_zero() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "name": "Дисплей без продаж",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        first_sale_dates={},
        sales_history_complete=True,
    )

    assert facts[0]["first_sale_at"] is None
    assert facts[0]["last_sale_at"] is None
    assert facts[0]["lifetime_sales_qty"] == "0"


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


def test_display_scope_keeps_archived_display_but_rejects_matrix_accessories() -> None:
    assert is_display_assortment_record({"folder_path": "Архив", "subject_1c": "Дисплей"})
    assert is_display_assortment_record(
        {
            "folder_path": "Архив",
            "name": "Дубликат Дисплей iPhone 6S в сборе с тачскрином",
        }
    )
    assert is_display_assortment_record(
        {
            "folder_path": "удалить",
            "name": "Дисплеи для Lenovo IdeaPhone A606",
            "category_1c": "Дисплеи для телефонов",
        }
    )
    assert not is_display_assortment_record(
        {
            "folder_path": "Проклейки для Apple MacBook",
            "subject_1c": "скотч",
            "name": "Проклейка матрицы Apple MacBook Air",
        }
    )
    assert not is_display_assortment_record(
        {
            "folder_path": "Шлейфы для Apple MacBook",
            "category_1c": "Шлейфы для ноутбуков",
            "name": "Шлейф для Apple MacBook с компонентами на матрицу",
        }
    )


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


def test_full_history_receipt_bounds_preserve_old_age_after_new_receipt() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "OLD-1",
                "folder_path": "Дисплеи",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[
            {"nomenclature_ref": "0xA", "receipt_date": "2026-07-01"},
        ],
        receipt_bounds={"OLD-1": (date(2018, 1, 10), date(2026, 7, 1))},
        warehouse_policy=_warehouse_policy(),
        as_of=date(2026, 8, 12),
        history_start=date(2024, 8, 1),
    )
    fact = facts[0]
    assert fact["first_receipt_at"] == "2018-01-10"
    assert fact["last_receipt_at"] == "2026-07-01"
    assert fact["history_age_days"] == (date(2026, 8, 12) - date(2018, 1, 10)).days


def test_full_history_supplier_order_date_prevents_old_item_becoming_fruit() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "OLD-ORDER-1",
                "folder_path": "Дисплеи",
            }
        ],
        supplier_order_rows=[],
        first_supplier_order_dates={"OLD-ORDER-1": date(2014, 6, 1)},
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        history_start=date(2024, 8, 1),
    )

    assert facts[0]["first_supplier_order_at"] == "2014-06-01"
    assert "history_truncated" not in facts[0]["warnings"]


def test_fact_snapshot_carries_previous_classification_audit_fields() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "PREVIOUS-1",
                "folder_path": "Дисплеи",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        previous_classifications={
            "PREVIOUS-1": {
                "status": "working",
                "auto_order_allowed": False,
                "manual_review_required": True,
                "blockers": ["legacy_blocker"],
                "reason_codes": ["legacy_reason"],
            }
        },
    )

    fact = facts[0]
    assert fact["previous_status"] == "working"
    assert fact["previous_classification_available"] is True
    assert fact["previous_auto_order_allowed"] is False
    assert fact["previous_manual_review_required"] is True
    assert fact["previous_blockers"] == ["legacy_blocker"]
    assert fact["previous_reason_codes"] == ["legacy_reason"]


def test_current_inventory_cost_uses_current_party_totals_only() -> None:
    engine = _InventoryCostEngine()

    result = fetch_onec_item_inventory_costs(
        engine,
        nomenclature_codes=["SKU-1"],
        as_of=date.today(),
    )

    assert result == {"SKU-1": Decimal("150")}
    sql, params = engine.calls[0]
    assert "FROM dbo._AccumRgT7473 AS t" in sql
    assert "FROM dbo._AccumRg7453 AS r" not in sql
    assert params["current_totals_period"] is not None


def test_historical_inventory_cost_rebuilds_month_total_without_future_movements() -> None:
    engine = _InventoryCostEngine()
    as_of = date.today() - timedelta(days=40)

    result = fetch_onec_item_inventory_costs(
        engine,
        nomenclature_codes=["SKU-1"],
        as_of=as_of,
    )

    assert result == {"SKU-1": Decimal("150")}
    sql, params = engine.calls[0]
    assert "FROM dbo._AccumRgT7473 AS t" in sql
    assert "FROM dbo._AccumRg7453 AS r" in sql
    assert "r._Period >= :month_start" in sql
    assert "r._Period < :date_to" in sql
    assert "SUM(party.quantity) > 0" in sql
    assert "SUM(party.amount) >= 0" in sql
    assert params["month_start"].date() == as_of.replace(day=1)
    assert params["date_to"].date() == as_of + timedelta(days=1)


def test_inventory_cost_rejects_future_as_of_before_querying_onec() -> None:
    engine = _InventoryCostEngine()

    try:
        fetch_onec_item_inventory_costs(
            engine,
            nomenclature_codes=["SKU-1"],
            as_of=date.today() + timedelta(days=1),
        )
    except ValueError as exc:
        assert str(exc) == "inventory_cost_as_of_cannot_be_future"
    else:
        raise AssertionError("future inventory cost date must be rejected")

    assert engine.calls == []


def test_cost_quartile_uses_fallback_group_and_unknown_cost_has_no_minimum() -> None:
    warehouses = [
        {
            "warehouse_code": f"shop-{index}",
            "role": "physical_sales_point",
            "sells_systematically": True,
        }
        for index in range(11)
    ] + [
        {
            "warehouse_code": "site",
            "role": "online_site_reserve",
            "sells_systematically": True,
        },
        {
            "warehouse_code": "wholesale",
            "role": "wholesale",
            "sells_systematically": True,
        },
        {
            "warehouse_code": "cdek",
            "role": "central_transfer_stock",
            "sells_systematically": False,
            "is_transit": True,
        },
    ]
    rows = [
        {
            "nomenclature_ref": f"0x{index}",
            "nomenclature_code": f"SKU-{index}",
            "folder_path": "Дисплеи",
            "quality_raw": "Original",
            "brand_compatibility": f"Brand-{index}",
            "name": f"Дисплей {index}",
        }
        for index in range(8)
    ]
    rows.append(
        {
            "nomenclature_ref": "0xUNKNOWN",
            "nomenclature_code": "SKU-UNKNOWN",
            "folder_path": "Дисплеи",
            "quality_raw": "Original",
            "name": "Дисплей без себестоимости",
        }
    )
    costs = {f"SKU-{index}": Decimal(str((index + 1) * 100)) for index in range(8)}
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=rows,
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=warehouses,
        inventory_costs=costs,
        comparable_group_min_size=8,
    )
    cheapest = next(item for item in facts if item["nomenclature_code"] == "SKU-0")
    unknown = next(item for item in facts if item["nomenclature_code"] == "SKU-UNKNOWN")
    assert cheapest["cost_quartile"] == "Q1"
    assert cheapest["cost_group_sample_size"] == 8
    assert cheapest["minimum_representation_qty"] == 13
    assert unknown["inventory_cost_per_unit"] is None
    assert unknown["cost_quartile"] == ""
    assert unknown["minimum_representation_qty"] is None


def test_sales_distribution_keeps_daily_anonymous_observations() -> None:
    result = _sales_distribution_from_rows(
        [
            {
                "nomenclature_code": "SKU-1",
                "business_date": "2026-08-01",
                "document_ref": "DOC-1",
                "customer_ref": "CUSTOMER-1",
                "sales_point_ref": "SHOP-1",
                "quantity": "7",
            },
            {
                "nomenclature_code": "SKU-1",
                "business_date": "2026-08-02",
                "document_ref": "DOC-2",
                "customer_ref": "CUSTOMER-2",
                "sales_point_ref": "SHOP-2",
                "quantity": "3",
            },
        ]
    )["SKU-1"]
    assert result["sales_active_days_short"] == 2
    assert result["sales_document_count_short"] == 2
    assert result["sales_customer_count_short"] == 2
    assert result["sales_point_count_short"] == 2
    assert result["sales_max_day_share_short"] == "0.7"
    observations = result["sales_observations_short"]
    assert observations[0]["business_date"] == "2026-08-01"
    assert observations[0]["quantity"] == "7"
    assert observations[0]["document_id"] != "DOC-1"
    assert observations[0]["customer_id"] != "CUSTOMER-1"
