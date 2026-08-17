from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, text

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_facts import (
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    DocumentLineMapping,
    default_history_start,
    validate_document_line_mapping,
)

DEFAULT_SUPPLIER_ORDER_MAPPING_JSON = Path(
    "config/assortment/display-supplier-order-line-mapping.json"
)
DEFAULT_RECEIPT_MAPPING_JSON = Path("config/assortment/display-receipt-line-mapping.json")
DEFAULT_OUTPUT_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-supplier-lead-time-history.csv"
)
DEFAULT_OUTPUT_DETAIL_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-supplier-lead-time-history-detail.csv"
)
DEFAULT_OUTPUT_JSON = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-supplier-lead-time-history-summary.json"
)
DEFAULT_OUTPUT_SEASONALITY_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-supplier-lead-time-seasonality.csv"
)
DEFAULT_OUTPUT_SEASONALITY_JSON = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-supplier-lead-time-seasonality-summary.json"
)
ONEC_EMPTY_DATE = date(1753, 1, 1)
MAX_SQLSERVER_EXPANDING_REFS = 1800

AGGREGATE_CSV_COLUMNS = [
    "supplier_name",
    "supplier_code",
    "supplier_ref",
    "responsible_name",
    "responsible_code",
    "responsible_ref",
    "responsible_count",
    "nomenclature_code",
    "name",
    "display_group_key",
    "order_line_count",
    "ordered_qty",
    "order_amount",
    "orders_with_cargo_count",
    "orders_with_receipt_after_cargo_count",
    "missing_cargo_count",
    "missing_receipt_after_cargo_count",
    "negative_supplier_prepare_count",
    "supplier_prepare_days_avg",
    "supplier_prepare_days_median",
    "supplier_prepare_days_min",
    "supplier_prepare_days_max",
    "logistics_receiving_days_avg",
    "logistics_receiving_days_median",
    "logistics_receiving_days_min",
    "logistics_receiving_days_max",
    "total_arrival_days_avg",
    "total_arrival_days_median",
    "supplier_prepare_outlier_count",
    "logistics_receiving_outlier_count",
    "latest_supplier_order_at",
    "latest_cargo_handoff_at",
    "latest_receipt_at",
    "recommended_supplier_prepare_days",
    "recommended_logistics_days",
    "lead_time_confidence",
    "notes",
]

DETAIL_CSV_COLUMNS = [
    "supplier_name",
    "supplier_code",
    "supplier_ref",
    "responsible_name",
    "responsible_code",
    "responsible_ref",
    "supplier_order_number",
    "supplier_order_ref",
    "supplier_order_created_at",
    "cargo_handoff_at",
    "expected_receipt_at",
    "warehouse_receipt_at",
    "receipt_number",
    "receipt_ref",
    "nomenclature_code",
    "name",
    "display_group_key",
    "qty",
    "price",
    "amount",
    "supplier_prepare_days",
    "logistics_receiving_days",
    "total_arrival_days",
    "receipt_match_confidence",
    "missing_cargo_handoff_at",
    "missing_receipt_after_cargo",
    "negative_supplier_prepare_days",
    "supplier_prepare_outlier",
    "logistics_receiving_outlier",
    "notes",
]

