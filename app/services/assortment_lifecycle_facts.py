from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError

from app.services.assortment_lifecycle import (
    DEMAND_WINDOW_LONG_DAYS,
    DEMAND_WINDOW_MEDIUM_DAYS,
    DEMAND_WINDOW_SHORT_DAYS,
)
from app.services.display_scope_policy import filter_display_scope_records

# Окна наблюдения спроса берём из самой формулы, чтобы сборщик фактов и
# формула не разъехались числами.
DEMAND_WINDOWS_DAYS = (
    DEMAND_WINDOW_SHORT_DAYS,
    DEMAND_WINDOW_MEDIUM_DAYS,
    DEMAND_WINDOW_LONG_DAYS,
)

ONEC_EMPTY_DATE = date(1753, 1, 1)
DEFAULT_HISTORY_MONTHS = 24
RECEIPT_MAPPING_UNRESOLVED = "receipt_mapping_unresolved"
SUPPLIER_ORDER_MAPPING_UNRESOLVED = "supplier_order_mapping_unresolved"
MAX_SQLSERVER_EXPANDING_REFS = 1800
FEATURE_SNAPSHOT_SCHEMA = "procurement_feature_snapshot.v1"
BASE_REQUIRED_FEATURE_FIELDS = ("subject_1c",)
DISPLAY_REQUIRED_FEATURE_FIELDS = ("subject_1c", "quality_raw", "model_compatibility")
DISPLAY_NAME_PREFIX_RE = re.compile(r"^\s*дисплей\s+для\s+(.+)$", re.IGNORECASE)
MATRIX_NAME_PREFIX_RE = re.compile(r"^\s*матриц[аы]\s+для\s+(.+)$", re.IGNORECASE)
PARENTHETICAL_RE = re.compile(r"\(([^()]*)\)")
DISPLAY_DESCRIPTION_MARKERS = (
    "в сборе",
    "тачскрин",
    "рамк",
    "черн",
    "бел",
    "розов",
    "золот",
    "сереб",
    "серый",
    "син",
    "красн",
)
GENERIC_DISPLAY_FOLDER_BRANDS = {"планшетов", "телефонов", "смартфонов"}
DISPLAY_SCOPE_MARKERS = ("диспле", "матриц")
ONEC_NOMENCLATURE_PROPERTY_ALIASES = {
    "Качество": "quality_raw",
    "Класс дисплея": "display_quality_raw",
    "Предмет": "subject_1c",
    "Категория": "category_1c",
    "Совместим с брендом": "brand_compatibility",
    "Совместим с моделью": "model_compatibility",
}


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
                "name": _clean(raw.get("name")),
                "role": _clean(raw.get("role")),
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
    first_sale_dates: Mapping[str, tuple[date, date]] | None = None,
    as_of: date | None = None,
    sales_window_totals: Mapping[str, Mapping[int, Decimal]] | None = None,
    days_in_sale_totals: Mapping[str, Mapping[int, Decimal]] | None = None,
    previous_statuses: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scope_result = filter_display_scope_records(nomenclature_rows)
    items_by_key: dict[str, Mapping[str, Any]] = {}
    code_by_key: dict[str, str] = {}
    key_by_code: dict[str, str] = {}
    for row in scope_result.included:
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

        card_created_at = _date(
            item.get("card_created_at") or item.get("onec_novelty_date") or item.get("created_at")
        )

        override = manual_overrides.get(code)
        override_fields = _manual_override_fields(override) if override else {}
        feature_item = {**dict(item), **override_fields} if override_fields else item

        fact: dict[str, Any] = {
            "nomenclature_code": code,
            "name": _clean(item.get("name") or item.get("nomenclature_name")),
            "folder_path": _clean(item.get("folder_path") or item.get("folder")),
            "short_name_1c": _clean(item.get("short_name_1c")),
            "additional_name_1c": _clean(item.get("additional_name_1c")),
            "vendor_sku_1c": _clean(item.get("vendor_sku_1c")),
            "created_at": _json_date(_date(item.get("created_at")) or card_created_at),
            "card_created_at": _json_date(card_created_at),
            "first_supplier_order_at": _json_date(first_supplier_order_at),
            "supplier_order_cargo_handoff_dates": [
                _json_date(value) for value in sorted(set(cargo_dates.get(key, ())))
            ],
            "receipt_dates": [
                _json_date(value) for value in sorted(set(receipt_dates.get(key, ())))
            ],
            # Дата первой реализации покупателю — вход в СП / Старт продаж
            # (решение 2026-08-02). None означает "продаж не было".
            "first_sale_at": _json_date(((first_sale_dates or {}).get(code) or (None, None))[0]),
            # Последняя продажа — вход в «Пенсию» (решение 2026-08-02).
            "last_sale_at": _json_date(((first_sale_dates or {}).get(code) or (None, None))[1]),
            # Дата, на которую собран факт: нужна правилу «Родился мёртвым»,
            # чтобы измерить, сколько карточка молчит. Берём конец окна
            # наблюдения, а не системные часы — иначе повторный расчёт того же
            # снимка дал бы другой результат.
            "as_of": _json_date(as_of),
            # Продажи по окнам 30/90/180 и дни наличия за те же окна — вход
            # переходов «Пошли продажи -> Растим -> Поддерживаем» по динамике
            # спроса. None означает «замера не было», 0 — «продаж не было».
            **_demand_window_fields(code, sales_window_totals, days_in_sale_totals),
            # Прошлый статус нужен гистерезису: плоская карточка остаётся там,
            # где стояла, и не дёргается между «Растим» и «Поддерживаем».
            "previous_status": (previous_statuses or {}).get(code) or None,
            "has_need_signal": bool(manager_signals.get(code)),
            "warehouses": [dict(row) for row in warehouse_policy],
            "manager_need_signals": [dict(row) for row in manager_signals.get(code, ())],
            "warnings": warnings,
        }
        item_value = item_values.get(key)
        fact.update(
            build_procurement_feature_snapshot_fields(
                feature_item,
                code=code,
                item_value=item_value,
                group_values=group_values,
                receipt_dates=receipt_dates.get(key, ()),
                has_need_signal=bool(manager_signals.get(code)),
            )
        )
        if item_value is not None:
            fact["expensive_item_value"] = _json_decimal(item_value)
            fact["expensive_group_values"] = [_json_decimal(value) for value in group_values]
        route_days = _route_days(cargo_dates.get(key, ()), receipt_dates.get(key, ()))
        if route_days is not None:
            fact["expensive_route_days"] = route_days

        if override_fields:
            fact.update(override_fields)
        facts.append(fact)

    summary = {
        "items": len(facts),
        "supplier_order_rows": len(supplier_order_rows),
        "receipt_rows": len(receipt_rows),
        "history_start": _json_date(history_start),
        "warnings": dict(warnings_count),
        "scope_policy": scope_result.audit,
    }
    return facts, summary


