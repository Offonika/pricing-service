#!/usr/bin/env python3
"""Load open 1C procurement supplier orders and dry-run/apply them to Bitrix."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.procurement_supplier_crm import (  # noqa: E402
    normalize_procurement_contour,
    procurement_stage_key,
    sync_supplier_to_crm,
)
from scripts.ensure_procurement_bitrix_process import (  # noqa: E402
    DEFAULT_ENV_FILE,
    DEFAULT_MAPPING_PATH,
    load_env,
)
from scripts.import_onec_supplier_order_to_procurement import (  # noqa: E402
    DEFAULT_CARGO_FINANCE_USER_ID,
    BitrixRestApi,
    crm_item_rest_field_name,
    field_name,
    formatted_amount,
    import_order,
    item_matches_order_identity,
    load_mapping,
    source_date,
    source_number,
    source_number_lookup_candidates,
    source_type,
    value_year,
)

DEFAULT_INPUT_PATH = REPO_ROOT / "build/bitrix/onec_open_procurement_supplier_orders_input.json"
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/onec_open_procurement_supplier_orders_result.json"
ONEC_EMPTY_DATE = date(1753, 1, 1)
OPEN_BALANCE_PERIOD = "3999-11-01T00:00:00"
DEFAULT_CONTOUR_KEYS = ("cargo", "ved_import")
CONTOUR_BY_ENUM_ORDER = {
    0: "Обычный",
    1: "Карго",
    2: "ВЭДИмпорт",
}
CONTOUR_ENUM_ORDER_BY_KEY = {
    "ordinary": 0,
    "cargo": 1,
    "ved_import": 2,
}
CONTOUR_TITLE_PREFIX = {
    "ordinary": "Закупка",
    "cargo": "Карго",
    "ved_import": "ВЭД импорт",
}
READ_ONLY_BITRIX_METHODS = {
    "crm.company.list",
    "crm.company.get",
    "crm.contact.list",
    "crm.contact.get",
    "crm.duplicate.findbycomm",
    "crm.item.list",
}


class CachedBitrixApi:
    """Cache identical read-only REST calls during dry-run batch imports."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.cache: dict[tuple[str, str], Any] = {}

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if method not in READ_ONLY_BITRIX_METHODS:
            return self.api.call(method, params)
        key = (method, json.dumps(params, ensure_ascii=False, sort_keys=True, default=str))
        if key not in self.cache:
            self.cache[key] = self.api.call(method, params)
        return deepcopy(self.cache[key])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--input-json", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--assigned-by-id", default="")
    parser.add_argument(
        "--finance-user-id",
        default="",
        help="Bitrix user id for cargo payment task; defaults to env or Karina Avakyan 130746.",
    )
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--date-from", help="Filter 1C order date from YYYY-MM-DD.")
    parser.add_argument("--date-to", help="Filter 1C order date through YYYY-MM-DD.")
    parser.add_argument(
        "--contours",
        default=",".join(DEFAULT_CONTOUR_KEYS),
        help="Comma-separated procurement contours to sync: cargo, ved_import, ordinary.",
    )
    parser.add_argument(
        "--blank-contour-cargo-dropoff-only",
        action="store_true",
        help="Only sync open orders with empty КонтурЗакупки and filled Сдача в карго.",
    )
    parser.add_argument("--skip-bitrix", action="store_true", help="Only export 1C input JSON.")
    parser.add_argument("--apply", action="store_true", help="Write Bitrix changes.")
    return parser.parse_args(argv)


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def clean(value: Any) -> str:
    return str(value or "").strip()


def parse_contour_keys(value: str) -> set[str]:
    aliases = {
        "ordinary": "ordinary",
        "обычный": "ordinary",
        "cargo": "cargo",
        "карго": "cargo",
        "ved": "ved_import",
        "vedimport": "ved_import",
        "ved_import": "ved_import",
        "вэд": "ved_import",
        "вэдимпорт": "ved_import",
        "вэд_импорт": "ved_import",
    }
    keys: set[str] = set()
    for part in clean(value).split(","):
        compact = part.strip().casefold().replace(" ", "").replace("-", "_")
        if not compact:
            continue
        key = aliases.get(compact)
        if not key:
            raise ValueError(f"Unsupported procurement contour filter: {part!r}")
        keys.add(key)
    return keys or set(DEFAULT_CONTOUR_KEYS)


