from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.services.assortment_lifecycle_facts import DocumentLineMapping
from app.services.assortment_lifecycle_signal_ingestion import (
    display_family_registry_snapshot_from_mapping,
)
from app.services.assortment_lifecycle_signal_sources import (
    AssortmentSignalSourceRows,
    build_assortment_signal_source_bundle,
    fetch_customer_sale_signal_rows,
    fetch_registry_nomenclature_rows,
    fetch_supplier_order_signal_rows,
    fetch_supplier_receipt_signal_rows,
)
from app.services.onec_stock_availability import CurrentStockSnapshot


def _registry(*codes: str):
    return display_family_registry_snapshot_from_mapping(
        {
            "schema": "display_family_registry_snapshot.v1",
            "version_number": 7,
            "status": "active",
            "members": [
                {
                    "product_id": index,
                    "family_key": "iphone-17-pro-max",
                    "nomenclature_code": code,
                    "aliases": [f"ARTICLE-{code}"],
                    "name": f"Дисплей {code}",
                }
                for index, code in enumerate(codes, start=1)
            ],
        }
    )


def _stock(
    quantities: dict[str, Decimal] | None = None,
    *,
    captured_at: datetime = datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
) -> CurrentStockSnapshot:
    values = quantities or {"SKU-1": Decimal("8")}
    return CurrentStockSnapshot(
        captured_at=captured_at,
        source_period=datetime(5999, 11, 1),
        source_row_count=len(values),
        product_code_count=len(values),
        positive_row_count=len(values),
        positive_product_code_count=len(values),
        total_positive_quantity=sum(values.values(), Decimal("0")),
        total_net_quantity=sum(values.values(), Decimal("0")),
        quantities_by_code=values,
        net_quantities_by_code=values,
        source_status="ready",
    )


def _rows(
    *,
    nomenclature_rows: list[dict[str, Any]] | None = None,
    sales: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
    receipts: list[dict[str, Any]] | None = None,
    stock: CurrentStockSnapshot | None = None,
) -> AssortmentSignalSourceRows:
    return AssortmentSignalSourceRows(
        nomenclature_rows=tuple(
            nomenclature_rows
            or [
                {
                    "nomenclature_ref": "0xSKU1",
                    "nomenclature_code": "SKU-1",
                    "article": "ARTICLE-SKU-1",
                    "name": "Дисплей iPhone 17 Pro Max OLED",
                }
            ]
        ),
        customer_sale_rows=tuple(sales or ()),
        stock_snapshot=stock or _stock(),
        supplier_order_rows=tuple(orders or ()),
        supplier_receipt_rows=tuple(receipts or ()),
    )


def _build(rows: AssortmentSignalSourceRows, registry=None):
    return build_assortment_signal_source_bundle(
        registry or _registry("SKU-1"),
        rows,
        date_from=datetime(2026, 8, 1, tzinfo=UTC),
        source_as_of=datetime(2026, 8, 17, 11, 0, tzinfo=UTC),
        extracted_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )


def test_source_bundle_emits_all_five_types_with_actual_extraction_availability() -> None:
    rows = _rows(
        sales=[
            {
                "nomenclature_code": "SKU-1",
                "occurred_at": datetime(2026, 8, 16, 9, 0),
                "document_ref": "0xSALE",
                "sales_point_ref": "0xPOINT",
                "quantity": Decimal("2"),
            }
        ],
        orders=[
            {
                "nomenclature_code": "SKU-1",
                "occurred_at": datetime(2026, 8, 10, 10, 0),
                "cargo_handoff_at": datetime(2026, 8, 14, 10, 0),
                "supplier_order_ref": "0xORDER",
                "line_number": 1,
                "quantity": Decimal("5"),
            }
        ],
        receipts=[
            {
                "nomenclature_code": "SKU-1",
                "occurred_at": datetime(2026, 8, 16, 10, 0),
                "receipt_ref": "0xRECEIPT",
                "supplier_order_ref": "0xORDER",
                "line_number": 1,
                "quantity": Decimal("4"),
            }
        ],
    )

    bundle = _build(rows)

    assert bundle["schema"] == "assortment_signal_source_bundle.v1"
    assert {item["signal_type"] for item in bundle["items"]} == {
        "customer_sale",
        "stock_availability",
        "supplier_order",
        "supplier_receipt",
        "cargo",
    }
    assert {item["available_at"] for item in bundle["items"]} == {"2026-08-17T12:00:00+00:00"}
    assert {item["reliability"] for item in bundle["items"]} == {"1"}
    assert bundle["source_window"]["available_at_policy"] == (
        "first_snapshot_extraction_completed_at"
    )
    assert bundle["family_registry_snapshot"]["version_number"] == 7
    assert bundle["data_quality"]["status"] == "ready"
    assert bundle["data_quality"]["checks"] == {
        "all_source_row_and_quantity_balances_hold": True,
        "no_conflicting_source_identities": True,
        "available_at_is_actual_extraction_time": True,
        "raw_1c_references_exported": False,
        "external_writes_performed": False,
        "persistence_performed": False,
    }

    serialized = json.dumps(bundle, ensure_ascii=False)
    for raw_reference in ("0xSALE", "0xPOINT", "0xORDER", "0xRECEIPT", "0xSKU1"):
        assert raw_reference not in serialized