def build_procurement_feature_snapshot_fields(
    item: Mapping[str, Any],
    *,
    code: str,
    item_value: Decimal | None,
    group_values: Sequence[Decimal],
    receipt_dates: Sequence[date],
    has_need_signal: bool,
) -> dict[str, Any]:
    """Build the procurement feature snapshot from 1C card attributes and facts."""

    name = _clean(item.get("name") or item.get("nomenclature_name"))
    folder_path = _clean(item.get("folder_path") or item.get("folder"))
    subject_1c = _first_text(item, "subject_1c", "subject", "Предмет")
    if not subject_1c:
        subject_1c = _infer_subject_1c(name=name, folder_path=folder_path)
    item_tags = _text_list(_first_value(item, "item_tags", "tags", "tag", "Тэг"))
    quality_raw = _first_text(item, "quality_raw", "quality", "Качество")
    quality_normalized = _normalize_quality(quality_raw)
    characteristic_values = _characteristic_values(
        _first_value(item, "characteristic_values", "characteristics", "Характеристики")
    )
    model_compatibility = _first_text(
        item,
        "model_compatibility",
        "compatible_model",
        "СовместимСМоделью",
    )
    if not model_compatibility:
        model_compatibility = _infer_display_model_compatibility(name)
    brand_compatibility = _first_text(
        item,
        "brand_compatibility",
        "compatible_brand",
        "СовместимСБрендом",
    )
    if not brand_compatibility:
        brand_compatibility = _infer_brand_compatibility(
            folder_path=folder_path,
            model_compatibility=model_compatibility,
        )
    feature_fields: dict[str, Any] = {
        "feature_snapshot_schema": FEATURE_SNAPSHOT_SCHEMA,
        "product_ref": _clean(item.get("product_ref") or item.get("nomenclature_ref")),
        "article": _first_text(item, "article", "sku", "SKU", "Артикул"),
        "kind_1c": _first_text(item, "kind_1c", "nomenclature_kind", "ВидНоменклатуры"),
        "subject_1c": subject_1c,
        "category_1c": _first_text(item, "category_1c", "category", "Категория"),
        "item_tags": item_tags,
        "brand_compatibility": brand_compatibility,
        "model_compatibility": model_compatibility,
        "quality_raw": quality_raw,
        "quality_normalized": quality_normalized,
        "characteristic_values": characteristic_values,
        "price_segment": _price_segment(item_value, group_values),
    }
    required_fields = _required_feature_fields(folder_path)
    missing_required_attributes = [
        field_name for field_name in required_fields if not feature_fields.get(field_name)
    ]
    data_quality_score = _data_quality_score(required_fields, missing_required_attributes)
    feature_fields.update(
        {
            "missing_required_attributes": missing_required_attributes,
            "data_quality_score": data_quality_score,
            "future_ka_mapping_status": (
                "needs_mapping" if missing_required_attributes else "ready"
            ),
        }
    )
    feature_fields.update(
        _calculation_unit_fields(
            item,
            code=code,
            folder_path=folder_path,
            feature_fields=feature_fields,
            data_quality_score=data_quality_score,
        )
    )
    feature_fields.update(
        _demand_method_fields(
            item,
            receipt_dates=receipt_dates,
            has_need_signal=has_need_signal,
            data_quality_score=data_quality_score,
        )
    )
    return feature_fields


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


