from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError

ONEC_EMPTY_DATE = date(1753, 1, 1)
DEFAULT_HISTORY_MONTHS = 24
RECEIPT_MAPPING_UNRESOLVED = "receipt_mapping_unresolved"
SUPPLIER_ORDER_MAPPING_UNRESOLVED = "supplier_order_mapping_unresolved"


@dataclass(frozen=True)
class DocumentLineMapping:
    document_table: str
    line_table: str
    line_document_column: str
    line_nomenclature_column: str
    document_id_column: str = "_IDRRef"
    document_date_column: str = "_Date_Time"
    posted_column: str = "_Posted"
    marked_column: str = "_Marked"
    line_price_column: str = ""
    cargo_handoff_column: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DocumentLineMapping:
        return cls(
            document_table=_required_text(payload, "document_table"),
            line_table=_required_text(payload, "line_table"),
            line_document_column=_required_text(payload, "line_document_column"),
            line_nomenclature_column=_required_text(payload, "line_nomenclature_column"),
            document_id_column=str(payload.get("document_id_column") or "_IDRRef"),
            document_date_column=str(payload.get("document_date_column") or "_Date_Time"),
            posted_column=str(payload.get("posted_column") or "_Posted"),
            marked_column=str(payload.get("marked_column") or "_Marked"),
            line_price_column=str(payload.get("line_price_column") or ""),
            cargo_handoff_column=str(payload.get("cargo_handoff_column") or ""),
        )


def default_history_start(today: date | None = None, *, history_months: int) -> date:
    effective_today = today or date.today()
    return effective_today - timedelta(days=max(1, history_months) * 31)