def test_scope_and_registry_are_applied_before_events_and_bitok_never_emits() -> None:
    registry = _registry("SKU-1", "BITOK")
    rows = _rows(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xSKU1",
                "nomenclature_code": "SKU-1",
                "article": "ARTICLE-SKU-1",
                "name": "Дисплей iPhone 17 Pro Max",
            },
            {
                "nomenclature_ref": "0xBITOK",
                "nomenclature_code": "BITOK",
                "article": "ARTICLE-BITOK",
                "name": "Дисплей iPhone 13 (биток)",
            },
        ],
        sales=[
            {
                "nomenclature_code": "BITOK",
                "occurred_at": datetime(2026, 8, 16, 9, 0),
                "document_ref": "0xSALE",
                "sales_point_ref": "0xPOINT",
                "quantity": "2",
            }
        ],
        stock=_stock({"SKU-1": Decimal("1"), "BITOK": Decimal("9")}),
    )

    bundle = _build(rows, registry)

    assert {item["nomenclature_code"] for item in bundle["items"]} == {"SKU-1"}
    assert bundle["data_quality"]["display_scope"]["excluded_reason_counts"] == {
        "excluded_display_name_bitok": 1
    }
    sale_profile = bundle["data_quality"]["source_profiles"]["customer_sale"]
    assert sale_profile["exclusion_reason_counts"] == {"sku_not_in_scoped_registry_cohort": 1}
    assert bundle["data_quality"]["family_registry_coverage"]["missing_nomenclature_codes"] == [
        "BITOK"
    ]


def test_cargo_requires_confirmed_nonempty_date_and_future_events_are_excluded() -> None:
    rows = _rows(
        orders=[
            {
                "nomenclature_code": "SKU-1",
                "occurred_at": datetime(2026, 8, 10, 10, 0),
                "cargo_handoff_at": None,
                "supplier_order_ref": "0xORDER-1",
                "line_number": 1,
                "quantity": "2",
            },
            {
                "nomenclature_code": "SKU-1",
                "occurred_at": datetime(2026, 8, 10, 10, 0),
                "cargo_handoff_at": datetime(2026, 8, 18, 10, 0),
                "supplier_order_ref": "0xORDER-2",
                "line_number": 1,
                "quantity": "3",
            },
        ]
    )

    bundle = _build(rows)

    assert not [item for item in bundle["items"] if item["signal_type"] == "cargo"]
    cargo_profile = bundle["data_quality"]["source_profiles"]["cargo"]
    assert cargo_profile["candidate_row_count"] == 2
    assert cargo_profile["emitted_row_count"] == 0
    assert cargo_profile["exclusion_reason_counts"] == {
        "confirmed_cargo_handoff_missing": 1,
        "future_event_excluded": 1,
    }
    assert all(cargo_profile["equations"].values())


def test_duplicate_source_identity_is_profiled_and_conflicting_content_blocks_quality() -> None:
    first = {
        "nomenclature_code": "SKU-1",
        "occurred_at": datetime(2026, 8, 16, 9, 0),
        "document_ref": "0xSALE",
        "sales_point_ref": "0xPOINT",
        "quantity": "2",
    }
    changed = {**first, "quantity": "3"}

    bundle = _build(_rows(sales=[first, changed]))

    assert bundle["data_quality"]["status"] == "blocked"
    assert bundle["data_quality"]["identity"]["duplicate_identity_count"] == 1
    assert bundle["data_quality"]["identity"]["conflicting_identity_count"] == 1


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def mappings(self):
        return self.rows


class _FakeConnection:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        return _FakeResult()


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def connect(self):
        return _FakeConnection(self.calls)


def test_all_source_queries_are_select_only_and_document_facts_require_posted_unmarked() -> None:
    engine = _FakeEngine()
    date_from = datetime(2026, 8, 1, tzinfo=UTC)
    as_of = datetime(2026, 8, 17, tzinfo=UTC)
    order_mapping = DocumentLineMapping(
        document_table="_Document133",
        line_table="_Document133_VT2515",
        line_document_column="_Document133_IDRRef",
        line_nomenclature_column="_Fld2523RRef",
        line_number_column="_LineNo2516",
        line_quantity_column="_Fld2520",
        cargo_handoff_column="_Fld8852",
    )
    receipt_mapping = DocumentLineMapping(
        document_table="_Document194",
        line_table="_Document194_VT4507",
        line_document_column="_Document194_IDRRef",
        line_nomenclature_column="_Fld4509RRef",
        line_number_column="_LineNo4508",
        line_quantity_column="_Fld4514",
        line_supplier_order_column="_Fld4525RRef",
    )

    fetch_registry_nomenclature_rows(engine, nomenclature_codes=["SKU-1"])
    fetch_customer_sale_signal_rows(
        engine,
        nomenclature_codes=["SKU-1"],
        date_from=date_from,
        as_of=as_of,
    )
    fetch_supplier_order_signal_rows(
        engine,
        mapping=order_mapping,
        allowed_refs=["0xSKU"],
        date_from=date_from,
        as_of=as_of,
    )
    fetch_supplier_receipt_signal_rows(
        engine,
        mapping=receipt_mapping,
        allowed_refs=["0xSKU"],
        date_from=date_from,
        as_of=as_of,
    )

    assert len(engine.calls) == 4
    normalized_sql = [" ".join(sql.casefold().split()) for sql, _params in engine.calls]
    assert all(sql.startswith("select") for sql in normalized_sql)
    assert all(
        token not in f" {sql} "
        for sql in normalized_sql
        for token in (" insert ", " update ", " delete ", " merge ")
    )
    for sql in normalized_sql[1:]:
        assert "_marked = 0x00" in sql
        assert "_posted = 0x01" in sql