SEASONALITY_CSV_COLUMNS = [
    "week_start",
    "iso_year",
    "iso_week",
    "month",
    "season_label",
    "order_line_count",
    "ordered_qty",
    "order_amount",
    "orders_with_cargo_count",
    "orders_with_receipt_after_cargo_count",
    "missing_cargo_count",
    "missing_receipt_after_cargo_count",
    "supplier_prepare_days_median",
    "logistics_receiving_days_median",
    "total_arrival_days_median",
    "baseline_supplier_prepare_days",
    "baseline_logistics_days",
    "supplier_prepare_delta_days",
    "logistics_delta_days",
    "prepare_delay_signal",
    "road_seasonality_signal",
    "route_risk_level",
    "top_supplier_name",
    "top_responsible_name",
    "supplier_count",
    "responsible_count",
    "notes",
]


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    onec_database_url = (
        args.onec_database_url
        or os.environ.get("ONEC_DATABASE_URL", "")
        or settings.onec_database_url
        or ""
    )
    if not onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is required")

    history_start = default_history_start(args.as_of, history_months=args.history_months)
    supplier_mapping = _load_document_line_mapping(
        args.supplier_order_mapping_json,
        error_code=SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    )
    receipt_mapping = _load_document_line_mapping(
        args.receipt_mapping_json,
        error_code=RECEIPT_MAPPING_UNRESOLVED,
    )

    engine = build_engine(onec_database_url, pool_pre_ping=True)
    try:
        source_rows = fetch_display_supplier_lead_time_source_rows(
            engine,
            folder=args.folder,
            history_start=history_start,
            as_of=args.as_of,
            supplier_mapping=supplier_mapping,
            receipt_mapping=receipt_mapping,
            limit=args.limit,
        )
    finally:
        engine.dispose()

    detail_rows = build_lead_time_detail_rows(
        source_rows["supplier_order_rows"],
        source_rows["receipt_rows"],
    )
    outlier_thresholds = mark_lead_time_outliers(detail_rows)
    aggregate_rows = aggregate_lead_time_rows(detail_rows)
    seasonality_rows = build_weekly_seasonality_rows(detail_rows)
    seasonality_summary = build_seasonality_summary(
        seasonality_rows,
        history_start=history_start,
        as_of=args.as_of,
    )
    summary = build_summary(
        detail_rows,
        aggregate_rows=aggregate_rows,
        seasonality_rows=seasonality_rows,
        source_counts={key: len(value) for key, value in source_rows.items()},
        history_start=history_start,
        as_of=args.as_of,
        outlier_thresholds=outlier_thresholds,
    )

    write_csv(args.output_csv, aggregate_rows, AGGREGATE_CSV_COLUMNS)
    write_csv(args.output_detail_csv, detail_rows, DETAIL_CSV_COLUMNS)
    write_csv(args.output_seasonality_csv, seasonality_rows, SEASONALITY_CSV_COLUMNS)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.output_seasonality_json:
        args.output_seasonality_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_seasonality_json.write_text(
            json.dumps(seasonality_summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    payload = {
        "status": "ready",
        "output_csv": str(args.output_csv),
        "output_detail_csv": str(args.output_detail_csv),
        "output_json": str(args.output_json) if args.output_json else None,
        "output_seasonality_csv": str(args.output_seasonality_csv),
        "output_seasonality_json": (
            str(args.output_seasonality_json) if args.output_seasonality_json else None
        ),
        "seasonality_summary": seasonality_summary,
        **summary,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def fetch_display_supplier_lead_time_source_rows(
    engine,
    *,
    folder: str,
    history_start: date,
    as_of: date,
    supplier_mapping: DocumentLineMapping,
    receipt_mapping: DocumentLineMapping,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    supplier_issues = validate_document_line_mapping(engine, supplier_mapping)
    if supplier_issues:
        raise ValueError(f"{SUPPLIER_ORDER_MAPPING_UNRESOLVED}: {', '.join(supplier_issues)}")
    receipt_issues = validate_document_line_mapping(engine, receipt_mapping)
    if receipt_issues:
        raise ValueError(f"{RECEIPT_MAPPING_UNRESOLVED}: {', '.join(receipt_issues)}")

    nomenclature_rows = fetch_display_nomenclature_rows(engine, folder=folder, limit=limit)
    refs = {_clean(row.get("nomenclature_ref")) for row in nomenclature_rows}
    refs.discard("")
    if not refs:
        return {
            "nomenclature_rows": [],
            "supplier_order_rows": [],
            "receipt_rows": [],
        }

    return {
        "nomenclature_rows": nomenclature_rows,
        "supplier_order_rows": fetch_supplier_order_line_rows(
            engine,
            supplier_mapping=supplier_mapping,
            allowed_refs=refs,
            history_start=history_start,
            date_to=as_of + timedelta(days=1),
        ),
        "receipt_rows": fetch_receipt_line_rows(
            engine,
            receipt_mapping=receipt_mapping,
            allowed_refs=refs,
            history_start=history_start,
            date_to=as_of + timedelta(days=1),
        ),
    }


def fetch_display_nomenclature_rows(engine, *, folder: str, limit: int) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 50000))
    query = text(f"""
        SELECT TOP {bounded_limit}
            CONVERT(varchar(34), item._IDRRef, 1) AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(item._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld836 AS nvarchar(max)))), N'') AS article,
            NULLIF(LTRIM(RTRIM(item._Description)), N'') AS name,
            parent._Description AS folder_path
        FROM dbo._Reference62 AS item WITH (NOLOCK)
        LEFT JOIN dbo._Reference62 AS parent WITH (NOLOCK)
            ON parent._IDRRef = item._ParentIDRRef
        WHERE item._Marked = 0x00
          AND item._Fld836 IS NOT NULL
          AND (parent._Description LIKE :folder_like OR item._Description LIKE :folder_like)
        ORDER BY item._Code
    """)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(query, {"folder_like": f"%{folder}%"}).mappings()]


def fetch_supplier_order_line_rows(
    engine,
    *,
    supplier_mapping: DocumentLineMapping,
    allowed_refs: set[str],
    history_start: date,
    date_to: date,
) -> list[dict[str, Any]]:
    supplier_ref_column = "_Fld2498RRef"
    responsible_ref_column = "_Fld2504RRef"
    expected_receipt_column = "_Fld2493"
    quantity_column = "_Fld2520"
    amount_column = "_Fld2526"
    line_no_column = "_LineNo2516"
    query = _expanding_text(
        f"""
        SELECT
            CONVERT(varchar(34), line.{_ident(supplier_mapping.line_nomenclature_column)}, 1)
                AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(product._Description)), N'') AS name,
            parent._Description AS folder_path,
            CONVERT(varchar(34), doc.{_ident(supplier_mapping.document_id_column)}, 1)
                AS supplier_order_ref,
            NULLIF(LTRIM(RTRIM(doc._Number)), N'') AS supplier_order_number,
            doc.{_ident(supplier_mapping.document_date_column)} AS supplier_order_created_at,
            doc.{_ident(supplier_mapping.cargo_handoff_column)} AS cargo_handoff_at,
            doc.{_ident(expected_receipt_column)} AS expected_receipt_at,
            CONVERT(varchar(34), doc.{_ident(supplier_ref_column)}, 1) AS supplier_ref,
            NULLIF(LTRIM(RTRIM(supplier._Code)), N'') AS supplier_code,
            NULLIF(LTRIM(RTRIM(supplier._Description)), N'') AS supplier_name,
            CONVERT(varchar(34), doc.{_ident(responsible_ref_column)}, 1) AS responsible_ref,
            NULLIF(LTRIM(RTRIM(responsible._Code)), N'') AS responsible_code,
            NULLIF(LTRIM(RTRIM(responsible._Description)), N'') AS responsible_name,
            line.{_ident(line_no_column)} AS line_no,
            CAST(line.{_ident(quantity_column)} AS decimal(18, 3)) AS qty,
            CAST(line.{_ident(supplier_mapping.line_price_column)} AS decimal(18, 2)) AS price,
            CAST(line.{_ident(amount_column)} AS decimal(18, 2)) AS amount
        FROM dbo.{_ident(supplier_mapping.line_table)} AS line WITH (NOLOCK)
        JOIN dbo.{_ident(supplier_mapping.document_table)} AS doc WITH (NOLOCK)
            ON doc.{_ident(supplier_mapping.document_id_column)}
                = line.{_ident(supplier_mapping.line_document_column)}
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = line.{_ident(supplier_mapping.line_nomenclature_column)}
        LEFT JOIN dbo._Reference62 AS parent WITH (NOLOCK)
            ON parent._IDRRef = product._ParentIDRRef
        LEFT JOIN dbo._Reference54 AS supplier WITH (NOLOCK)
            ON supplier._IDRRef = doc.{_ident(supplier_ref_column)}
        LEFT JOIN dbo._Reference69 AS responsible WITH (NOLOCK)
            ON responsible._IDRRef = doc.{_ident(responsible_ref_column)}
        WHERE doc.{_ident(supplier_mapping.marked_column)} = 0x00
          AND doc.{_ident(supplier_mapping.posted_column)} = 0x01
          AND doc.{_ident(supplier_mapping.document_date_column)} >= :history_start
          AND doc.{_ident(supplier_mapping.document_date_column)} < :date_to
          AND line.{_ident(quantity_column)} > 0
          AND CONVERT(varchar(34), line.{_ident(supplier_mapping.line_nomenclature_column)}, 1)
                IN :refs
        ORDER BY doc.{_ident(supplier_mapping.document_date_column)}, doc._Number, line.{line_no_column}
        """,
        refs=sorted(allowed_refs),
    ).bindparams(
        bindparam("history_start", value=datetime.combine(history_start, time.min)),
        bindparam("date_to", value=datetime.combine(date_to, time.min)),
    )
    return _fetch_chunked(engine, query, sorted(allowed_refs))


def fetch_receipt_line_rows(
    engine,
    *,
    receipt_mapping: DocumentLineMapping,
    allowed_refs: set[str],
    history_start: date,
    date_to: date,
) -> list[dict[str, Any]]:
    line_no_column = "_LineNo4508"
    quantity_column = "_Fld4513"
    query = _expanding_text(
        f"""
        SELECT
            CONVERT(varchar(34), line.{_ident(receipt_mapping.line_nomenclature_column)}, 1)
                AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
            CONVERT(varchar(34), doc.{_ident(receipt_mapping.document_id_column)}, 1)
                AS receipt_ref,
            NULLIF(LTRIM(RTRIM(doc._Number)), N'') AS receipt_number,
            doc.{_ident(receipt_mapping.document_date_column)} AS receipt_at,
            line.{_ident(line_no_column)} AS line_no,
            CAST(line.{_ident(quantity_column)} AS decimal(18, 3)) AS receipt_qty
        FROM dbo.{_ident(receipt_mapping.line_table)} AS line WITH (NOLOCK)
        JOIN dbo.{_ident(receipt_mapping.document_table)} AS doc WITH (NOLOCK)
            ON doc.{_ident(receipt_mapping.document_id_column)}
                = line.{_ident(receipt_mapping.line_document_column)}
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = line.{_ident(receipt_mapping.line_nomenclature_column)}
        WHERE doc.{_ident(receipt_mapping.marked_column)} = 0x00
          AND doc.{_ident(receipt_mapping.posted_column)} = 0x01
          AND doc.{_ident(receipt_mapping.document_date_column)} >= :history_start
          AND doc.{_ident(receipt_mapping.document_date_column)} < :date_to
          AND CONVERT(varchar(34), line.{_ident(receipt_mapping.line_nomenclature_column)}, 1)
                IN :refs
        ORDER BY doc.{_ident(receipt_mapping.document_date_column)}, doc._Number, line.{line_no_column}
        """,
        refs=sorted(allowed_refs),
    ).bindparams(
        bindparam("history_start", value=datetime.combine(history_start, time.min)),
        bindparam("date_to", value=datetime.combine(date_to, time.min)),
    )
    return _fetch_chunked(engine, query, sorted(allowed_refs))


def build_lead_time_detail_rows(
    supplier_order_rows: Sequence[Mapping[str, Any]],
    receipt_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    receipts_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in receipt_rows:
        receipt_at = _date(row.get("receipt_at") or row.get("receipt_date"))
        ref = _clean(row.get("nomenclature_ref"))
        if ref and receipt_at:
            receipts_by_ref[ref].append({**dict(row), "receipt_at": receipt_at})
    for rows in receipts_by_ref.values():
        rows.sort(key=lambda row: (row["receipt_at"], _clean(row.get("receipt_number"))))

    detail_rows: list[dict[str, Any]] = []
    for row in supplier_order_rows:
        item = dict(row)
        nomenclature_ref = _clean(item.get("nomenclature_ref"))
        order_date = _date(
            item.get("supplier_order_created_at")
            or item.get("supplier_order_date")
            or item.get("order_date")
            or item.get("document_date")
        )
        cargo_date = _date(item.get("cargo_handoff_at") or item.get("cargo_handoff_date"))
        expected_receipt_at = _date(item.get("expected_receipt_at"))
        receipt = _nearest_receipt_after(receipts_by_ref.get(nomenclature_ref, ()), cargo_date)
        receipt_date = _date(receipt.get("receipt_at")) if receipt else None

        prepare_days = _days_between(order_date, cargo_date)
        logistics_days = _days_between(cargo_date, receipt_date)
        total_days = _days_between(order_date, receipt_date)
        missing_cargo = cargo_date is None
        missing_receipt = cargo_date is not None and receipt_date is None
        negative_prepare = prepare_days is not None and prepare_days < 0
        notes = []
        if missing_cargo:
            notes.append("нет даты сдачи в cargo")
        if missing_receipt:
            notes.append("нет поступления после cargo по этому SKU")
        if negative_prepare:
            notes.append("дата cargo раньше даты заказа")

        detail_rows.append(
            {
                "supplier_name": _clean(item.get("supplier_name")) or "unknown_supplier",
                "supplier_code": _clean(item.get("supplier_code")),
                "supplier_ref": _clean(item.get("supplier_ref")),
                "responsible_name": _clean(item.get("responsible_name")) or "unknown_responsible",
                "responsible_code": _clean(item.get("responsible_code")),
                "responsible_ref": _clean(item.get("responsible_ref")),
                "supplier_order_number": _clean(item.get("supplier_order_number")),
                "supplier_order_ref": _clean(item.get("supplier_order_ref")),
                "supplier_order_created_at": _json_date(order_date),
                "cargo_handoff_at": _json_date(cargo_date),
                "expected_receipt_at": _json_date(expected_receipt_at),
                "warehouse_receipt_at": _json_date(receipt_date),
                "receipt_number": _clean(receipt.get("receipt_number")) if receipt else "",
                "receipt_ref": _clean(receipt.get("receipt_ref")) if receipt else "",
                "nomenclature_code": _clean(item.get("nomenclature_code")),
                "name": _clean(item.get("name")),
                "display_group_key": display_group_key(item),
                "qty": _json_decimal(_decimal(item.get("qty"))),
                "price": _json_decimal(_decimal(item.get("price"))),
                "amount": _json_decimal(_decimal(item.get("amount"))),
                "supplier_prepare_days": _json_int(prepare_days),
                "logistics_receiving_days": _json_int(logistics_days),
                "total_arrival_days": _json_int(total_days),
                "receipt_match_confidence": (
                    "same_sku_after_cargo"
                    if receipt_date
                    else "missing_cargo" if missing_cargo else "no_same_sku_receipt_after_cargo"
                ),
                "missing_cargo_handoff_at": int(missing_cargo),
                "missing_receipt_after_cargo": int(missing_receipt),
                "negative_supplier_prepare_days": int(negative_prepare),
                "supplier_prepare_outlier": 0,
                "logistics_receiving_outlier": 0,
                "notes": "; ".join(notes),
            }
        )
    detail_rows.sort(
        key=lambda item: (
            item["supplier_name"],
            item["nomenclature_code"],
            item["supplier_order_created_at"],
            item["supplier_order_number"],
        )
    )
    return detail_rows


def mark_lead_time_outliers(detail_rows: list[dict[str, Any]]) -> dict[str, Any]:
    prepare_values = [
        value
        for value in (_int_or_none(row.get("supplier_prepare_days")) for row in detail_rows)
        if value is not None and value >= 0
    ]
    logistics_values = [
        value
        for value in (_int_or_none(row.get("logistics_receiving_days")) for row in detail_rows)
        if value is not None and value >= 0
    ]
    prepare_threshold = _outlier_threshold(prepare_values)
    logistics_threshold = _outlier_threshold(logistics_values)
    for row in detail_rows:
        prepare = _int_or_none(row.get("supplier_prepare_days"))
        logistics = _int_or_none(row.get("logistics_receiving_days"))
        row["supplier_prepare_outlier"] = int(
            prepare_threshold is not None and prepare is not None and prepare > prepare_threshold
        )
        row["logistics_receiving_outlier"] = int(
            logistics_threshold is not None
            and logistics is not None
            and logistics > logistics_threshold
        )
    return {
        "supplier_prepare_days": _json_number(prepare_threshold),
        "logistics_receiving_days": _json_number(logistics_threshold),
    }


def aggregate_lead_time_rows(detail_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        key = (
            _clean(row.get("supplier_ref")),
            _clean(row.get("supplier_name")) or "unknown_supplier",
            _clean(row.get("nomenclature_code")),
            _clean(row.get("display_group_key")),
        )
        grouped[key].append(row)

    aggregate_rows: list[dict[str, Any]] = []
    for rows in grouped.values():
        first = rows[0]
        responsible = _top_identity(
            rows,
            ref_key="responsible_ref",
            code_key="responsible_code",
            name_key="responsible_name",
        )
        prepare_values = _metric_values(rows, "supplier_prepare_days")
        logistics_values = _metric_values(rows, "logistics_receiving_days")
        total_values = _metric_values(rows, "total_arrival_days")
        missing_cargo = sum(_int_or_none(row.get("missing_cargo_handoff_at")) or 0 for row in rows)
        missing_receipt = sum(
            _int_or_none(row.get("missing_receipt_after_cargo")) or 0 for row in rows
        )
        notes = [
            "receipt_match=nearest_same_sku_after_cargo",
            f"missing_cargo={missing_cargo}",
            f"missing_receipt_after_cargo={missing_receipt}",
        ]
        aggregate_rows.append(
            {
                "supplier_name": _clean(first.get("supplier_name")) or "unknown_supplier",
                "supplier_code": _clean(first.get("supplier_code")),
                "supplier_ref": _clean(first.get("supplier_ref")),
                "responsible_name": responsible["name"],
                "responsible_code": responsible["code"],
                "responsible_ref": responsible["ref"],
                "responsible_count": responsible["count"],
                "nomenclature_code": _clean(first.get("nomenclature_code")),
                "name": _clean(first.get("name")),
                "display_group_key": _clean(first.get("display_group_key")),
                "order_line_count": len(rows),
                "ordered_qty": _sum_decimal(rows, "qty"),
                "order_amount": _sum_decimal(rows, "amount"),
                "orders_with_cargo_count": sum(
                    1 for row in rows if _clean(row.get("cargo_handoff_at"))
                ),
                "orders_with_receipt_after_cargo_count": sum(
                    1 for row in rows if _clean(row.get("warehouse_receipt_at"))
                ),
                "missing_cargo_count": missing_cargo,
                "missing_receipt_after_cargo_count": missing_receipt,
                "negative_supplier_prepare_count": sum(
                    _int_or_none(row.get("negative_supplier_prepare_days")) or 0 for row in rows
                ),
                **_metric_columns("supplier_prepare_days", prepare_values),
                **_metric_columns("logistics_receiving_days", logistics_values),
                "total_arrival_days_avg": _avg(total_values),
                "total_arrival_days_median": _median(total_values),
                "supplier_prepare_outlier_count": sum(
                    _int_or_none(row.get("supplier_prepare_outlier")) or 0 for row in rows
                ),
                "logistics_receiving_outlier_count": sum(
                    _int_or_none(row.get("logistics_receiving_outlier")) or 0 for row in rows
                ),
                "latest_supplier_order_at": _max_text_date(
                    row.get("supplier_order_created_at") for row in rows
                ),
                "latest_cargo_handoff_at": _max_text_date(
                    row.get("cargo_handoff_at") for row in rows
                ),
                "latest_receipt_at": _max_text_date(
                    row.get("warehouse_receipt_at") for row in rows
                ),
                "recommended_supplier_prepare_days": _recommended_days(prepare_values),
                "recommended_logistics_days": _recommended_days(logistics_values),
                "lead_time_confidence": _confidence(
                    len(rows),
                    len(prepare_values),
                    len(logistics_values),
                    missing_cargo + missing_receipt,
                ),
                "notes": "; ".join(notes),
            }
        )
    aggregate_rows.sort(
        key=lambda row: (
            row["supplier_name"],
            row["display_group_key"],
            row["nomenclature_code"],
        )
    )
    return aggregate_rows


def build_weekly_seasonality_rows(
    detail_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    prepare_baseline = _median_int(_metric_values(detail_rows, "supplier_prepare_days"))
    logistics_baseline = _median_int(_metric_values(detail_rows, "logistics_receiving_days"))
    grouped: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        order_date = _date(row.get("supplier_order_created_at"))
        if order_date is None:
            continue
        grouped[_week_start(order_date)].append(row)

    rows: list[dict[str, Any]] = []
    for week_start, week_rows in sorted(grouped.items()):
        prepare_values = _metric_values(week_rows, "supplier_prepare_days")
        logistics_values = _metric_values(week_rows, "logistics_receiving_days")
        total_values = _metric_values(week_rows, "total_arrival_days")
        prepare_median = _median_int(prepare_values)
        logistics_median = _median_int(logistics_values)
        prepare_delta = _delta(prepare_median, prepare_baseline)
        logistics_delta = _delta(logistics_median, logistics_baseline)
        missing_cargo = sum(
            _int_or_none(row.get("missing_cargo_handoff_at")) or 0 for row in week_rows
        )
        missing_receipt = sum(
            _int_or_none(row.get("missing_receipt_after_cargo")) or 0 for row in week_rows
        )
        order_count = len(week_rows)
        prepare_delay = bool(
            order_count >= 3
            and prepare_delta is not None
            and prepare_delta >= 7
            and prepare_median is not None
        )
        missing_receipt_share = (
            Decimal(missing_receipt) / Decimal(order_count) if order_count else Decimal("0")
        )
        road_seasonality = bool(
            order_count >= 3
            and (
                (
                    logistics_delta is not None
                    and logistics_delta >= 7
                    and logistics_median is not None
                )
                or missing_receipt_share >= Decimal("0.40")
            )
        )
        notes = []
        season_label = _season_label(week_start)
        if season_label == "pre_new_year":
            notes.append("предновогоднее окно")
        if prepare_delay:
            notes.append("подготовка дольше базовой")
        if road_seasonality:
            notes.append("сезонность дороги / внешний фактор к проверке")

        supplier = _top_identity(
            week_rows,
            ref_key="supplier_ref",
            code_key="supplier_code",
            name_key="supplier_name",
        )
        responsible = _top_identity(
            week_rows,
            ref_key="responsible_ref",
            code_key="responsible_code",
            name_key="responsible_name",
        )
        iso_year, iso_week, _ = week_start.isocalendar()
        rows.append(
            {
                "week_start": week_start.isoformat(),
                "iso_year": iso_year,
                "iso_week": iso_week,
                "month": week_start.month,
                "season_label": season_label,
                "order_line_count": order_count,
                "ordered_qty": _sum_decimal(week_rows, "qty"),
                "order_amount": _sum_decimal(week_rows, "amount"),
                "orders_with_cargo_count": sum(
                    1 for row in week_rows if _clean(row.get("cargo_handoff_at"))
                ),
                "orders_with_receipt_after_cargo_count": sum(
                    1 for row in week_rows if _clean(row.get("warehouse_receipt_at"))
                ),
                "missing_cargo_count": missing_cargo,
                "missing_receipt_after_cargo_count": missing_receipt,
                "supplier_prepare_days_median": _json_int(prepare_median),
                "logistics_receiving_days_median": _json_int(logistics_median),
                "total_arrival_days_median": _json_int(_median_int(total_values)),
                "baseline_supplier_prepare_days": _json_int(prepare_baseline),
                "baseline_logistics_days": _json_int(logistics_baseline),
                "supplier_prepare_delta_days": _json_int(prepare_delta),
                "logistics_delta_days": _json_int(logistics_delta),
                "prepare_delay_signal": int(prepare_delay),
                "road_seasonality_signal": int(road_seasonality),
                "route_risk_level": _route_risk_level(
                    order_count=order_count,
                    prepare_delay=prepare_delay,
                    road_seasonality=road_seasonality,
                    missing_receipt_share=missing_receipt_share,
                ),
                "top_supplier_name": supplier["name"],
                "top_responsible_name": responsible["name"],
                "supplier_count": supplier["count"],
                "responsible_count": responsible["count"],
                "notes": "; ".join(notes),
            }
        )
    return rows


def build_seasonality_summary(
    seasonality_rows: Sequence[Mapping[str, Any]],
    *,
    history_start: date,
    as_of: date,
) -> dict[str, Any]:
    risk_counts = Counter(_clean(row.get("route_risk_level")) for row in seasonality_rows)
    road_signal_rows = [
        row for row in seasonality_rows if (_int_or_none(row.get("road_seasonality_signal")) or 0)
    ]
    prepare_signal_rows = [
        row for row in seasonality_rows if (_int_or_none(row.get("prepare_delay_signal")) or 0)
    ]
    top_road_weeks = sorted(
        road_signal_rows,
        key=lambda row: (
            _int_or_none(row.get("logistics_delta_days")) or -999,
            _int_or_none(row.get("missing_receipt_after_cargo_count")) or 0,
            _int_or_none(row.get("order_line_count")) or 0,
        ),
        reverse=True,
    )[:10]
    top_prepare_weeks = sorted(
        prepare_signal_rows,
        key=lambda row: (
            _int_or_none(row.get("supplier_prepare_delta_days")) or -999,
            _int_or_none(row.get("order_line_count")) or 0,
        ),
        reverse=True,
    )[:10]
    return {
        "schema": "display_supplier_lead_time_seasonality.v1",
        "history_start": history_start.isoformat(),
        "as_of": as_of.isoformat(),
        "week_count": len(seasonality_rows),
        "route_risk_counts": dict(sorted(risk_counts.items())),
        "road_seasonality_signal_weeks": len(road_signal_rows),
        "prepare_delay_signal_weeks": len(prepare_signal_rows),
        "top_road_seasonality_weeks": [_seasonality_summary_row(row) for row in top_road_weeks],
        "top_prepare_delay_weeks": [_seasonality_summary_row(row) for row in top_prepare_weeks],
    }


def build_summary(
    detail_rows: Sequence[Mapping[str, Any]],
    *,
    aggregate_rows: Sequence[Mapping[str, Any]],
    seasonality_rows: Sequence[Mapping[str, Any]] = (),
    source_counts: Mapping[str, int],
    history_start: date,
    as_of: date,
    outlier_thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    prepare_values = _metric_values(detail_rows, "supplier_prepare_days")
    logistics_values = _metric_values(detail_rows, "logistics_receiving_days")
    total_values = _metric_values(detail_rows, "total_arrival_days")
    confidence_counts = Counter(_clean(row.get("lead_time_confidence")) for row in aggregate_rows)
    slow_rows = sorted(
        aggregate_rows,
        key=lambda row: (
            _int_or_none(row.get("recommended_supplier_prepare_days")) or 0,
            _int_or_none(row.get("recommended_logistics_days")) or 0,
            _int_or_none(row.get("order_line_count")) or 0,
        ),
        reverse=True,
    )[:10]
    return {
        "schema": "display_supplier_lead_time_history.v1",
        "history_start": history_start.isoformat(),
        "as_of": as_of.isoformat(),
        "source_counts": dict(source_counts),
        "detail_rows": len(detail_rows),
        "aggregate_rows": len(aggregate_rows),
        "seasonality_weeks": len(seasonality_rows),
        "road_seasonality_signal_weeks": sum(
            _int_or_none(row.get("road_seasonality_signal")) or 0 for row in seasonality_rows
        ),
        "prepare_delay_signal_weeks": sum(
            _int_or_none(row.get("prepare_delay_signal")) or 0 for row in seasonality_rows
        ),
        "supplier_count": len(
            {_clean(row.get("supplier_ref")) for row in detail_rows if row.get("supplier_ref")}
        ),
        "sku_count": len({_clean(row.get("nomenclature_code")) for row in detail_rows}),
        "missing_cargo_count": sum(
            _int_or_none(row.get("missing_cargo_handoff_at")) or 0 for row in detail_rows
        ),
        "missing_receipt_after_cargo_count": sum(
            _int_or_none(row.get("missing_receipt_after_cargo")) or 0 for row in detail_rows
        ),
        "negative_supplier_prepare_count": sum(
            _int_or_none(row.get("negative_supplier_prepare_days")) or 0 for row in detail_rows
        ),
        "supplier_prepare_outlier_count": sum(
            _int_or_none(row.get("supplier_prepare_outlier")) or 0 for row in detail_rows
        ),
        "logistics_receiving_outlier_count": sum(
            _int_or_none(row.get("logistics_receiving_outlier")) or 0 for row in detail_rows
        ),
        "supplier_prepare_days_avg": _avg(prepare_values),
        "supplier_prepare_days_median": _median(prepare_values),
        "logistics_receiving_days_avg": _avg(logistics_values),
        "logistics_receiving_days_median": _median(logistics_values),
        "total_arrival_days_avg": _avg(total_values),
        "total_arrival_days_median": _median(total_values),
        "recommended_defaults": {
            "supplier_prepare_days": _recommended_days(prepare_values),
            "logistics_days": _recommended_days(logistics_values),
            "lead_time_days": _json_int(
                (_int_or_none(_recommended_days(prepare_values)) or 0)
                + (_int_or_none(_recommended_days(logistics_values)) or 0)
            ),
        },
        "outlier_thresholds": dict(outlier_thresholds),
        "lead_time_confidence_counts": dict(sorted(confidence_counts.items())),
        "top_slow_rows": [
            {
                "supplier_name": row.get("supplier_name"),
                "nomenclature_code": row.get("nomenclature_code"),
                "name": row.get("name"),
                "recommended_supplier_prepare_days": row.get("recommended_supplier_prepare_days"),
                "recommended_logistics_days": row.get("recommended_logistics_days"),
                "order_line_count": row.get("order_line_count"),
            }
            for row in slow_rows
        ],
        "method_note": (
            "warehouse_receipt_at is matched as the nearest posted same-SKU receipt after "
            "cargo_handoff_at; it is not a direct subordinate-document link yet"
        ),
    }


def display_group_key(row: Mapping[str, Any]) -> str:
    name = _clean(row.get("name"))
    if not name:
        return ""
    value = name.casefold().replace("ё", "е")
    if "дисплей для " in value:
        value = value.split("дисплей для ", 1)[1]
    value = value.split("+", 1)[0]
    value = value.split("(", 1)[0]
    value = re.sub(r"\s*/\s*", " / ", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value[:160]


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


def _load_document_line_mapping(path: Path | None, *, error_code: str) -> DocumentLineMapping:
    if path is None:
        raise ValueError(f"{error_code}: mapping_json_required")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{error_code}: mapping_json_must_be_object")
    return DocumentLineMapping.from_mapping(payload)


def _fetch_chunked(engine, query, refs: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for refs_chunk in _chunks(refs, MAX_SQLSERVER_EXPANDING_REFS):
            rows.extend(dict(row) for row in conn.execute(query, {"refs": refs_chunk}).mappings())
    return rows


def _nearest_receipt_after(
    receipts: Sequence[Mapping[str, Any]],
    cargo_date: date | None,
) -> Mapping[str, Any] | None:
    if cargo_date is None:
        return None
    for receipt in receipts:
        receipt_at = _date(receipt.get("receipt_at") or receipt.get("receipt_date"))
        if receipt_at is not None and receipt_at >= cargo_date:
            return receipt
    return None


def _top_identity(
    rows: Sequence[Mapping[str, Any]],
    *,
    ref_key: str,
    code_key: str,
    name_key: str,
) -> dict[str, str | int]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        ref = _clean(row.get(ref_key))
        code = _clean(row.get(code_key))
        name = _clean(row.get(name_key)) or "unknown"
        if ref or code or name != "unknown":
            counts[(ref, code, name)] += 1
    if not counts:
        return {"ref": "", "code": "", "name": "", "count": 0}
    (ref, code, name), _ = counts.most_common(1)[0]
    return {"ref": ref, "code": code, "name": name, "count": len(counts)}


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _season_label(value: date) -> str:
    if value.month in {11, 12}:
        return "pre_new_year"
    if value.month in {1, 2}:
        return "post_new_year"
    return "regular"


def _median_int(values: Sequence[int]) -> int | None:
    if not values:
        return None
    return int(Decimal(str(median(values))).to_integral_value(rounding=ROUND_CEILING))


def _delta(value: int | None, baseline: int | None) -> int | None:
    if value is None or baseline is None:
        return None
    return value - baseline


def _route_risk_level(
    *,
    order_count: int,
    prepare_delay: bool,
    road_seasonality: bool,
    missing_receipt_share: Decimal,
) -> str:
    if road_seasonality and (order_count >= 8 or missing_receipt_share >= Decimal("0.60")):
        return "high"
    if road_seasonality or prepare_delay:
        return "medium"
    return "normal"


def _seasonality_summary_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "week_start": row.get("week_start"),
        "order_line_count": row.get("order_line_count"),
        "supplier_prepare_delta_days": row.get("supplier_prepare_delta_days"),
        "logistics_delta_days": row.get("logistics_delta_days"),
        "route_risk_level": row.get("route_risk_level"),
        "top_supplier_name": row.get("top_supplier_name"),
        "top_responsible_name": row.get("top_responsible_name"),
        "notes": row.get("notes"),
    }


def _metric_columns(prefix: str, values: Sequence[int]) -> dict[str, str]:
    return {
        f"{prefix}_avg": _avg(values),
        f"{prefix}_median": _median(values),
        f"{prefix}_min": _json_int(min(values) if values else None),
        f"{prefix}_max": _json_int(max(values) if values else None),
    }


def _metric_values(rows: Sequence[Mapping[str, Any]], key: str) -> list[int]:
    values = []
    for row in rows:
        value = _int_or_none(row.get(key))
        if value is not None and value >= 0:
            values.append(value)
    return values


def _outlier_threshold(values: Sequence[int]) -> float | None:
    if len(values) < 4:
        return None
    sorted_values = sorted(values)
    midpoint = len(sorted_values) // 2
    lower = sorted_values[:midpoint]
    upper = (
        sorted_values[midpoint:] if len(sorted_values) % 2 == 0 else sorted_values[midpoint + 1 :]
    )
    if not lower or not upper:
        return None
    q1 = float(median(lower))
    q3 = float(median(upper))
    iqr = q3 - q1
    return q3 + 1.5 * iqr


def _confidence(
    order_line_count: int,
    prepare_count: int,
    logistics_count: int,
    missing_count: int,
) -> str:
    if order_line_count <= 0:
        return "low"
    missing_share = Decimal(missing_count) / Decimal(order_line_count * 2)
    if prepare_count >= 5 and logistics_count >= 5 and missing_share <= Decimal("0.25"):
        return "high"
    if prepare_count >= 2 and logistics_count >= 2 and missing_share <= Decimal("0.50"):
        return "medium"
    return "low"


def _recommended_days(values: Sequence[int]) -> str:
    if not values:
        return ""
    basis = median(values) if len(values) >= 3 else max(values)
    return _json_int(int(Decimal(str(basis)).to_integral_value(rounding=ROUND_CEILING)))


def _sum_decimal(rows: Sequence[Mapping[str, Any]], key: str) -> str:
    total = Decimal("0")
    has_value = False
    for row in rows:
        value = _decimal(row.get(key))
        if value is not None:
            total += value
            has_value = True
    return _json_decimal(total) if has_value else ""


def _avg(values: Sequence[int]) -> str:
    if not values:
        return ""
    return _json_number(round(mean(values), 1))


def _median(values: Sequence[int]) -> str:
    if not values:
        return ""
    return _json_number(round(float(median(values)), 1))


def _max_text_date(values: Sequence[Any]) -> str:
    dates = [_date(value) for value in values]
    dates = [value for value in dates if value is not None]
    return max(dates).isoformat() if dates else ""


def _days_between(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value.date()
    elif isinstance(value, date):
        result = value
    else:
        text_value = str(value).strip().removesuffix("Z")
        if "T" in text_value:
            text_value = text_value.split("T", 1)[0]
        if " " in text_value:
            text_value = text_value.split(" ", 1)[0]
        try:
            result = date.fromisoformat(text_value)
        except ValueError:
            return None
    return None if result <= ONEC_EMPTY_DATE else result


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _ident(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"unsafe_sql_identifier:{value}")
    return value


def _json_date(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _json_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    formatted = format(value.normalize(), "f")
    if "." not in formatted:
        return formatted
    return formatted.rstrip("0").rstrip(".") or "0"


def _json_int(value: int | None) -> str:
    return "" if value is None else str(value)


def _json_number(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _expanding_text(sql: str, **expanding_values: Sequence[str]):
    statement = text(sql)
    for name, values in expanding_values.items():
        statement = statement.bindparams(bindparam(name, value=tuple(values), expanding=True))
    return statement


def _chunks(values: Sequence[str], size: int) -> list[tuple[str, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a read-only display supplier lead-time report from 1C supplier orders "
            "and posted receipts."
        )
    )
    parser.add_argument("--folder", default="дисплеи", help="1C folder/name filter")
    parser.add_argument("--history-months", type=int, default=6)
    parser.add_argument("--as-of", type=_parse_date, default=date.today())
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument(
        "--supplier-order-mapping-json",
        type=Path,
        default=DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    )
    parser.add_argument("--receipt-mapping-json", type=Path, default=DEFAULT_RECEIPT_MAPPING_JSON)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--output-detail-csv", type=Path, default=DEFAULT_OUTPUT_DETAIL_CSV)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument(
        "--output-seasonality-csv",
        type=Path,
        default=DEFAULT_OUTPUT_SEASONALITY_CSV,
    )
    parser.add_argument(
        "--output-seasonality-json",
        type=Path,
        default=DEFAULT_OUTPUT_SEASONALITY_JSON,
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.history_months <= 0:
        raise SystemExit("--history-months must be positive")
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    return args


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