def normalize_onec_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.date() <= ONEC_EMPTY_DATE:
            return ""
        return value.isoformat()
    if isinstance(value, date):
        if value <= ONEC_EMPTY_DATE:
            return ""
        return value.isoformat()
    raw = clean(value)
    return "" if not raw or raw.startswith("1753-01-01") else raw


def contour_value(row: dict[str, Any]) -> str:
    enum_order = row.get("contour_enum_order")
    if enum_order is None:
        return ""
    try:
        return CONTOUR_BY_ENUM_ORDER.get(int(enum_order), "")
    except (TypeError, ValueError):
        return ""


def build_title(order: dict[str, Any]) -> str:
    number = clean(order.get("number"))
    amount = formatted_amount(order)
    supplier = order.get("supplier") if isinstance(order.get("supplier"), dict) else {}
    supplier_title = clean(supplier.get("title"))
    prefix = CONTOUR_TITLE_PREFIX.get(clean(order.get("procurement_contour_key")), "Закупка")
    return " · ".join(part for part in [prefix, number, amount, supplier_title] if part)


def order_from_open_supplier_order_row(
    row: dict[str, Any],
    *,
    allowed_contours: set[str],
) -> dict[str, Any] | None:
    contour = contour_value(row)
    currency = clean(row.get("currency_name"))
    cargo_dropoff_date = normalize_onec_date(row.get("cargo_dropoff_date"))
    logical_key = normalize_procurement_contour(
        contour,
        is_open_supplier_order=True,
        currency=currency,
        has_cargo_dropoff=bool(cargo_dropoff_date),
    )
    if logical_key not in allowed_contours:
        return None
    number = clean(row.get("number"))
    order_date = normalize_onec_date(row.get("order_date"))
    order: dict[str, Any] = {
        "number": number,
        "onec_source_number": number,
        "source_type": "ЗаказПоставщику",
        "date": order_date,
        "onec_source_date": order_date,
        "posted": bool(row.get("posted")),
        "КонтурЗакупки": contour,
        "procurement_contour_key": logical_key,
        "is_open_supplier_order": True,
        "supplier": {
            "onec_ref": clean(row.get("supplier_ref")),
            "title": clean(row.get("supplier_name")),
        },
        "currency": currency,
        "amount": row.get("open_amount"),
        "planned_warehouse": clean(row.get("store_name")),
        "supplier_dispatch_date": normalize_onec_date(row.get("supplier_dispatch_date")),
        "cargo_dropoff_date": cargo_dropoff_date,
        "expected_receipt_date": normalize_onec_date(row.get("expected_receipt_date")),
        "payment_date": normalize_onec_date(row.get("payment_date")),
        "open_qty": row.get("open_qty"),
        "open_amount_rub": row.get("open_amount_rub"),
        "open_line_count": row.get("open_line_count"),
        "onec_ref": clean(row.get("onec_ref")),
        "contract_name": clean(row.get("contract_name")),
        "comment": clean(row.get("comment")),
    }
    order["procurement_stage_key"] = procurement_stage_key(logical_key, order)
    order["title"] = build_title(order)
    return order