def fetch_first_sale_dates(
    engine: Engine,
    *,
    nomenclature_codes: Sequence[str],
) -> dict[str, tuple[date, date]]:
    """Дата первой реализации покупателю по каждому коду номенклатуры.

    Окно истории намеренно НЕ применяется: факт «продажи начались» не должен
    исчезать оттого, что первая продажа вышла за горизонт сбора остальных
    фактов. Запрос агрегатный (MIN по коду), поэтому дешёвый даже без окна.
    """
    codes = tuple(code for code in {_clean(value) for value in nomenclature_codes} if code)
    if not codes:
        return {}
    query = text("""
        SELECT NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
               MIN(sale._Date_Time) AS first_sale_at,
               MAX(sale._Date_Time) AS last_sale_at
        FROM dbo._Document203 AS sale WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS sale_line WITH (NOLOCK)
            ON sale_line._Document203_IDRRef = sale._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = sale_line._Fld4974RRef
        WHERE sale._Marked = 0x00 AND sale._Posted = 0x01
          AND sale_line._Fld4971 > 0
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """).bindparams(bindparam("codes", expanding=True))
    result: dict[str, tuple[date, date]] = {}
    with engine.connect() as conn:
        for chunk in _chunks(list(codes), MAX_SQLSERVER_EXPANDING_REFS):
            for row in conn.execute(query, {"codes": chunk}).mappings():
                code = _clean(row.get("nomenclature_code"))
                first = _date(row.get("first_sale_at"))
                last = _date(row.get("last_sale_at"))
                if code and first is not None and last is not None:
                    result[code] = (first, last)
    return result


def _demand_window_fields(
    code: str,
    sales_window_totals: Mapping[str, Mapping[int, Decimal]] | None,
    days_in_sale_totals: Mapping[str, Mapping[int, Decimal]] | None,
) -> dict[str, Any]:
    sales = (sales_window_totals or {}).get(code) or {}
    days_in_sale = (days_in_sale_totals or {}).get(code) or {}
    fields: dict[str, Any] = {}
    for suffix, window_days in (
        ("short", DEMAND_WINDOW_SHORT_DAYS),
        ("medium", DEMAND_WINDOW_MEDIUM_DAYS),
        ("long", DEMAND_WINDOW_LONG_DAYS),
    ):
        fields[f"sales_qty_{suffix}"] = _json_decimal(sales.get(window_days))
        fields[f"days_in_sale_{suffix}"] = _json_decimal(days_in_sale.get(window_days))
    return fields


