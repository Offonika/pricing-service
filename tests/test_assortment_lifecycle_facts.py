from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, text

from app.services.assortment_lifecycle_facts import (
    DocumentLineMapping,
    build_assortment_lifecycle_fact_records,
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
    assert first["manual_expensive_profile"] == "fast_expensive"
    assert [warehouse["warehouse_code"] for warehouse in first["warehouses"]] == [
        "shop-1",
        "central",
        "defect",
        "transit",
        "rare",
    ]


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