def fetch_open_supplier_orders(
    onec_database_url: str,
    *,
    limit: int,
    date_from: str,
    date_to: str,
    contours: set[str],
    blank_contour_cargo_dropoff_only: bool = False,
    filter_contours_in_sql: bool = False,
    fail_on_query_limit: bool = False,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 5000))
    filters = [
        "doc._Marked = 0x00",
        "doc._Posted = 0x01",
    ]
    params: dict[str, Any] = {}
    if date_from:
        filters.append("doc._Date_Time >= :date_from")
        params["date_from"] = datetime.fromisoformat(date_from)
    if date_to:
        filters.append("doc._Date_Time < :date_to")
        params["date_to"] = datetime.fromisoformat(date_to)
    if blank_contour_cargo_dropoff_only:
        filters.append("contour._EnumOrder IS NULL")
        filters.append("doc._Fld8852 > :empty_onec_date")
        params["empty_onec_date"] = datetime.combine(ONEC_EMPTY_DATE, datetime.min.time())
    elif filter_contours_in_sql:
        enum_orders = sorted(
            CONTOUR_ENUM_ORDER_BY_KEY[key] for key in contours if key in CONTOUR_ENUM_ORDER_BY_KEY
        )
        if not enum_orders:
            raise ValueError("No supported procurement contours for SQL filter")
        contour_params: list[str] = []
        for index, enum_order in enumerate(enum_orders):
            param_name = f"contour_enum_order_{index}"
            params[param_name] = enum_order
            contour_params.append(f":{param_name}")
        # Unassigned 1C contours must stay in the candidate set because the
        # established normalization classifies foreign-currency and cargo-date
        # orders as logical `cargo`.
        filters.append(
            f"(contour._EnumOrder IN ({', '.join(contour_params)}) "
            "OR contour._EnumOrder IS NULL)"
        )
    where_sql = " AND ".join(f"({part})" for part in filters)
    sql = text(f"""
        WITH open_balance AS (
            SELECT
                bal._Fld7149RRef AS order_ref,
                SUM(CAST(bal._Fld7156 AS decimal(18, 3))) AS open_qty,
                SUM(CAST(bal._Fld7157 AS decimal(18, 2))) AS open_amount,
                SUM(CAST(bal._Fld7158 AS decimal(18, 2))) AS open_amount_rub,
                COUNT(*) AS open_line_count
            FROM dbo._AccumRgT7160 AS bal WITH (NOLOCK)
            WHERE bal._Period = :balance_period
            GROUP BY bal._Fld7149RRef
            HAVING SUM(CAST(bal._Fld7156 AS decimal(18, 3))) > 0
        )
        SELECT TOP {limit}
            CONVERT(varchar(34), doc._IDRRef, 1) AS onec_ref,
            NULLIF(LTRIM(RTRIM(doc._Number)), N'') AS number,
            doc._Date_Time AS order_date,
            CASE WHEN doc._Posted = 0x01 THEN 1 ELSE 0 END AS posted,
            COALESCE(CONVERT(varchar(34), doc._Fld2498RRef, 1), '') AS supplier_ref,
            COALESCE(supplier_ref._Description, '') AS supplier_name,
            COALESCE(CONVERT(varchar(34), doc._Fld2494RRef, 1), '') AS contract_ref,
            COALESCE(contract_ref._Description, '') AS contract_name,
            COALESCE(CONVERT(varchar(34), doc._Fld2506RRef, 1), '') AS store_ref,
            COALESCE(store_ref._Description, '') AS store_name,
            COALESCE(CONVERT(varchar(34), doc._Fld2490RRef, 1), '') AS currency_ref,
            COALESCE(currency_ref._Description, '') AS currency_name,
            CAST(open_balance.open_qty AS decimal(18, 3)) AS open_qty,
            CAST(open_balance.open_amount AS decimal(18, 2)) AS open_amount,
            CAST(open_balance.open_amount_rub AS decimal(18, 2)) AS open_amount_rub,
            CAST(open_balance.open_line_count AS int) AS open_line_count,
            doc._Fld8851 AS supplier_dispatch_date,
            doc._Fld8852 AS cargo_dropoff_date,
            doc._Fld2493 AS expected_receipt_date,
            doc._Fld2492 AS payment_date,
            doc._Fld2497 AS comment,
            contour._EnumOrder AS contour_enum_order
        FROM open_balance
        JOIN dbo._Document133 AS doc WITH (NOLOCK)
            ON doc._IDRRef = open_balance.order_ref
        LEFT JOIN dbo._Reference54 AS supplier_ref WITH (NOLOCK)
            ON supplier_ref._IDRRef = doc._Fld2498RRef
        LEFT JOIN dbo._Reference37 AS contract_ref WITH (NOLOCK)
            ON contract_ref._IDRRef = doc._Fld2494RRef
        LEFT JOIN dbo._Reference80 AS store_ref WITH (NOLOCK)
            ON store_ref._IDRRef = doc._Fld2506RRef
        LEFT JOIN dbo._Reference20 AS currency_ref WITH (NOLOCK)
            ON currency_ref._IDRRef = doc._Fld2490RRef
        LEFT JOIN dbo._Enum10091 AS contour WITH (NOLOCK)
            ON contour._IDRRef = doc._Fld10092RRef
        WHERE {where_sql}
        ORDER BY doc._Date_Time DESC
        """)
    params["balance_period"] = datetime.fromisoformat(OPEN_BALANCE_PERIOD)
    engine = create_engine(onec_database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).mappings()]
    if fail_on_query_limit and len(rows) >= limit:
        raise RuntimeError(
            f"procurement source query may be truncated: fetched {len(rows)} rows at limit {limit}"
        )
    orders: list[dict[str, Any]] = []
    for row in rows:
        order = order_from_open_supplier_order_row(row, allowed_contours=contours)
        if order:
            orders.append(order)
    return orders


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )


def bitrix_webhook(args: argparse.Namespace, env: dict[str, str]) -> str:
    return (
        clean(args.webhook_url)
        or clean(env.get("PROCUREMENT_BITRIX_WEBHOOK_URL"))
        or clean(env.get("BITRIX_BOX_WEBHOOK_BASE"))
        or clean(env.get("BITRIX24_BOX_WEBHOOK_URL"))
    )


def list_existing_procurement_items(api: Any, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    number_field = crm_item_rest_field_name(field_name(mapping, "onec_source_number"))
    source_type_field = crm_item_rest_field_name(field_name(mapping, "onec_source_type"))
    source_date_field = crm_item_rest_field_name(field_name(mapping, "onec_source_date"))
    if not entity_type_id or not number_field:
        return []
    select = [
        field
        for field in ["id", "title", number_field, source_type_field, source_date_field]
        if field
    ]
    items: list[dict[str, Any]] = []
    start: int | None = 0
    while True:
        params: dict[str, Any] = {"entityTypeId": entity_type_id, "select": select}
        if start is not None:
            params["start"] = start
        payload = api.call("crm.item.list", params)
        result = payload.get("result") if isinstance(payload, dict) else payload
        page_items = result.get("items") if isinstance(result, dict) else []
        if not isinstance(page_items, list):
            break
        items.extend(item for item in page_items if isinstance(item, dict))
        next_start = payload.get("next") if isinstance(payload, dict) else None
        if next_start is None or not page_items:
            break
        start = int(next_start)
    return items


def prefetched_procurement_item_id(
    existing_items: list[dict[str, Any]], order: dict[str, Any], mapping: dict[str, Any]
) -> str:
    number_field = crm_item_rest_field_name(field_name(mapping, "onec_source_number"))
    number = source_number(order)
    if not number_field or not number:
        return ""
    source_type_field = crm_item_rest_field_name(field_name(mapping, "onec_source_type"))
    source_date_field = crm_item_rest_field_name(field_name(mapping, "onec_source_date"))
    expected_source_type = source_type(order)
    expected_year = value_year(source_date(order))
    lookup_candidates = source_number_lookup_candidates(number)
    for candidates in [lookup_candidates[:1], lookup_candidates[1:]]:
        if not candidates:
            continue
        matched_ids: set[str] = set()
        for candidate in candidates:
            for item in existing_items:
                if clean(item.get(number_field)) != candidate:
                    continue
                if not item_matches_order_identity(
                    item,
                    source_type_field=source_type_field,
                    expected_source_type=expected_source_type,
                    source_date_field=source_date_field,
                    expected_year=expected_year,
                ):
                    continue
                item_id = clean(item.get("id"))
                if item_id:
                    matched_ids.add(item_id)
        if len(matched_ids) == 1:
            return next(iter(matched_ids))
        if len(matched_ids) > 1:
            raise RuntimeError(
                "Найдено несколько Bitrix-карточек закупки для одного номера 1С "
                f"{number!r}; автообновление остановлено."
            )
    return ""


def supplier_cache_key(order: dict[str, Any]) -> str:
    supplier = order.get("supplier") if isinstance(order.get("supplier"), dict) else {}
    return clean(supplier.get("onec_ref")) or clean(supplier.get("title")).casefold()


def run_bitrix_import(
    orders: list[dict[str, Any]],
    *,
    webhook_base: str,
    mapping: dict[str, Any],
    apply: bool,
    assigned_by_id: str,
    finance_user_id: str,
) -> list[dict[str, Any]]:
    base_api = BitrixRestApi(webhook_base)
    api = base_api if apply else CachedBitrixApi(base_api)
    existing_items = list_existing_procurement_items(api, mapping)
    supplier_results: dict[str, dict[str, Any]] = {}
    used_batch_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for order in orders:
        try:
            existing_id = prefetched_procurement_item_id(existing_items, order, mapping)
            supplier_key = supplier_cache_key(order)
            supplier_result = supplier_results.get(supplier_key) if supplier_key else None
            if supplier_result is None:
                supplier = order.get("supplier") if isinstance(order.get("supplier"), dict) else {}
                supplier_result = sync_supplier_to_crm(
                    api,
                    supplier,
                    mapping=mapping,
                    apply=apply,
                    assigned_by_id=assigned_by_id or None,
                )
                if supplier_key:
                    supplier_results[supplier_key] = supplier_result
            row = import_order(
                api,
                order,
                mapping=mapping,
                apply=apply,
                assigned_by_id=assigned_by_id,
                finance_user_id=finance_user_id,
                supplier_result=supplier_result,
                existing_item_id=existing_id,
                used_batch_ids=used_batch_ids,
            )
            if not apply:
                row["existing_item_id"] = existing_id
                row["would_action"] = "update" if existing_id else "create"
            rows.append(row)
        except Exception as exc:
            rows.append(
                {
                    "source_number": clean(order.get("number")),
                    "action": "blocked",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "contour": clean(order.get("procurement_contour_key")),
                    "stage_key": clean(order.get("procurement_stage_key")),
                }
            )
    return rows


def summarize(orders: list[dict[str, Any]], result_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "orders": len(orders),
        "contours": dict(Counter(clean(order.get("procurement_contour_key")) for order in orders)),
        "stages": dict(Counter(clean(order.get("procurement_stage_key")) for order in orders)),
        "currencies": dict(Counter(clean(order.get("currency")) for order in orders)),
        "bitrix_actions": dict(
            Counter(clean(row.get("would_action") or row.get("action")) for row in result_rows)
        ),
        "blocked": sum(1 for row in result_rows if clean(row.get("action")) == "blocked"),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    contours = parse_contour_keys(args.contours)
    env = load_env(args.env_file)
    onec_url = clean(env.get("ONEC_DATABASE_URL"))
    if not onec_url:
        raise SystemExit(f"ONEC_DATABASE_URL is not configured in {args.env_file}")
    orders = fetch_open_supplier_orders(
        onec_url,
        limit=args.limit,
        date_from=clean(args.date_from),
        date_to=clean(args.date_to),
        contours=contours,
        blank_contour_cargo_dropoff_only=bool(args.blank_contour_cargo_dropoff_only),
    )
    input_payload = {
        "contours": sorted(contours),
        "blank_contour_cargo_dropoff_only": bool(args.blank_contour_cargo_dropoff_only),
        "orders": orders,
    }
    write_json(args.input_json, input_payload)

    result_rows: list[dict[str, Any]] = []
    mode = "export-only"
    if not args.skip_bitrix:
        webhook_base = bitrix_webhook(args, env)
        if not webhook_base:
            raise SystemExit(
                f"Bitrix webhook is not configured. Set PROCUREMENT_BITRIX_WEBHOOK_URL "
                f"or BITRIX_BOX_WEBHOOK_BASE in {args.env_file}"
            )
        mapping = load_mapping(args.mapping_path)
        finance_user_id = (
            clean(args.finance_user_id)
            or clean(env.get("PROCUREMENT_CARGO_FINANCE_USER_ID"))
            or clean(env.get("PROCUREMENT_FINANCE_USER_ID"))
            or DEFAULT_CARGO_FINANCE_USER_ID
        )
        result_rows = run_bitrix_import(
            orders,
            webhook_base=webhook_base,
            mapping=mapping,
            apply=bool(args.apply),
            assigned_by_id=clean(args.assigned_by_id),
            finance_user_id=finance_user_id,
        )
        mode = "apply" if args.apply else "dry-run"

    result = {
        "mode": mode,
        "contours": sorted(contours),
        "blank_contour_cargo_dropoff_only": bool(args.blank_contour_cargo_dropoff_only),
        "input_json": str(args.input_json),
        "rows": result_rows,
        "summary": summarize(orders, result_rows),
    }
    write_json(args.result_path, result)
    print(json.dumps({"mode": mode, "summary": result["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