def fetch_sales_window_totals(
    engine: Engine,
    *,
    nomenclature_codes: Sequence[str],
    date_to: date,
    windows_days: Sequence[int] = DEMAND_WINDOWS_DAYS,
) -> dict[str, dict[int, Decimal]]:
    """Продано штук по каждому коду за окна 30/90/180 дней.

    Брутто, без вычета возвратов — тот же «спрос брутто», что уже принят для
    расчёта количества заказа (возврат «не понадобился» означает, что спрос
    был). Нужен формуле статусов, чтобы «Растим» и «Поддерживаем» определялись
    динамикой спроса, а не числом поступлений.
    """
    codes = tuple(code for code in {_clean(value) for value in nomenclature_codes} if code)
    windows = tuple(sorted({int(value) for value in windows_days if int(value) > 0}))
    if not codes or not windows:
        return {}
    window_starts = {
        window_days: datetime.combine(
            date_to - timedelta(days=window_days - 1), datetime.min.time()
        )
        for window_days in windows
    }
    window_columns = ",\n".join(
        f"               SUM(CASE WHEN sale._Date_Time >= :window_from_{window_days} "
        f"THEN CAST(sale_line._Fld4971 AS decimal(18, 3)) ELSE 0 END) AS window_{window_days}"
        for window_days in windows
    )
    query = text(f"""
        SELECT NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
{window_columns}
        FROM dbo._Document203 AS sale WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS sale_line WITH (NOLOCK)
            ON sale_line._Document203_IDRRef = sale._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = sale_line._Fld4974RRef
        WHERE sale._Marked = 0x00 AND sale._Posted = 0x01
          AND sale_line._Fld4971 > 0
          AND sale._Date_Time >= :window_from_min
          AND sale._Date_Time < :date_to
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """).bindparams(bindparam("codes", expanding=True))
    params: dict[str, Any] = {
        "date_to": datetime.combine(date_to + timedelta(days=1), datetime.min.time()),
        "window_from_min": min(window_starts.values()),
    }
    for window_days, window_from in window_starts.items():
        params[f"window_from_{window_days}"] = window_from
    # Нули проставляем заранее: код без единой продажи в окне должен вернуть 0,
    # а не «нет данных» — иначе формула не отличит тишину от отсутствия замера.
    result: dict[str, dict[int, Decimal]] = {
        code: {window_days: Decimal("0") for window_days in windows} for code in codes
    }
    with engine.connect() as conn:
        for chunk in _chunks(list(codes), MAX_SQLSERVER_EXPANDING_REFS):
            for row in conn.execute(query, {**params, "codes": chunk}).mappings():
                code = _clean(row.get("nomenclature_code"))
                if code not in result:
                    continue
                for window_days in windows:
                    qty = _decimal(row.get(f"window_{window_days}"))
                    result[code][window_days] = qty if qty is not None else Decimal("0")
    return result


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


def enrich_nomenclature_rows_with_product_snapshot(
    engine: Engine,
    nomenclature_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not nomenclature_rows:
        return []
    table_names = set(inspect(engine).get_table_names())
    if "product" not in table_names:
        return [dict(row) for row in nomenclature_rows]

    lookup_keys = sorted(
        {
            _clean(
                row.get("nomenclature_code")
                or row.get("code")
                or row.get("_Code")
                or row.get("article")
            )
            for row in nomenclature_rows
            if _clean(
                row.get("nomenclature_code")
                or row.get("code")
                or row.get("_Code")
                or row.get("article")
            )
        }
    )
    if not lookup_keys:
        return [dict(row) for row in nomenclature_rows]

    product_columns = _table_columns(engine, "product")
    optional_columns = {
        "fact_sku",
        "planned_sku",
        "code_1c",
        "info_system_code",
        "brand",
        "category",
        "subject",
        "subject_1c",
        "vid_nomenklatury",
        "vid_nomenklatury_1c",
        "quality_raw",
        "display_quality_raw",
        "quality",
        "display_quality",
        "display_type",
        "display_construction",
        "display_refresh_rate_hz",
        "display_screen_kit",
        "display_has_frame",
        "display_has_touch",
        "display_has_ic_pad",
        "display_has_binding_no_solder",
        "display_backlight",
        "display_matrix_tags",
        "display_diagonal",
        "display_resolution",
    }
    select_parts = ["p.id", "p.article", "p.name"]
    for column_name in sorted(optional_columns):
        if column_name in product_columns:
            select_parts.append(f"p.{column_name}")
        else:
            select_parts.append(f"NULL AS {column_name}")
    query = text(f"""
        SELECT {", ".join(select_parts)}
        FROM product AS p
        WHERE p.article IN :keys_article
           OR p.fact_sku IN :keys_fact_sku
           OR p.code_1c IN :keys_code_1c
           OR p.info_system_code IN :keys_info_system
        """).bindparams(
        bindparam("keys_article", expanding=True),
        bindparam("keys_fact_sku", expanding=True),
        bindparam("keys_code_1c", expanding=True),
        bindparam("keys_info_system", expanding=True),
    )
    product_rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for keys_chunk in _chunks(lookup_keys, MAX_SQLSERVER_EXPANDING_REFS):
            product_rows.extend(
                dict(row)
                for row in conn.execute(
                    query,
                    {
                        "keys_article": keys_chunk,
                        "keys_fact_sku": keys_chunk,
                        "keys_code_1c": keys_chunk,
                        "keys_info_system": keys_chunk,
                    },
                ).mappings()
            )

    compatibility_by_product_id = _fetch_product_compatibility_values(
        engine,
        [int(row["id"]) for row in product_rows if row.get("id") is not None],
        table_names=table_names,
    )
    product_by_key: dict[str, dict[str, Any]] = {}
    for product in product_rows:
        product_id = int(product["id"])
        overlay = _product_feature_overlay(
            product,
            compatibility_by_product_id.get(product_id, ()),
        )
        for field_name in ("code_1c", "article", "fact_sku", "info_system_code"):
            key = _clean(product.get(field_name))
            if key and key not in product_by_key:
                product_by_key[key] = overlay

    enriched_rows: list[dict[str, Any]] = []
    for row in nomenclature_rows:
        item = dict(row)
        lookup_key = _clean(
            item.get("nomenclature_code")
            or item.get("code")
            or item.get("_Code")
            or item.get("article")
        )
        overlay = product_by_key.get(lookup_key)
        if overlay:
            for key, value in overlay.items():
                if value not in (None, "", [], {}) and item.get(key) in (None, "", [], {}):
                    item[key] = value
        enriched_rows.append(item)
    return enriched_rows


def _fetch_nomenclature_rows(engine: Engine, *, folder: str, limit: int) -> list[dict[str, Any]]:
    folder_patterns = _folder_like_patterns(folder)
    folder_conditions: list[str] = []
    query_params: dict[str, str] = {}
    for index, pattern in enumerate(folder_patterns):
        param_name = f"folder_like_{index}"
        folder_conditions.append(
            f"(parent._Description LIKE :{param_name} OR item._Description LIKE :{param_name})"
        )
        query_params[param_name] = pattern
    folder_where = " OR ".join(folder_conditions) or "1 = 0"

    query = text(f"""
        SELECT TOP {max(1, min(limit, 50000))}
            CONVERT(varchar(34), item._IDRRef, 1) AS nomenclature_ref,
            CONVERT(varchar(34), item._ParentIDRRef, 1) AS parent_ref,
            NULLIF(LTRIM(RTRIM(item._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld836 AS nvarchar(max)))), N'') AS article,
            NULLIF(LTRIM(RTRIM(item._Description)), N'') AS name,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld847 AS nvarchar(max)))), N'') AS short_name_1c,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld8858 AS nvarchar(max)))), N'') AS additional_name_1c,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld9945 AS nvarchar(max)))), N'') AS vendor_sku_1c,
            item._Fld9840 AS card_created_at,
            CAST(item._Folder AS int) AS folder_flag,
            item._Marked AS marked,
            parent._Description AS folder_path
        FROM dbo._Reference62 AS item WITH (NOLOCK)
        LEFT JOIN dbo._Reference62 AS parent WITH (NOLOCK)
            ON parent._IDRRef = item._ParentIDRRef
        WHERE item._Marked = 0x00
          AND item._Fld836 IS NOT NULL
          AND ({folder_where})
        ORDER BY item._Code
        """)
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(query, query_params).mappings()]
    properties_by_ref = _fetch_nomenclature_property_rows(
        engine,
        refs=[_clean(row.get("nomenclature_ref")) for row in rows],
        property_aliases=ONEC_NOMENCLATURE_PROPERTY_ALIASES,
    )
    for row in rows:
        if not _clean(row.get("folder_path")):
            row["folder_path"] = folder
        property_values = properties_by_ref.get(_clean(row.get("nomenclature_ref")), {})
        for key, value in property_values.items():
            if value and not _clean(row.get(key)):
                row[key] = value
    return rows


