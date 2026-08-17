"""Read-only 1C adapters for the first assortment signal ingestion wave.

The module deliberately stops at a portable source bundle.  It does not open an
application write session, persist signals, create orders, or call external
systems.  Raw 1C references are used only to derive stable anonymous identities
and never leave the adapter output.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from app.services.assortment_lifecycle_facts import (
    DocumentLineMapping,
    validate_document_line_mapping,
)
from app.services.assortment_lifecycle_signal_ingestion import (
    SIGNAL_INGESTION_SOURCE_SCHEMA,
    AssortmentLifecycleSignalIngestionError,
    DisplayFamilyRegistryMemberSnapshot,
    DisplayFamilyRegistrySnapshot,
    display_family_registry_snapshot_as_mapping,
)
from app.services.display_scope_policy import filter_display_scope_records
from app.services.onec_stock_availability import (
    CurrentStockSnapshot,
    fetch_current_stock_snapshot,
)

BUSINESS_TIMEZONE = ZoneInfo("Europe/Moscow")
ONEC_SIGNAL_SOURCE = "onec_ut103_read_only"
ONEC_EMPTY_DATE = date(1753, 1, 1)
MAX_SQLSERVER_EXPANDING_VALUES = 1800


class AssortmentLifecycleSignalSourceError(ValueError):
    """A source snapshot cannot be represented safely or reproducibly."""


@dataclass(frozen=True)
class AssortmentSignalSourceRows:
    """Raw rows held in memory only until the normalized bundle is built."""

    nomenclature_rows: tuple[Mapping[str, Any], ...]
    customer_sale_rows: tuple[Mapping[str, Any], ...]
    stock_snapshot: CurrentStockSnapshot
    supplier_order_rows: tuple[Mapping[str, Any], ...]
    supplier_receipt_rows: tuple[Mapping[str, Any], ...]


@dataclass
class _SourceProfile:
    candidate_row_count: int = 0
    input_numeric_quantity: Decimal = Decimal("0")
    emitted_row_count: int = 0
    emitted_quantity: Decimal = Decimal("0")
    exclusion_reason_counts: Counter[str] = field(default_factory=Counter)
    excluded_numeric_quantity: Decimal = Decimal("0")
    occurred_at_values: list[datetime] = field(default_factory=list)

    def candidate(self, quantity: object | None) -> Decimal | None:
        self.candidate_row_count += 1
        parsed = _decimal(quantity)
        if parsed is not None:
            self.input_numeric_quantity += parsed
        return parsed

    def exclude(self, reason: str, quantity: Decimal | None) -> None:
        self.exclusion_reason_counts[reason] += 1
        if quantity is not None:
            self.excluded_numeric_quantity += quantity

    def emit(self, quantity: Decimal, occurred_at: datetime) -> None:
        self.emitted_row_count += 1
        self.emitted_quantity += quantity
        self.occurred_at_values.append(occurred_at)

    def as_mapping(self, *, extracted_at: datetime) -> dict[str, Any]:
        excluded_row_count = sum(self.exclusion_reason_counts.values())
        row_balance = self.candidate_row_count == self.emitted_row_count + excluded_row_count
        quantity_balance = self.input_numeric_quantity == (
            self.emitted_quantity + self.excluded_numeric_quantity
        )
        lags = [
            max(Decimal("0"), Decimal(str((extracted_at - value).total_seconds())))
            for value in self.occurred_at_values
        ]
        return {
            "candidate_row_count": self.candidate_row_count,
            "emitted_row_count": self.emitted_row_count,
            "excluded_row_count": excluded_row_count,
            "exclusion_reason_counts": dict(sorted(self.exclusion_reason_counts.items())),
            "input_numeric_quantity": _decimal_text(self.input_numeric_quantity),
            "emitted_quantity": _decimal_text(self.emitted_quantity),
            "excluded_numeric_quantity": _decimal_text(self.excluded_numeric_quantity),
            "earliest_occurred_at": (
                min(self.occurred_at_values).isoformat() if self.occurred_at_values else None
            ),
            "latest_occurred_at": (
                max(self.occurred_at_values).isoformat() if self.occurred_at_values else None
            ),
            "maximum_first_snapshot_availability_lag_seconds": (
                _decimal_text(max(lags)) if lags else None
            ),
            "equations": {
                "candidate_rows_equal_emitted_plus_excluded": row_balance,
                "input_numeric_quantity_equals_emitted_plus_excluded": quantity_balance,
            },
        }


def load_document_line_mapping(path: Path, *, error_code: str) -> DocumentLineMapping:
    """Load the canonical checked-in mapping without accepting SQL identifiers ad hoc."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssortmentLifecycleSignalSourceError(f"{error_code}:mapping_read_failed") from exc
    if not isinstance(payload, Mapping):
        raise AssortmentLifecycleSignalSourceError(f"{error_code}:mapping_must_be_object")
    try:
        return DocumentLineMapping.from_mapping(payload)
    except ValueError as exc:
        raise AssortmentLifecycleSignalSourceError(f"{error_code}:{exc}") from exc