def validate_warehouse_policy(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_warehouses = payload.get("warehouses")
    if not isinstance(raw_warehouses, list) or not raw_warehouses:
        raise ValueError("warehouse_policy_required")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_warehouses:
        if not isinstance(raw, Mapping):
            raise ValueError("warehouse_policy_item_must_be_object")
        code = str(raw.get("warehouse_code") or raw.get("code") or "").strip()
        if not code:
            raise ValueError("warehouse_code_required")
        if code in seen:
            raise ValueError(f"warehouse_code_duplicate:{code}")
        seen.add(code)
        result.append(
            {
                "warehouse_code": code,
                "sells_systematically": _bool(raw.get("sells_systematically"), default=True),
                "is_central": _bool(raw.get("is_central"), default=False),
                "is_defect_warehouse": _bool(raw.get("is_defect_warehouse"), default=False),
                "is_transit": _bool(raw.get("is_transit"), default=False),
                "is_non_systematic_sale": _bool(raw.get("is_non_systematic_sale"), default=False),
            }
        )
    return result


def normalize_manual_overrides(
    payload: Mapping[str, Any] | Sequence[Any] | None,
) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    if isinstance(payload, Mapping):
        raw_items = payload.get("items")
        if raw_items is None:
            raw_items = [
                {"nomenclature_code": code, **value}
                for code, value in payload.items()
                if isinstance(value, Mapping)
            ]
    else:
        raw_items = payload
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise ValueError("manual_overrides_must_be_list_or_object")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ValueError("manual_override_item_must_be_object")
        code = str(raw.get("nomenclature_code") or raw.get("NomenclatureCode") or "").strip()
        if not code:
            raise ValueError("manual_override_nomenclature_code_required")
        result[code] = dict(raw)
    return result


def normalize_manager_signals(
    payload: Mapping[str, Any] | Sequence[Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    if not payload:
        return {}
    raw_items: Any = payload.get("items") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise ValueError("manager_signals_must_be_list_or_object")
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ValueError("manager_signal_item_must_be_object")
        code = str(raw.get("nomenclature_code") or raw.get("NomenclatureCode") or "").strip()
        if not code:
            raise ValueError("manager_signal_nomenclature_code_required")
        result[code].append(dict(raw))
    return dict(result)


def build_assortment_lifecycle_fact_records(
    *,
    nomenclature_rows: Sequence[Mapping[str, Any]],
    supplier_order_rows: Sequence[Mapping[str, Any]],
    receipt_rows: Sequence[Mapping[str, Any]],
    warehouse_policy: Sequence[Mapping[str, Any]],
    manual_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    manager_signals: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    history_start: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items_by_key: dict[str, Mapping[str, Any]] = {}
    code_by_key: dict[str, str] = {}
    key_by_code: dict[str, str] = {}
    for row in nomenclature_rows:
        code = _clean(row.get("nomenclature_code") or row.get("code") or row.get("_Code"))
        if not code:
            continue
        key = _row_key(row) or code
        items_by_key[key] = row
        code_by_key[key] = code
        key_by_code[code] = key

    supplier_dates: dict[str, list[date]] = defaultdict(list)
    cargo_dates: dict[str, list[date]] = defaultdict(list)
    line_prices: dict[str, list[Decimal]] = defaultdict(list)
    for row in supplier_order_rows:
        key = _resolve_event_key(row, code_by_key, key_by_code)
        if not key:
            continue
        order_date = _date(row.get("order_date") or row.get("supplier_order_date"))
        cargo_date = _date(
            row.get("cargo_handoff_date")
            or row.get("cargo_dropoff_date")
            or row.get("supplier_order_cargo_handoff_date")
        )
        if order_date is not None:
            supplier_dates[key].append(order_date)
        if cargo_date is not None:
            cargo_dates[key].append(cargo_date)
        price = _decimal(
            row.get("line_price") or row.get("price") or row.get("item_value") or row.get("cost")
        )
        if price is not None:
            line_prices[key].append(price)

    receipt_dates: dict[str, list[date]] = defaultdict(list)
    for row in receipt_rows:
        key = _resolve_event_key(row, code_by_key, key_by_code)
        if not key:
            continue
        receipt_date = _date(row.get("receipt_date") or row.get("document_date"))
        if receipt_date is not None:
            receipt_dates[key].append(receipt_date)

    manual_overrides = manual_overrides or {}
    manager_signals = manager_signals or {}
    item_values = {
        key: _item_value(items_by_key[key], line_prices.get(key, ())) for key in items_by_key
    }
    group_values = [value for value in item_values.values() if value is not None]

    facts: list[dict[str, Any]] = []
    warnings_count: defaultdict[str, int] = defaultdict(int)
    for key, item in sorted(items_by_key.items(), key=lambda pair: code_by_key[pair[0]]):
        code = code_by_key[key]
        warnings: list[str] = []
        first_supplier_order_at = _min_date(supplier_dates.get(key, ()))
        event_dates = [*supplier_dates.get(key, ()), *receipt_dates.get(key, ())]
        if history_start is not None and _event_touches_history_start(
            event_dates,
            history_start,
        ):
            warnings.append("history_truncated")
            warnings_count["history_truncated"] += 1

        fact: dict[str, Any] = {
            "nomenclature_code": code,
            "name": _clean(item.get("name") or item.get("nomenclature_name")),
            "folder_path": _clean(item.get("folder_path") or item.get("folder")),
            "created_at": _json_date(_date(item.get("created_at"))),
            "first_supplier_order_at": _json_date(first_supplier_order_at),
            "supplier_order_cargo_handoff_dates": [
                _json_date(value) for value in sorted(set(cargo_dates.get(key, ())))
            ],
            "receipt_dates": [
                _json_date(value) for value in sorted(set(receipt_dates.get(key, ())))
            ],
            "has_need_signal": bool(manager_signals.get(code)),
            "warehouses": [dict(row) for row in warehouse_policy],
            "manager_need_signals": [dict(row) for row in manager_signals.get(code, ())],
            "warnings": warnings,
        }
        item_value = item_values.get(key)
        if item_value is not None:
            fact["expensive_item_value"] = _json_decimal(item_value)
            fact["expensive_group_values"] = [_json_decimal(value) for value in group_values]
        route_days = _route_days(cargo_dates.get(key, ()), receipt_dates.get(key, ()))
        if route_days is not None:
            fact["expensive_route_days"] = route_days

        override = manual_overrides.get(code)
        if override:
            fact.update(_manual_override_fields(override))
        facts.append(fact)

    summary = {
        "items": len(facts),
        "supplier_order_rows": len(supplier_order_rows),
        "receipt_rows": len(receipt_rows),
        "history_start": _json_date(history_start),
        "warnings": dict(warnings_count),
    }
    return facts, summary


def validate_document_line_mapping(engine: Engine, mapping: DocumentLineMapping) -> tuple[str, ...]:
    issues: list[str] = []
    table_columns: dict[str, set[str]] = {}
    for table in (mapping.document_table, mapping.line_table):
        columns = _table_columns(engine, table)
        table_columns[table] = columns
        if not columns:
            issues.append(f"table_missing:{table}")
    document_required = {
        mapping.document_id_column,
        mapping.document_date_column,
        mapping.posted_column,
        mapping.marked_column,
    }
    if mapping.cargo_handoff_column:
        document_required.add(mapping.cargo_handoff_column)
    for column in sorted(document_required - table_columns[mapping.document_table]):
        issues.append(f"column_missing:{mapping.document_table}.{column}")
    line_required = {mapping.line_document_column, mapping.line_nomenclature_column}
    if mapping.line_price_column:
        line_required.add(mapping.line_price_column)
    for column in sorted(line_required - table_columns[mapping.line_table]):
        issues.append(f"column_missing:{mapping.line_table}.{column}")
    return tuple(issues)


def fetch_onec_lifecycle_source_rows(
    engine: Engine,
    *,
    folder: str,
    history_start: date,
    supplier_mapping: DocumentLineMapping,
    receipt_mapping: DocumentLineMapping,
    limit: int = 5000,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    supplier_issues = validate_document_line_mapping(engine, supplier_mapping)
    if supplier_issues:
        raise ValueError(f"{SUPPLIER_ORDER_MAPPING_UNRESOLVED}: {', '.join(supplier_issues)}")
    receipt_issues = validate_document_line_mapping(engine, receipt_mapping)
    if receipt_issues:
        raise ValueError(f"{RECEIPT_MAPPING_UNRESOLVED}: {', '.join(receipt_issues)}")

    nomenclature_rows = _fetch_nomenclature_rows(engine, folder=folder, limit=limit)
    allowed_refs = {_clean(row.get("nomenclature_ref")) for row in nomenclature_rows}
    allowed_refs.discard("")
    if not allowed_refs:
        return [], [], []
    supplier_rows = _fetch_document_line_rows(
        engine,
        supplier_mapping,
        allowed_refs=allowed_refs,
        history_start=history_start,
        cargo_alias="cargo_handoff_date",
        value_alias="line_price",
    )
    receipt_rows = _fetch_document_line_rows(
        engine,
        receipt_mapping,
        allowed_refs=allowed_refs,
        history_start=history_start,
        cargo_alias="",
        value_alias="",
    )
    return nomenclature_rows, supplier_rows, receipt_rows


def _fetch_nomenclature_rows(engine: Engine, *, folder: str, limit: int) -> list[dict[str, Any]]:
    query = text(f"""
        SELECT TOP {max(1, min(limit, 50000))}
            CONVERT(varchar(34), item._IDRRef, 1) AS nomenclature_ref,
            CONVERT(varchar(34), item._ParentIDRRef, 1) AS parent_ref,
            NULLIF(LTRIM(RTRIM(item._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(item._Description)), N'') AS name,
            CAST(item._Folder AS int) AS folder_flag,
            item._Marked AS marked,
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
        rows = [dict(row) for row in conn.execute(query, {"folder_like": f"%{folder}%"}).mappings()]
    for row in rows:
        if not _clean(row.get("folder_path")):
            row["folder_path"] = folder
    return rows


def _fetch_document_line_rows(
    engine: Engine,
    mapping: DocumentLineMapping,
    *,
    allowed_refs: set[str],
    history_start: date,
    cargo_alias: str,
    value_alias: str,
) -> list[dict[str, Any]]:
    refs = sorted(allowed_refs)
    cargo_select = (
        f", doc.{_ident(mapping.cargo_handoff_column)} AS {cargo_alias}"
        if cargo_alias and mapping.cargo_handoff_column
        else ""
    )
    value_select = (
        f", line.{_ident(mapping.line_price_column)} AS {value_alias}"
        if value_alias and mapping.line_price_column
        else ""
    )
    query = text(f"""
        SELECT
            CONVERT(varchar(34), line.{_ident(mapping.line_nomenclature_column)}, 1)
                AS nomenclature_ref,
            doc.{_ident(mapping.document_date_column)} AS document_date
            {cargo_select}
            {value_select}
        FROM dbo.{_ident(mapping.line_table)} AS line WITH (NOLOCK)
        JOIN dbo.{_ident(mapping.document_table)} AS doc WITH (NOLOCK)
            ON doc.{_ident(mapping.document_id_column)} = line.{_ident(mapping.line_document_column)}
        WHERE doc.{_ident(mapping.marked_column)} = 0x00
          AND doc.{_ident(mapping.posted_column)} = 0x01
          AND doc.{_ident(mapping.document_date_column)} >= :history_start
          AND CONVERT(varchar(34), line.{_ident(mapping.line_nomenclature_column)}, 1) IN :refs
        ORDER BY doc.{_ident(mapping.document_date_column)}
        """).bindparams(bindparam("refs", expanding=True))
    with engine.connect() as conn:
        raw_rows = [
            dict(row)
            for row in conn.execute(
                query,
                {"history_start": history_start, "refs": refs},
            ).mappings()
        ]
    result: list[dict[str, Any]] = []
    for row in raw_rows:
        item = {
            "nomenclature_ref": _clean(row.get("nomenclature_ref")),
            "document_date": _json_date(_date(row.get("document_date"))),
        }
        if cargo_alias:
            item[cargo_alias] = _json_date(_date(row.get(cargo_alias)))
            item["order_date"] = item["document_date"]
        else:
            item["receipt_date"] = item["document_date"]
        if value_alias:
            value = _decimal(row.get(value_alias))
            if value is not None:
                item[value_alias] = _json_decimal(value)
        result.append(item)
    return result


def _table_columns(engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    for schema in ("dbo", None):
        try:
            columns = inspector.get_columns(table_name, schema=schema)
        except NoSuchTableError:
            continue
        except Exception:
            if schema is None:
                raise
            continue
        return {str(column["name"]) for column in columns}
    return set()


def _manual_override_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "manual_status",
        "manual_reason",
        "manual_approved_by",
        "manual_changed_at",
        "exclusive_min_stock_qty",
        "exclusive_review_period_days",
        "working_confirmed_by_folder_responsible",
        "manual_expensive_profile",
    }
    return {key: value for key, value in raw.items() if key in allowed}


def _item_value(item: Mapping[str, Any], prices: Iterable[Decimal]) -> Decimal | None:
    explicit = _decimal(
        item.get("expensive_item_value") or item.get("item_value") or item.get("cost")
    )
    if explicit is not None:
        return explicit
    values = list(prices)
    return values[-1] if values else None


def _route_days(cargo_values: Sequence[date], receipt_values: Sequence[date]) -> int | None:
    if not cargo_values or not receipt_values:
        return None
    first_cargo = min(cargo_values)
    receipts_after = [value for value in receipt_values if value >= first_cargo]
    if not receipts_after:
        return None
    return (min(receipts_after) - first_cargo).days


def _event_touches_history_start(values: Sequence[date], history_start: date) -> bool:
    return bool(values and min(values) <= history_start)


def _resolve_event_key(
    row: Mapping[str, Any],
    code_by_key: Mapping[str, str],
    key_by_code: Mapping[str, str],
) -> str:
    key = _row_key(row)
    if key in code_by_key:
        return key
    code = _clean(row.get("nomenclature_code") or row.get("code"))
    return key_by_code.get(code, "")


def _row_key(row: Mapping[str, Any]) -> str:
    return _clean(row.get("nomenclature_ref") or row.get("ref") or row.get("idrref"))


def _min_date(values: Sequence[date]) -> date | None:
    return min(values) if values else None


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


def _bool(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text_value = str(value).strip().casefold()
    if text_value in {"1", "true", "yes", "y", "да", "истина"}:
        return True
    if text_value in {"0", "false", "no", "n", "нет", "ложь"}:
        return False
    return default


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = _clean(payload.get(key))
    if not value:
        raise ValueError(f"{key}_required")
    return value


def _ident(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"unsafe_sql_identifier:{value}")
    return value


def _json_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_decimal(value: Decimal) -> str:
    return format(value, "f")