def _fetch_nomenclature_property_rows(
    engine: Engine,
    *,
    refs: Sequence[str],
    property_aliases: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    ref_values = [ref for ref in refs if ref]
    property_names = [name for name in property_aliases if name]
    if not ref_values or not property_names:
        return {}

    query = text("""
        WITH latest_props AS (
            SELECT
                CONVERT(varchar(34), r._IDRRef, 1) AS nomenclature_ref,
                LTRIM(RTRIM(CAST(p._Fld8930 AS nvarchar(max)))) AS property_name,
                LTRIM(RTRIM(CAST(p._Fld8934 AS nvarchar(max)))) AS property_value,
                ROW_NUMBER() OVER (
                    PARTITION BY r._IDRRef, p._Fld8930
                    ORDER BY p._Fld8931 DESC
                ) AS rn
            FROM dbo._InfoRg8928 AS p WITH (NOLOCK)
            JOIN dbo._Reference62 AS r WITH (NOLOCK)
                ON r._IDRRef = p._Fld8929RRef
            WHERE CONVERT(varchar(34), r._IDRRef, 1) IN :refs
              AND LTRIM(RTRIM(CAST(p._Fld8930 AS nvarchar(max)))) IN :property_names
        )
        SELECT nomenclature_ref, property_name, property_value
        FROM latest_props
        WHERE rn = 1
          AND property_value IS NOT NULL
          AND LTRIM(RTRIM(property_value)) <> ''
        """).bindparams(
        bindparam("refs", expanding=True),
        bindparam("property_names", expanding=True),
    )

    result: dict[str, dict[str, str]] = {}
    with engine.connect() as conn:
        for refs_chunk in _chunks(ref_values, MAX_SQLSERVER_EXPANDING_REFS):
            rows = conn.execute(
                query,
                {
                    "refs": refs_chunk,
                    "property_names": property_names,
                },
            ).mappings()
            for row in rows:
                alias = property_aliases.get(_clean(row.get("property_name")))
                value = _clean(row.get("property_value"))
                ref = _clean(row.get("nomenclature_ref"))
                if ref and alias and value:
                    result.setdefault(ref, {})[alias] = value
    return result


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
    raw_rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for refs_chunk in _chunks(refs, MAX_SQLSERVER_EXPANDING_REFS):
            raw_rows.extend(
                dict(row)
                for row in conn.execute(
                    query,
                    {"history_start": history_start, "refs": refs_chunk},
                ).mappings()
            )
    raw_rows.sort(key=lambda row: row.get("document_date") or ONEC_EMPTY_DATE)
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


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _manual_override_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "manual_status",
        "manual_reason",
        "manual_approved_by",
        "manual_changed_at",
        "commercial_marks",
        "exclusive_kind",
        "exclusive_confidence",
        "exclusive_checked_at",
        "exclusive_review_at",
        "exclusive_reason",
        "exclusive_approved_by",
        "exclusive_evidence_refs",
        "exclusive_min_stock_qty",
        "exclusive_review_period_days",
        "working_confirmed_by_folder_responsible",
        "analog_winner_confirmed_by_folder_responsible",
        "manual_expensive_profile",
    }
    fields = {key: value for key, value in raw.items() if key in allowed}
    manual_status = _clean(fields.get("manual_status")).casefold()
    if manual_status == "exclusive":
        fields.pop("manual_status", None)
        fields["commercial_marks"] = _with_commercial_mark(
            fields.get("commercial_marks"),
            "exclusive",
        )
        if not fields.get("exclusive_reason") and fields.get("manual_reason"):
            fields["exclusive_reason"] = fields["manual_reason"]
        if not fields.get("exclusive_approved_by") and fields.get("manual_approved_by"):
            fields["exclusive_approved_by"] = fields["manual_approved_by"]
        if not fields.get("exclusive_checked_at") and fields.get("manual_changed_at"):
            fields["exclusive_checked_at"] = fields["manual_changed_at"]
    return fields