def fetch_registry_nomenclature_rows(
    engine: Engine,
    *,
    nomenclature_codes: Sequence[str],
) -> list[dict[str, Any]]:
    """Resolve active-registry canonical codes to current, unmarked 1C cards.

    ``_Code`` is the indexed operational key.  Searching the free-form article
    field requires a full 1C scan and made the first live preflight exceed ten
    minutes.  Registry members without a canonical 1C code are therefore
    reported as coverage gaps instead of triggering an unbounded fallback scan.
    """

    normalized = sorted({_clean(value) for value in nomenclature_codes if _clean(value)})
    if not normalized:
        return []
    query = text("""
        SELECT
            CONVERT(varchar(34), item._IDRRef, 1) AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(item._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld836 AS nvarchar(max)))), N'') AS article,
            NULLIF(LTRIM(RTRIM(item._Description)), N'') AS name
        FROM dbo._Reference62 AS item WITH (NOLOCK)
        WHERE item._Marked = 0x00
          AND NULLIF(LTRIM(RTRIM(item._Code)), N'') IN :codes
        ORDER BY item._Code
        """).bindparams(bindparam("codes", expanding=True))
    rows = _fetch_expanding(engine, query, "codes", normalized)
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        ref = _clean(row.get("nomenclature_ref"))
        if ref:
            unique.setdefault(ref, row)
    return sorted(
        unique.values(),
        key=lambda row: (_clean(row.get("nomenclature_code")), _clean(row.get("nomenclature_ref"))),
    )