def _with_commercial_mark(value: Any, mark: str) -> list[str]:
    if value in (None, ""):
        values: list[str] = []
    elif isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, Sequence):
        values = [_clean(part) for part in value if _clean(part)]
    else:
        values = []
    if mark not in values:
        values.append(mark)
    return values


def _fetch_product_compatibility_values(
    engine: Engine,
    product_ids: Sequence[int],
    *,
    table_names: set[str],
) -> dict[int, tuple[str, ...]]:
    if not product_ids or "productcompatibility" not in table_names:
        return {}
    query = text("""
        SELECT product_id, value
        FROM productcompatibility
        WHERE product_id IN :product_ids
        ORDER BY product_id, source, value
        """).bindparams(bindparam("product_ids", expanding=True))
    values_by_product_id: dict[int, list[str]] = defaultdict(list)
    with engine.connect() as conn:
        for ids_chunk in _chunks(sorted(set(product_ids)), MAX_SQLSERVER_EXPANDING_REFS):
            for row in conn.execute(query, {"product_ids": ids_chunk}).mappings():
                product_id = int(row["product_id"])
                value = _clean(row.get("value"))
                if value and value not in values_by_product_id[product_id]:
                    values_by_product_id[product_id].append(value)
    return {product_id: tuple(values) for product_id, values in values_by_product_id.items()}


def _product_feature_overlay(
    product: Mapping[str, Any],
    compatibility_values: Sequence[str],
) -> dict[str, Any]:
    characteristic_values = _product_characteristic_values(product)
    compatibility_text = " / ".join(value for value in compatibility_values if value)
    brand_compatibility = _clean(product.get("brand")) or _compatibility_brand(compatibility_values)
    item_tags = _text_list(product.get("display_matrix_tags"))
    return {
        "product_ref": _clean(product.get("info_system_code")),
        "article": _clean(product.get("article")),
        "kind_1c": _clean(product.get("vid_nomenklatury_1c") or product.get("vid_nomenklatury")),
        "subject_1c": _clean(product.get("subject_1c") or product.get("subject")),
        "category_1c": _clean(product.get("category")),
        "item_tags": item_tags,
        "brand_compatibility": brand_compatibility,
        "model_compatibility": compatibility_text,
        "quality_raw": _clean(
            product.get("quality_raw")
            or product.get("display_quality_raw")
            or product.get("display_quality")
            or product.get("quality")
        ),
        "quality_normalized": _clean(product.get("display_quality") or product.get("quality")),
        "characteristic_values": characteristic_values,
    }