def fetch_customer_sale_signal_rows(
    engine: Engine,
    *,
    nomenclature_codes: Sequence[str],
    date_from: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Read posted customer sales at document/SKU/sales-point grain."""

    codes = sorted({_clean(value) for value in nomenclature_codes if _clean(value)})
    if not codes:
        return []
    query = text("""
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
            doc._Date_Time AS occurred_at,
            CONVERT(varchar(34), doc._IDRRef, 1) AS document_ref,
            CONVERT(varchar(34),
                CASE
                  WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
                  THEN line._Fld4983RRef ELSE doc._Fld4940RRef
                END, 1) AS sales_point_ref,
            SUM(CAST(line._Fld4971 AS decimal(28, 3))) AS quantity
        FROM dbo._Document203 AS doc WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS line WITH (NOLOCK)
          ON line._Document203_IDRRef = doc._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = line._Fld4974RRef
        WHERE doc._Marked = 0x00
          AND doc._Posted = 0x01
          AND line._Fld4971 > 0
          AND doc._Date_Time >= :date_from
          AND doc._Date_Time <= :as_of
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY product._Code, doc._Date_Time, doc._IDRRef,
          CASE
            WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
            THEN line._Fld4983RRef ELSE doc._Fld4940RRef
          END
        ORDER BY doc._Date_Time, product._Code, doc._IDRRef
        """).bindparams(bindparam("codes", expanding=True))
    return _fetch_expanding(
        engine,
        query,
        "codes",
        codes,
        params={
            "date_from": _onec_naive_datetime(date_from),
            "as_of": _onec_naive_datetime(as_of),
        },
    )


def fetch_supplier_order_signal_rows(
    engine: Engine,
    *,
    mapping: DocumentLineMapping,
    allowed_refs: Sequence[str],
    date_from: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Read posted supplier-order lines and their confirmed cargo field."""

    refs = sorted({_clean(value) for value in allowed_refs if _clean(value)})
    if not refs:
        return []
    if not mapping.line_number_column or not mapping.line_quantity_column:
        raise AssortmentLifecycleSignalSourceError(
            "supplier_order_line_identity_or_quantity_missing"
        )
    cargo_select = (
        f"doc.{_ident(mapping.cargo_handoff_column)}" if mapping.cargo_handoff_column else "NULL"
    )
    query = text(f"""
        SELECT
            CONVERT(varchar(34), line.{_ident(mapping.line_nomenclature_column)}, 1)
                AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
            CONVERT(varchar(34), doc.{_ident(mapping.document_id_column)}, 1)
                AS supplier_order_ref,
            doc.{_ident(mapping.document_date_column)} AS occurred_at,
            {cargo_select} AS cargo_handoff_at,
            line.{_ident(mapping.line_number_column)} AS line_number,
            CAST(line.{_ident(mapping.line_quantity_column)} AS decimal(28, 3)) AS quantity
        FROM dbo.{_ident(mapping.line_table)} AS line WITH (NOLOCK)
        JOIN dbo.{_ident(mapping.document_table)} AS doc WITH (NOLOCK)
          ON doc.{_ident(mapping.document_id_column)}
            = line.{_ident(mapping.line_document_column)}
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = line.{_ident(mapping.line_nomenclature_column)}
        WHERE doc.{_ident(mapping.marked_column)} = 0x00
          AND doc.{_ident(mapping.posted_column)} = 0x01
          AND doc.{_ident(mapping.document_date_column)} >= :date_from
          AND doc.{_ident(mapping.document_date_column)} <= :as_of
          AND CONVERT(varchar(34), line.{_ident(mapping.line_nomenclature_column)}, 1)
            IN :refs
        ORDER BY doc.{_ident(mapping.document_date_column)},
          doc.{_ident(mapping.document_id_column)}, line.{_ident(mapping.line_number_column)}
        """).bindparams(bindparam("refs", expanding=True))
    return _fetch_expanding(
        engine,
        query,
        "refs",
        refs,
        params={
            "date_from": _onec_naive_datetime(date_from),
            "as_of": _onec_naive_datetime(as_of),
        },
    )


def fetch_supplier_receipt_signal_rows(
    engine: Engine,
    *,
    mapping: DocumentLineMapping,
    allowed_refs: Sequence[str],
    date_from: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Read posted supplier-receipt lines with an optional order linkage."""

    refs = sorted({_clean(value) for value in allowed_refs if _clean(value)})
    if not refs:
        return []
    if not mapping.line_number_column or not mapping.line_quantity_column:
        raise AssortmentLifecycleSignalSourceError(
            "supplier_receipt_line_identity_or_quantity_missing"
        )
    supplier_order_select = (
        "CONVERT(varchar(34), " f"line.{_ident(mapping.line_supplier_order_column)}, 1)"
        if mapping.line_supplier_order_column
        else "CAST('' AS varchar(34))"
    )
    query = text(f"""
        SELECT
            CONVERT(varchar(34), line.{_ident(mapping.line_nomenclature_column)}, 1)
                AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
            CONVERT(varchar(34), doc.{_ident(mapping.document_id_column)}, 1) AS receipt_ref,
            {supplier_order_select} AS supplier_order_ref,
            doc.{_ident(mapping.document_date_column)} AS occurred_at,
            line.{_ident(mapping.line_number_column)} AS line_number,
            CAST(line.{_ident(mapping.line_quantity_column)} AS decimal(28, 3)) AS quantity
        FROM dbo.{_ident(mapping.line_table)} AS line WITH (NOLOCK)
        JOIN dbo.{_ident(mapping.document_table)} AS doc WITH (NOLOCK)
          ON doc.{_ident(mapping.document_id_column)}
            = line.{_ident(mapping.line_document_column)}
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = line.{_ident(mapping.line_nomenclature_column)}
        WHERE doc.{_ident(mapping.marked_column)} = 0x00
          AND doc.{_ident(mapping.posted_column)} = 0x01
          AND doc.{_ident(mapping.document_date_column)} >= :date_from
          AND doc.{_ident(mapping.document_date_column)} <= :as_of
          AND CONVERT(varchar(34), line.{_ident(mapping.line_nomenclature_column)}, 1)
            IN :refs
        ORDER BY doc.{_ident(mapping.document_date_column)},
          doc.{_ident(mapping.document_id_column)}, line.{_ident(mapping.line_number_column)}
        """).bindparams(bindparam("refs", expanding=True))
    return _fetch_expanding(
        engine,
        query,
        "refs",
        refs,
        params={
            "date_from": _onec_naive_datetime(date_from),
            "as_of": _onec_naive_datetime(as_of),
        },
    )


def fetch_assortment_signal_source_rows(
    engine: Engine,
    registry_snapshot: DisplayFamilyRegistrySnapshot,
    *,
    date_from: datetime,
    as_of: datetime,
    supplier_order_mapping: DocumentLineMapping,
    supplier_receipt_mapping: DocumentLineMapping,
) -> AssortmentSignalSourceRows:
    """Fetch the five sources with registry and scope gates before fact queries."""

    _validate_window(date_from, as_of)
    nomenclature_codes = sorted({member.nomenclature_code for member in registry_snapshot.members})
    nomenclature_rows = fetch_registry_nomenclature_rows(
        engine,
        nomenclature_codes=nomenclature_codes,
    )
    scope_result = filter_display_scope_records(nomenclature_rows)
    scoped_rows = [dict(row) for row in scope_result.included]
    allowed_codes = sorted(
        {
            _clean(row.get("nomenclature_code"))
            for row in scoped_rows
            if _clean(row.get("nomenclature_code"))
        }
    )
    allowed_refs = sorted(
        {
            _clean(row.get("nomenclature_ref"))
            for row in scoped_rows
            if _clean(row.get("nomenclature_ref"))
        }
    )

    supplier_issues = validate_document_line_mapping(engine, supplier_order_mapping)
    if supplier_issues:
        raise AssortmentLifecycleSignalSourceError(
            "supplier_order_mapping_unresolved:" + ",".join(supplier_issues)
        )
    receipt_issues = validate_document_line_mapping(engine, supplier_receipt_mapping)
    if receipt_issues:
        raise AssortmentLifecycleSignalSourceError(
            "supplier_receipt_mapping_unresolved:" + ",".join(receipt_issues)
        )

    return AssortmentSignalSourceRows(
        nomenclature_rows=tuple(nomenclature_rows),
        customer_sale_rows=tuple(
            fetch_customer_sale_signal_rows(
                engine,
                nomenclature_codes=allowed_codes,
                date_from=date_from,
                as_of=as_of,
            )
        ),
        stock_snapshot=fetch_current_stock_snapshot(engine),
        supplier_order_rows=tuple(
            fetch_supplier_order_signal_rows(
                engine,
                mapping=supplier_order_mapping,
                allowed_refs=allowed_refs,
                date_from=date_from,
                as_of=as_of,
            )
        ),
        supplier_receipt_rows=tuple(
            fetch_supplier_receipt_signal_rows(
                engine,
                mapping=supplier_receipt_mapping,
                allowed_refs=allowed_refs,
                date_from=date_from,
                as_of=as_of,
            )
        ),
    )


def extract_assortment_signal_source_bundle(
    engine: Engine,
    registry_snapshot: DisplayFamilyRegistrySnapshot,
    *,
    date_from: datetime,
    as_of: datetime,
    supplier_order_mapping: DocumentLineMapping,
    supplier_receipt_mapping: DocumentLineMapping,
    extracted_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch and normalize one read-only first-wave snapshot."""

    rows = fetch_assortment_signal_source_rows(
        engine,
        registry_snapshot,
        date_from=date_from,
        as_of=as_of,
        supplier_order_mapping=supplier_order_mapping,
        supplier_receipt_mapping=supplier_receipt_mapping,
    )
    effective_extracted_at = _utc(extracted_at or datetime.now(UTC), "extracted_at")
    return build_assortment_signal_source_bundle(
        registry_snapshot,
        rows,
        date_from=date_from,
        source_as_of=as_of,
        extracted_at=effective_extracted_at,
    )


def build_assortment_signal_source_bundle(
    registry_snapshot: DisplayFamilyRegistrySnapshot,
    rows: AssortmentSignalSourceRows,
    *,
    date_from: datetime,
    source_as_of: datetime,
    extracted_at: datetime,
) -> dict[str, Any]:
    """Normalize raw rows and attach a compact, balanced data-quality profile."""

    date_from_utc, source_as_of_utc = _validate_window(date_from, source_as_of)
    extracted_at_utc = _utc(extracted_at, "extracted_at")
    if source_as_of_utc > extracted_at_utc:
        raise AssortmentLifecycleSignalSourceError("as_of_must_not_be_after_extraction")

    registry_aliases = _registry_alias_index(registry_snapshot)
    scope_result = filter_display_scope_records(list(rows.nomenclature_rows))
    metadata_by_code: dict[str, dict[str, Any]] = {}
    matched_product_ids: set[int] = set()
    registry_resolution_counts: Counter[str] = Counter()
    for raw in scope_result.included:
        code = _clean(raw.get("nomenclature_code"))
        article = _clean(raw.get("article"))
        members = _resolve_members((code, article), registry_aliases)
        if len(members) != 1:
            registry_resolution_counts[
                "not_in_active_registry" if not members else "ambiguous_active_registry_alias"
            ] += 1
            continue
        member = members[0]
        matched_product_ids.add(member.product_id)
        if not code:
            registry_resolution_counts["missing_1c_code"] += 1
            continue
        key = code.casefold()
        if key in metadata_by_code:
            registry_resolution_counts["duplicate_1c_code"] += 1
            continue
        metadata_by_code[key] = {
            "code": code,
            "article": article,
            "name": _clean(raw.get("name")) or member.name,
            "member": member,
        }

    profiles = {
        signal_type: _SourceProfile()
        for signal_type in (
            "customer_sale",
            "stock_availability",
            "supplier_order",
            "supplier_receipt",
            "cargo",
        )
    }
    items: list[dict[str, Any]] = []

    def metadata_for(raw: Mapping[str, Any]) -> dict[str, Any] | None:
        return metadata_by_code.get(_clean(raw.get("nomenclature_code")).casefold())

    for raw in rows.customer_sale_rows:
        profile = profiles["customer_sale"]
        quantity = profile.candidate(raw.get("quantity"))
        meta = metadata_for(raw)
        occurred_at = _source_datetime(raw.get("occurred_at"))
        document_ref = _clean(raw.get("document_ref"))
        sales_point_ref = _clean(raw.get("sales_point_ref"))
        reason = _event_exclusion_reason(
            meta=meta,
            quantity=quantity,
            occurred_at=occurred_at,
            source_as_of=source_as_of_utc,
            identity_present=bool(document_ref),
        )
        if reason:
            profile.exclude(reason, quantity)
            continue
        assert meta is not None and quantity is not None and occurred_at is not None
        document_id = _anonymous_id("sale-document", document_ref)
        sales_point_id = _anonymous_id("sales-point", sales_point_ref)
        items.append(
            _source_item(
                signal_type="customer_sale",
                source_event_id=_anonymous_id(
                    "customer-sale",
                    document_ref,
                    meta["code"],
                    sales_point_ref,
                ),
                occurred_at=occurred_at,
                available_at=extracted_at_utc,
                quantity=quantity,
                meta=meta,
                reliability_reason="onec_posted_customer_sale",
                payload={
                    "source_grain": "document_sku_sales_point",
                    "document_id": document_id,
                    "sales_point_id": sales_point_id,
                },
            )
        )
        profile.emit(quantity, occurred_at)

    stock_captured_at = _utc(rows.stock_snapshot.captured_at, "stock_captured_at")
    for meta in sorted(metadata_by_code.values(), key=lambda value: value["code"]):
        profile = profiles["stock_availability"]
        quantity = profile.candidate(rows.stock_snapshot.quantities_by_code.get(meta["code"], 0))
        reason = _event_exclusion_reason(
            meta=meta,
            quantity=quantity,
            occurred_at=stock_captured_at,
            source_as_of=extracted_at_utc,
            identity_present=True,
        )
        if reason:
            profile.exclude(reason, quantity)
            continue
        assert quantity is not None
        net_quantity = _decimal(rows.stock_snapshot.net_quantities_by_code.get(meta["code"], 0))
        items.append(
            _source_item(
                signal_type="stock_availability",
                source_event_id=_anonymous_id(
                    "stock-availability",
                    stock_captured_at.isoformat(),
                    meta["code"],
                ),
                occurred_at=stock_captured_at,
                available_at=extracted_at_utc,
                quantity=quantity,
                meta=meta,
                reliability_reason="onec_current_totals_snapshot",
                payload={
                    "source_grain": "capture_sku",
                    "source_period": rows.stock_snapshot.source_period.isoformat(),
                    "net_quantity": _decimal_text(net_quantity or Decimal("0")),
                    "source_row_present": meta["code"] in rows.stock_snapshot.quantities_by_code,
                },
            )
        )
        profile.emit(quantity, stock_captured_at)

    for raw in rows.supplier_order_rows:
        profile = profiles["supplier_order"]
        quantity = profile.candidate(raw.get("quantity"))
        meta = metadata_for(raw)
        occurred_at = _source_datetime(raw.get("occurred_at"))
        document_ref = _clean(raw.get("supplier_order_ref"))
        line_number = _clean(raw.get("line_number"))
        reason = _event_exclusion_reason(
            meta=meta,
            quantity=quantity,
            occurred_at=occurred_at,
            source_as_of=source_as_of_utc,
            identity_present=bool(document_ref and line_number),
        )
        if reason:
            profile.exclude(reason, quantity)
            continue
        assert meta is not None and quantity is not None and occurred_at is not None
        order_id = _anonymous_id("supplier-order", document_ref)
        items.append(
            _source_item(
                signal_type="supplier_order",
                source_event_id=_anonymous_id(
                    "supplier-order-line",
                    document_ref,
                    line_number,
                ),
                occurred_at=occurred_at,
                available_at=extracted_at_utc,
                quantity=quantity,
                meta=meta,
                reliability_reason="onec_posted_supplier_order",
                payload={
                    "source_grain": "supplier_order_line",
                    "supplier_order_id": order_id,
                    "line_number": line_number,
                },
            )
        )
        profile.emit(quantity, occurred_at)

    for raw in rows.supplier_receipt_rows:
        profile = profiles["supplier_receipt"]
        quantity = profile.candidate(raw.get("quantity"))
        meta = metadata_for(raw)
        occurred_at = _source_datetime(raw.get("occurred_at"))
        receipt_ref = _clean(raw.get("receipt_ref"))
        line_number = _clean(raw.get("line_number"))
        reason = _event_exclusion_reason(
            meta=meta,
            quantity=quantity,
            occurred_at=occurred_at,
            source_as_of=source_as_of_utc,
            identity_present=bool(receipt_ref and line_number),
        )
        if reason:
            profile.exclude(reason, quantity)
            continue
        assert meta is not None and quantity is not None and occurred_at is not None
        supplier_order_ref = _clean(raw.get("supplier_order_ref"))
        payload: dict[str, Any] = {
            "source_grain": "supplier_receipt_line",
            "receipt_id": _anonymous_id("supplier-receipt", receipt_ref),
            "line_number": line_number,
        }
        if supplier_order_ref:
            payload["supplier_order_id"] = _anonymous_id("supplier-order", supplier_order_ref)
        items.append(
            _source_item(
                signal_type="supplier_receipt",
                source_event_id=_anonymous_id(
                    "supplier-receipt-line",
                    receipt_ref,
                    line_number,
                ),
                occurred_at=occurred_at,
                available_at=extracted_at_utc,
                quantity=quantity,
                meta=meta,
                reliability_reason="onec_posted_supplier_receipt",
                payload=payload,
            )
        )
        profile.emit(quantity, occurred_at)

    for raw in rows.supplier_order_rows:
        profile = profiles["cargo"]
        quantity = profile.candidate(raw.get("quantity"))
        meta = metadata_for(raw)
        cargo_at = _source_datetime(raw.get("cargo_handoff_at"))
        document_ref = _clean(raw.get("supplier_order_ref"))
        line_number = _clean(raw.get("line_number"))
        if cargo_at is None:
            profile.exclude("confirmed_cargo_handoff_missing", quantity)
            continue
        reason = _event_exclusion_reason(
            meta=meta,
            quantity=quantity,
            occurred_at=cargo_at,
            source_as_of=source_as_of_utc,
            identity_present=bool(document_ref and line_number),
        )
        if reason:
            profile.exclude(reason, quantity)
            continue
        assert meta is not None and quantity is not None
        items.append(
            _source_item(
                signal_type="cargo",
                source_event_id=_anonymous_id(
                    "supplier-order-line",
                    document_ref,
                    line_number,
                ),
                occurred_at=cargo_at,
                available_at=extracted_at_utc,
                quantity=quantity,
                meta=meta,
                reliability_reason=("onec_posted_supplier_order_confirmed_cargo_handoff"),
                payload={
                    "source_grain": "supplier_order_line",
                    "supplier_order_id": _anonymous_id("supplier-order", document_ref),
                    "line_number": line_number,
                    "confirmation_field": "cargo_handoff_at",
                },
            )
        )
        profile.emit(quantity, cargo_at)

    items.sort(
        key=lambda item: (
            item["signal_type"],
            item["occurred_at"],
            item["source_event_id"],
        )
    )
    duplicate_quality = _duplicate_identity_quality(items)
    profile_mappings = {
        signal_type: profile.as_mapping(extracted_at=extracted_at_utc)
        for signal_type, profile in sorted(profiles.items())
    }
    all_row_balances = all(
        all(profile["equations"].values()) for profile in profile_mappings.values()
    )
    missing_members = [
        member.nomenclature_code
        for member in registry_snapshot.members
        if member.product_id not in matched_product_ids
    ]
    issue_count = (
        sum(registry_resolution_counts.values())
        + len(missing_members)
        + duplicate_quality["conflicting_identity_count"]
        + sum(
            sum(
                count
                for reason, count in profile["exclusion_reason_counts"].items()
                if reason != "confirmed_cargo_handoff_missing"
            )
            for profile in profile_mappings.values()
        )
    )
    data_quality_status = "ready"
    if not all_row_balances or duplicate_quality["conflicting_identity_count"]:
        data_quality_status = "blocked"
    elif issue_count:
        data_quality_status = "review_required"

    registry_mapping = display_family_registry_snapshot_as_mapping(registry_snapshot)
    canonical_for_id = {
        "registry_version": registry_snapshot.version_number,
        "date_from": date_from_utc.isoformat(),
        "source_as_of": source_as_of_utc.isoformat(),
        "extracted_at": extracted_at_utc.isoformat(),
        "items": items,
    }
    bundle_id = (
        "source-backed-"
        + hashlib.sha256(
            json.dumps(
                canonical_for_id,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
    )
    return {
        "schema": SIGNAL_INGESTION_SOURCE_SCHEMA,
        "bundle_id": bundle_id,
        "as_of": extracted_at_utc.isoformat(),
        "source_window": {
            "date_from": date_from_utc.isoformat(),
            "as_of": source_as_of_utc.isoformat(),
            "business_timezone": BUSINESS_TIMEZONE.key,
            "extracted_at": extracted_at_utc.isoformat(),
            "available_at_policy": "first_snapshot_extraction_completed_at",
        },
        "family_registry_snapshot": registry_mapping,
        "items": items,
        "data_quality": {
            "status": data_quality_status,
            "intended_grain": {
                "customer_sale": "document_sku_sales_point",
                "stock_availability": "capture_sku",
                "supplier_order": "supplier_order_line",
                "supplier_receipt": "supplier_receipt_line",
                "cargo": "supplier_order_line_with_confirmed_handoff",
            },
            "source_profiles": profile_mappings,
            "family_registry_coverage": {
                "registry_member_count": len(registry_snapshot.members),
                "matched_member_count": len(matched_product_ids),
                "missing_member_count": len(missing_members),
                "missing_nomenclature_codes": sorted(missing_members),
                "resolution_issue_counts": dict(sorted(registry_resolution_counts.items())),
            },
            "display_scope": scope_result.audit,
            "identity": duplicate_quality,
            "checks": {
                "all_source_row_and_quantity_balances_hold": all_row_balances,
                "no_conflicting_source_identities": (
                    duplicate_quality["conflicting_identity_count"] == 0
                ),
                "available_at_is_actual_extraction_time": all(
                    item["available_at"] == extracted_at_utc.isoformat() for item in items
                ),
                "raw_1c_references_exported": False,
                "external_writes_performed": False,
                "persistence_performed": False,
            },
        },
    }


def _source_item(
    *,
    signal_type: str,
    source_event_id: str,
    occurred_at: datetime,
    available_at: datetime,
    quantity: Decimal,
    meta: Mapping[str, Any],
    reliability_reason: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    item = {
        "signal_type": signal_type,
        "source": ONEC_SIGNAL_SOURCE,
        "source_event_id": source_event_id,
        "occurred_at": occurred_at.isoformat(),
        "available_at": available_at.isoformat(),
        "reliability": "1",
        "reliability_reason": reliability_reason,
        "nomenclature_code": meta["code"],
        "name": meta["name"],
        "quantity": _decimal_text(quantity),
        "payload": dict(payload),
    }
    if meta.get("article"):
        item["article"] = meta["article"]
    return item


def _event_exclusion_reason(
    *,
    meta: Mapping[str, Any] | None,
    quantity: Decimal | None,
    occurred_at: datetime | None,
    source_as_of: datetime,
    identity_present: bool,
) -> str | None:
    if meta is None:
        return "sku_not_in_scoped_registry_cohort"
    if not _clean(meta.get("name")):
        return "display_name_missing"
    if quantity is None:
        return "quantity_invalid_or_missing"
    if quantity < 0:
        return "quantity_negative"
    if occurred_at is None:
        return "occurred_at_invalid_or_missing"
    if occurred_at > source_as_of:
        return "future_event_excluded"
    if not identity_present:
        return "source_identity_missing"
    return None


def _duplicate_identity_quality(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_identity: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for item in items:
        identity = (
            _clean(item.get("signal_type")),
            _clean(item.get("source")),
            _clean(item.get("source_event_id")),
        )
        content_hash = hashlib.sha256(
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        by_identity[identity].append(content_hash)
    duplicate_identity_count = 0
    exact_duplicate_row_count = 0
    conflicting_identity_count = 0
    for hashes in by_identity.values():
        if len(hashes) < 2:
            continue
        duplicate_identity_count += 1
        if len(set(hashes)) == 1:
            exact_duplicate_row_count += len(hashes) - 1
        else:
            conflicting_identity_count += 1
    return {
        "identity_count": len(by_identity),
        "duplicate_identity_count": duplicate_identity_count,
        "exact_duplicate_row_count": exact_duplicate_row_count,
        "conflicting_identity_count": conflicting_identity_count,
    }


def _registry_alias_index(
    snapshot: DisplayFamilyRegistrySnapshot,
) -> dict[str, tuple[DisplayFamilyRegistryMemberSnapshot, ...]]:
    mutable: dict[str, dict[int, DisplayFamilyRegistryMemberSnapshot]] = defaultdict(dict)
    for member in snapshot.members:
        for alias in member.aliases:
            normalized = _clean(alias).casefold()
            if normalized:
                mutable[normalized][member.product_id] = member
    return {
        alias: tuple(products[key] for key in sorted(products))
        for alias, products in mutable.items()
    }


def _resolve_members(
    aliases: Sequence[str],
    index: Mapping[str, tuple[DisplayFamilyRegistryMemberSnapshot, ...]],
) -> tuple[DisplayFamilyRegistryMemberSnapshot, ...]:
    matched: dict[int, DisplayFamilyRegistryMemberSnapshot] = {}
    for alias in aliases:
        for member in index.get(_clean(alias).casefold(), ()):  # pragma: no branch
            matched[member.product_id] = member
    return tuple(matched[key] for key in sorted(matched))


def _validate_window(date_from: datetime, as_of: datetime) -> tuple[datetime, datetime]:
    date_from_utc = _utc(date_from, "date_from")
    as_of_utc = _utc(as_of, "as_of")
    if date_from_utc > as_of_utc:
        raise AssortmentLifecycleSignalSourceError("date_from_must_not_exceed_as_of")
    return date_from_utc, as_of_utc


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise AssortmentLifecycleSignalSourceError(f"{field_name}_must_be_datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise AssortmentLifecycleSignalSourceError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(UTC)


def _onec_naive_datetime(value: datetime) -> datetime:
    return _utc(value, "source_query_datetime").astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)


def _source_datetime(value: object | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        source = value.strip()
        if not source:
            return None
        if source.endswith("Z"):
            source = source[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(source)
        except ValueError:
            return None
    else:
        return None
    if parsed.date() <= ONEC_EMPTY_DATE:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=BUSINESS_TIMEZONE)
    return parsed.astimezone(UTC)


def _decimal(value: object | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _anonymous_id(namespace: str, *values: object) -> str:
    canonical = json.dumps(
        [namespace, *(_clean(value) for value in values)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clean(value: object | None) -> str:
    return " ".join(str(value or "").strip().split())


def _ident(value: str) -> str:
    if not value or not value.replace("_", "").isalnum():
        raise AssortmentLifecycleSignalSourceError(f"unsafe_sql_identifier:{value}")
    return value


def _fetch_expanding(
    engine: Engine,
    query: Any,
    parameter_name: str,
    values: Sequence[str],
    *,
    params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with engine.connect() as connection:
        for start in range(0, len(values), MAX_SQLSERVER_EXPANDING_VALUES):
            chunk = values[start : start + MAX_SQLSERVER_EXPANDING_VALUES]
            result = connection.execute(
                query,
                {**dict(params or {}), parameter_name: tuple(chunk)},
            )
            rows.extend(dict(row) for row in result.mappings())
    return rows


def source_bundle_embedded_registry(
    source_bundle: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return a portable embedded registry, if this source-backed bundle has one."""

    payload = source_bundle.get("family_registry_snapshot")
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise AssortmentLifecycleSignalIngestionError("family_registry_snapshot_must_be_object")
    return payload