def _product_characteristic_values(product: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "display_type",
        "display_construction",
        "display_refresh_rate_hz",
        "display_screen_kit",
        "display_has_frame",
        "display_has_touch",
        "display_has_ic_pad",
        "display_has_binding_no_solder",
        "display_backlight",
        "display_diagonal",
        "display_resolution",
    )
    result: dict[str, Any] = {}
    for field_name in fields:
        value = product.get(field_name)
        if value not in (None, ""):
            result[field_name] = value
    return result


def _compatibility_brand(values: Sequence[str]) -> str:
    for value in values:
        text_value = _clean(value)
        if text_value:
            return text_value.split(" ", 1)[0]
    return ""


def _calculation_unit_fields(
    item: Mapping[str, Any],
    *,
    code: str,
    folder_path: str,
    feature_fields: Mapping[str, Any],
    data_quality_score: str,
) -> dict[str, Any]:
    manual_level = _first_text(
        item,
        "calculation_unit_level",
        "unit_level",
        "procurement_calculation_unit_level",
    )
    manual_key = _first_text(item, "calculation_unit_key", "unit_key")
    analog_group_id = _first_text(item, "analog_group_id", "analog_group")
    store_need_key = _first_text(item, "store_need_key", "ПотребностьМагазина")
    if manual_level:
        unit_level = manual_level
        unit_source = "manual"
        unit_key = manual_key or code
        reason = "Расчетная единица задана вручную."
    elif analog_group_id:
        unit_level = "analog_group"
        unit_source = "1c_or_manual_properties"
        unit_key = analog_group_id
        reason = "У товара есть группа аналогов/замен, спрос считаем по группе."
    elif feature_fields.get("subject_1c") and feature_fields.get("item_tags"):
        unit_level = "subject_tag"
        unit_source = "1c_properties"
        unit_key = _unit_key(
            (
                str(feature_fields.get("subject_1c") or ""),
                *[str(tag) for tag in feature_fields.get("item_tags") or []],
            )
        )
        reason = "Есть надежные предмет и tag, используем их как рабочую группу."
    elif (
        feature_fields.get("quality_normalized")
        or feature_fields.get("characteristic_values")
        or feature_fields.get("model_compatibility")
    ):
        unit_level = "property_group"
        unit_source = "1c_properties"
        unit_key = _unit_key(
            (
                folder_path,
                str(feature_fields.get("subject_1c") or ""),
                str(feature_fields.get("quality_normalized") or ""),
                str(feature_fields.get("brand_compatibility") or ""),
                str(feature_fields.get("model_compatibility") or ""),
                json.dumps(
                    feature_fields.get("characteristic_values") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        reason = "Свойства товара влияют на поставщика, риск или взаимозаменяемость."
    elif store_need_key:
        unit_level = "store_need"
        unit_source = "manager_or_store_need"
        unit_key = store_need_key
        reason = "Строка пришла из отдельной потребности магазина/клиента."
    else:
        unit_level = "sku"
        unit_source = "1c_nomenclature"
        unit_key = code
        reason = "Надежной группы выше SKU пока нет, считаем по конкретной номенклатуре."
    return {
        "calculation_unit_level": unit_level,
        "calculation_unit_key": unit_key,
        "calculation_unit_source": unit_source,
        "calculation_unit_confidence": data_quality_score,
        "calculation_unit_reason": reason,
    }


def _demand_method_fields(
    item: Mapping[str, Any],
    *,
    receipt_dates: Sequence[date],
    has_need_signal: bool,
    data_quality_score: str,
) -> dict[str, Any]:
    manual_method = _first_text(item, "demand_method_code", "demand_method")
    if manual_method:
        return {
            "demand_method_code": manual_method,
            "demand_method_reason": "Метод спроса задан вручную.",
            "demand_method_confidence": data_quality_score,
        }
    manual_status = _clean(item.get("manual_status")).casefold()
    if manual_status in {"nonliquid", "do_not_order", "replace_candidate"}:
        return {
            "demand_method_code": "manual_review",
            "demand_method_reason": "Есть ручной стоп или статус, обычную формулу не применяем.",
            "demand_method_confidence": "0.00",
        }
    if has_need_signal:
        return {
            "demand_method_code": "store_need",
            "demand_method_reason": "Есть отдельная потребность менеджера/магазина.",
            "demand_method_confidence": data_quality_score,
        }
    if len(set(receipt_dates)) >= 3:
        return {
            "demand_method_code": "available_days_average",
            "demand_method_reason": "Есть повторные поступления, можно считать среднюю по доступным дням.",
            "demand_method_confidence": data_quality_score,
        }
    return {
        "demand_method_code": "manual_review",
        "demand_method_reason": "Истории пока мало для безопасного автозаказа.",
        "demand_method_confidence": "0.50" if data_quality_score != "0.00" else "0.00",
    }


def _required_feature_fields(folder_path: str) -> tuple[str, ...]:
    if _is_display_scope_text(folder_path):
        return DISPLAY_REQUIRED_FEATURE_FIELDS
    return BASE_REQUIRED_FEATURE_FIELDS


def _infer_subject_1c(*, name: str, folder_path: str) -> str:
    text_value = f"{folder_path} {name}".casefold()
    if _is_display_scope_text(text_value):
        return "дисплей"
    return ""


def _infer_display_model_compatibility(name: str) -> str:
    match = DISPLAY_NAME_PREFIX_RE.match(name) or MATRIX_NAME_PREFIX_RE.match(name)
    if not match:
        return ""
    model_value = PARENTHETICAL_RE.sub(
        lambda match_value: (
            ""
            if _is_display_description_parenthetical(match_value.group(1))
            else match_value.group(0)
        ),
        match.group(1),
    )
    return _collapse_spaces(model_value).strip(" -/,")


def _is_display_description_parenthetical(value: str) -> bool:
    normalized = value.casefold()
    return any(marker in normalized for marker in DISPLAY_DESCRIPTION_MARKERS)


def _infer_brand_compatibility(*, folder_path: str, model_compatibility: str) -> str:
    folder_parts = [part.strip() for part in folder_path.split("/") if part.strip()]
    for part in reversed(folder_parts):
        marker = "дисплеи для "
        normalized = part.casefold()
        if marker in normalized:
            brand = part[normalized.index(marker) + len(marker) :].strip()
            if brand and brand.casefold() not in GENERIC_DISPLAY_FOLDER_BRANDS:
                return brand
    return model_compatibility.split(" ", 1)[0] if model_compatibility else ""


def _is_display_scope_text(value: str) -> bool:
    normalized = value.casefold()
    return any(marker in normalized for marker in DISPLAY_SCOPE_MARKERS)


def _folder_like_patterns(folder: str) -> tuple[str, ...]:
    folder_value = _clean(folder)
    if not folder_value:
        return ()
    values = [folder_value]
    if _is_display_scope_text(folder_value):
        values.append("Матриц")
    patterns: list[str] = []
    for value in values:
        pattern = f"%{value}%"
        if pattern not in patterns:
            patterns.append(pattern)
    return tuple(patterns)


def _data_quality_score(required_fields: Sequence[str], missing_fields: Sequence[str]) -> str:
    if not required_fields:
        return "1.00"
    score = max(0, len(required_fields) - len(missing_fields)) / len(required_fields)
    return f"{score:.2f}"


def _price_segment(item_value: Decimal | None, group_values: Sequence[Decimal]) -> str:
    values = sorted(value for value in group_values if value is not None)
    if item_value is None or not values:
        return ""
    total = len(values)
    lower_idx = max(0, ceil(total * Decimal("0.25")) - 1)
    mid_idx = max(0, ceil(total * Decimal("0.50")) - 1)
    upper_idx = max(0, ceil(total * Decimal("0.75")) - 1)
    if item_value <= values[lower_idx]:
        return "economy"
    if item_value <= values[mid_idx]:
        return "mid_low"
    if item_value <= values[upper_idx]:
        return "mid_high"
    return "premium"


def _normalize_quality(value: str) -> str:
    normalized = value.strip().casefold().replace("-", " ").replace("_", " ")
    if not normalized:
        return ""
    compact = normalized.replace(" ", "")
    if compact in {"orig100", "original", "ориг100", "оригинал"}:
        return "original"
    if "soft" in normalized and "oled" in normalized:
        return "soft_oled"
    if "hard" in normalized and "oled" in normalized:
        return "hard_oled"
    if compact in {"incell", "inсell"}:
        return "incell"
    return "_".join(normalized.split())


def _characteristic_values(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            _clean(key): _clean(item_value)
            for key, item_value in value.items()
            if _clean(key) and _clean(item_value)
        }
    return {}


def _first_value(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return None


def _first_text(item: Mapping[str, Any], *names: str) -> str:
    return _clean(_first_value(item, *names))


def _text_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_clean(part) for part in value if _clean(part)]
    return []


def _unit_key(parts: Sequence[str]) -> str:
    return "|".join(part.strip().casefold() for part in parts if part.strip())


def _collapse_spaces(value: str) -> str:
    return " ".join(value.split())


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


def _json_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
