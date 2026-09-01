#!/usr/bin/env python3
"""Dry-run/apply import of 1C ЗаказПоставщику into Bitrix procurement smart-process."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.ensure_expertise_bitrix_process as bitrix_setup  # noqa: E402
from app.services.procurement_supplier_crm import (  # noqa: E402
    build_procurement_order_bitrix_fields,
    clean_string,
    sync_supplier_to_crm,
)
from scripts.ensure_procurement_bitrix_process import (  # noqa: E402
    DEFAULT_ENV_FILE,
    DEFAULT_MAPPING_PATH,
    load_env,
)

DEFAULT_INPUT_PATH = REPO_ROOT / "build/bitrix/onec_supplier_orders_input.json"
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/onec_supplier_order_import_result.json"
DEFAULT_CARGO_FINANCE_USER_ID = "130746"


class BitrixRestApi:
    def __init__(self, webhook_base: str) -> None:
        self.webhook_base = webhook_base

    def call(self, method: str, params: Any = None) -> Any:
        return bitrix_setup.bitrix_call(self.webhook_base, method, params or {})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--input-json", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--assigned-by-id", default="")
    parser.add_argument(
        "--finance-user-id",
        default="",
        help="Bitrix user id for cargo payment task; defaults to env or Karina Avakyan 130746.",
    )
    parser.add_argument(
        "--supplier-conflict-mode",
        choices=("create_card_with_blocker", "block_import"),
        default="create_card_with_blocker",
    )
    parser.add_argument("--apply", action="store_true", help="Write CRM/Bitrix changes.")
    return parser.parse_args(argv)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_mapping(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    return payload if isinstance(payload, dict) else {}


def load_orders(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("orders") or payload.get("rows") or [payload]
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def iso_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return clean_string(value)


def decimal_string(value: Any) -> str:
    raw = clean_string(value).replace(" ", "").replace(",", ".")
    if not raw:
        return ""
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        return ""
    return format(amount.normalize(), "f")


def amount_rub_value(order: dict[str, Any]) -> str:
    return decimal_string(
        order.get("amount_rub")
        or order.get("open_amount_rub")
        or order.get("amountRub")
        or order.get("СуммаРуб")
    )


def order_currency(order: dict[str, Any]) -> str:
    return clean_string(order.get("currency") or order.get("Валюта"))


def is_rub_currency(value: Any) -> bool:
    return clean_string(value).casefold().replace(" ", "") in {
        "rub",
        "rur",
        "руб",
        "руб.",
        "рубль",
        "рубли",
    }


def formatted_amount(order: dict[str, Any]) -> str:
    amount = decimal_string(order.get("amount") or order.get("Сумма"))
    currency = order_currency(order)
    if not amount or not currency:
        return ""
    amount_text = f"{Decimal(amount):,.2f}".replace(",", " ").rstrip("0").rstrip(".")
    return f"{amount_text} {currency}"


def first_order_value(order: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = order.get(key)
        if clean_string(value):
            return value
    return None


def cargo_dropoff_value(order: dict[str, Any]) -> Any:
    return first_order_value(order, "cargo_dropoff_date", "Сдача в карго")


def payment_date_value(order: dict[str, Any]) -> Any:
    return first_order_value(order, "payment_date", "Оплата")


def compact_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    raw = clean_string(value)
    if not raw:
        return ""
    normalized = raw.removesuffix("Z")
    try:
        return datetime.fromisoformat(normalized).strftime("%Y%m%d")
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    digits = re.sub(r"\D+", "", raw)
    return digits[:8] if len(digits) >= 8 else ""


def base_generated_batch_id(order: dict[str, Any]) -> str:
    explicit = clean_string(order.get("pilot_batch_id") or order.get("batch_id"))
    if explicit:
        return explicit
    cargo_date = compact_date(cargo_dropoff_value(order))
    number = source_number(order)
    if not cargo_date or not number:
        return ""
    return f"CARGO-{cargo_date}-{number}"


def order_title_prefix(order: dict[str, Any]) -> str:
    contour = clean_string(order.get("procurement_contour") or order.get("КонтурЗакупки"))
    compact = contour.casefold().replace(" ", "").replace("_", "")
    if compact in {"cargo", "карго"}:
        return "Карго"
    if compact in {"вэдимпорт", "vedimport"}:
        return "ВЭД импорт"
    return "Закупка"


def order_title(order: dict[str, Any]) -> str:
    title = clean_string(order.get("title"))
    if title:
        return title
    number = clean_string(
        order.get("number") or order.get("onec_source_number") or order.get("Номер")
    )
    supplier = order.get("supplier") if isinstance(order.get("supplier"), dict) else {}
    supplier_title = clean_string(supplier.get("title") or supplier.get("name"))
    amount = formatted_amount(order)
    return " · ".join(
        part for part in [order_title_prefix(order), number, amount, supplier_title] if part
    ).strip()


def field_name(mapping: dict[str, Any], logical_key: str) -> str:
    return clean_string((mapping.get("field_map") or {}).get(logical_key))


def crm_item_rest_field_name(field: str) -> str:
    raw = clean_string(field)
    if not raw:
        return ""
    builtins = {
        "ID": "id",
        "TITLE": "title",
        "STAGE_ID": "stageId",
        "CATEGORY_ID": "categoryId",
        "ASSIGNED_BY_ID": "assignedById",
    }
    upper = raw.upper()
    if upper in builtins:
        return builtins[upper]
    if upper.startswith("UF_CRM_"):
        parts = [part for part in raw.split("_")[2:] if part]
        return "ufCrm" + "".join(part[:1].upper() + part[1:].lower() for part in parts)
    return raw


def crm_item_rest_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {crm_item_rest_field_name(key): value for key, value in fields.items()}


def normalized_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return None
    number_text = raw.replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", number_text):
        return None
    unsigned = number_text.lstrip("+-")
    integer_part = unsigned.split(".", 1)[0]
    if len(integer_part) > 1 and integer_part.startswith("0"):
        return None
    try:
        return Decimal(number_text)
    except (InvalidOperation, ValueError):
        return None


def normalized_datetime_wall(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat(timespec="seconds")
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return ""
    normalized = raw.removesuffix("Z")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        normalized = f"{normalized}T00:00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    return parsed.replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds")


def parsed_datetime_value(value: Any) -> tuple[datetime, bool] | None:
    if isinstance(value, datetime):
        return value.replace(microsecond=0), value.tzinfo is not None
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day), False
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return None
    normalized = raw.removesuffix("Z")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        normalized = f"{normalized}T00:00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(microsecond=0), parsed.tzinfo is not None


def bitrix_datetimes_match(current: Any, desired: Any) -> bool | None:
    current_info = parsed_datetime_value(current)
    desired_info = parsed_datetime_value(desired)
    if not current_info or not desired_info:
        return None
    current_dt, current_has_tz = current_info
    desired_dt, desired_has_tz = desired_info
    if current_has_tz and desired_has_tz:
        return current_dt.astimezone(timezone.utc) == desired_dt.astimezone(timezone.utc)
    return current_dt.replace(tzinfo=None) == desired_dt.replace(tzinfo=None)


def bitrix_values_match(current: Any, desired: Any) -> bool:
    if isinstance(desired, bool):
        current_text = clean_string(current).casefold()
        return (
            current_text in {"1", "y", "yes", "true"}
            if desired
            else current_text
            in {
                "",
                "0",
                "n",
                "no",
                "false",
            }
        )
    current_decimal = normalized_decimal(current)
    desired_decimal = normalized_decimal(desired)
    if current_decimal is not None and desired_decimal is not None:
        return current_decimal == desired_decimal
    datetime_match = bitrix_datetimes_match(current, desired)
    if datetime_match is not None:
        return datetime_match
    current_datetime = normalized_datetime_wall(current)
    desired_datetime = normalized_datetime_wall(desired)
    if current_datetime and desired_datetime:
        return current_datetime == desired_datetime
    return clean_string(current) == clean_string(desired)


def changed_rest_fields(current_item: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if not bitrix_values_match(current_item.get(key), value)
    }


def mapped_stage_id(mapping: dict[str, Any], contour: str, stage_key: str) -> str:
    return clean_string(((mapping.get("stage_map") or {}).get(contour) or {}).get(stage_key))


def enum_value(mapping: dict[str, Any], logical_key: str, xml_id: str) -> str:
    return (
        clean_string(((mapping.get("enum_map") or {}).get(logical_key) or {}).get(xml_id)) or xml_id
    )


def get_procurement_item(api: Any, *, entity_type_id: int, item_id: str) -> dict[str, Any]:
    if not entity_type_id or not item_id:
        return {}
    payload = api.call("crm.item.get", {"entityTypeId": entity_type_id, "id": item_id})
    result = payload.get("result") if isinstance(payload, dict) else payload
    item = result.get("item") if isinstance(result, dict) else {}
    return item if isinstance(item, dict) else {}


def source_number(order: dict[str, Any]) -> str:
    return clean_string(
        order.get("number") or order.get("onec_source_number") or order.get("Номер")
    )


def source_type(order: dict[str, Any]) -> str:
    return clean_string(
        order.get("source_type")
        or order.get("onec_source_type")
        or order.get("ТипДокумента")
        or "ЗаказПоставщику"
    )


def source_date(order: dict[str, Any]) -> Any:
    return order.get("date") or order.get("onec_source_date") or order.get("Дата")


def value_year(value: Any) -> int | None:
    if isinstance(value, datetime):
        return value.year
    if isinstance(value, date):
        return value.year
    raw = clean_string(value)
    if not raw:
        return None
    normalized = raw.removesuffix("Z")
    try:
        return datetime.fromisoformat(normalized).year
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).year
        except ValueError:
            continue
    return None


def source_number_lookup_candidates(number: str) -> list[str]:
    original = clean_string(number)
    if not original:
        return []
    candidates = [original]
    match = re.match(r"^(.*?)(\d+)$", original)
    if not match:
        return candidates
    prefix, digits = match.groups()
    numeric = int(digits)
    widths = [len(digits) - 1, len(digits) + 1]
    for width in widths:
        if width <= 0:
            continue
        candidate = prefix + str(numeric).zfill(width)
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def item_matches_order_identity(
    item: dict[str, Any],
    *,
    source_type_field: str,
    expected_source_type: str,
    source_date_field: str,
    expected_year: int | None,
) -> bool:
    item_source_type = clean_string(item.get(source_type_field))
    if expected_source_type and item_source_type:
        if item_source_type.casefold() != expected_source_type.casefold():
            return False
    item_year = value_year(item.get(source_date_field))
    if expected_year and item_year:
        return item_year == expected_year
    return True


def add_order_scalar_fields(
    fields: dict[str, Any], order: dict[str, Any], mapping: dict[str, Any]
) -> None:
    rub_amount = amount_rub_value(order)
    original_amount = decimal_string(order.get("amount") or order.get("Сумма"))
    if rub_amount:
        fields["opportunity"] = rub_amount
        fields["currencyId"] = "RUB"
    elif original_amount and is_rub_currency(order_currency(order)):
        fields["opportunity"] = original_amount
        fields["currencyId"] = "RUB"

    pairs = {
        "title": order_title(order),
        "assigned_by": clean_string(order.get("assigned_by_id")),
        "pilot_batch_id": clean_string(order.get("pilot_batch_id") or order.get("batch_id")),
        "onec_source_type": clean_string(order.get("source_type") or "ЗаказПоставщику"),
        "onec_source_number": source_number(order),
        "onec_document_ref": clean_string(order.get("onec_ref")),
        "onec_source_date": iso_value(order.get("date") or order.get("onec_source_date")),
        "onec_posted": order.get("posted"),
        "ordered_quantity": order.get("ordered_qty"),
        "open_quantity": order.get("open_qty"),
        "received_quantity": order.get("received_qty"),
        "currency": clean_string(order.get("currency")),
        "amount": order.get("amount"),
        "planned_warehouse": clean_string(order.get("planned_warehouse") or order.get("warehouse")),
        "expects_import_gtd": order.get("expects_import_gtd"),
        "gtd_number": clean_string(order.get("gtd_number")),
        "payment_task_id": clean_string(order.get("payment_task_id")),
        "payment_task_status": clean_string(order.get("payment_task_status")),
        "payment_request_created_at": iso_value(order.get("payment_request_created_at")),
    }
    for logical_key, value in pairs.items():
        if value in ("", None):
            continue
        target = field_name(mapping, logical_key)
        if target:
            fields[target] = value
    onec_ref = clean_string(order.get("onec_ref"))
    if onec_ref:
        fields["xmlId"] = f"onec:supplier-order:{onec_ref.lower()}"
    lifecycle = clean_string(order.get("lifecycle_status"))
    lifecycle_field = field_name(mapping, "order_lifecycle_status")
    if lifecycle and lifecycle_field:
        fields[lifecycle_field] = enum_value(mapping, "order_lifecycle_status", lifecycle)


def payment_field_updates(
    mapping: dict[str, Any],
    *,
    task_id: str = "",
    status: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    task_field = field_name(mapping, "payment_task_id")
    if task_field and task_id:
        fields[task_field] = task_id
    status_field = field_name(mapping, "payment_task_status")
    if status_field and status:
        fields[status_field] = enum_value(mapping, "payment_task_status", status)
    created_at_field = field_name(mapping, "payment_request_created_at")
    if created_at_field and created_at:
        fields[created_at_field] = created_at
    return fields


def existing_procurement_item_id(api: Any, order: dict[str, Any], mapping: dict[str, Any]) -> str:
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    number_field = field_name(mapping, "onec_source_number")
    number = source_number(order)
    if not entity_type_id or not number_field or not number:
        return ""
    rest_number_field = crm_item_rest_field_name(number_field)
    rest_source_type_field = crm_item_rest_field_name(field_name(mapping, "onec_source_type"))
    rest_source_date_field = crm_item_rest_field_name(field_name(mapping, "onec_source_date"))
    select = [
        field
        for field in [
            "id",
            "title",
            rest_number_field,
            rest_source_type_field,
            rest_source_date_field,
        ]
        if field
    ]
    expected_source_type = source_type(order)
    expected_year = value_year(source_date(order))
    lookup_candidates = source_number_lookup_candidates(number)
    for candidates in [lookup_candidates[:1], lookup_candidates[1:]]:
        if not candidates:
            continue
        matched_ids: set[str] = set()
        for candidate in candidates:
            payload = api.call(
                "crm.item.list",
                {
                    "entityTypeId": entity_type_id,
                    "filter": {f"={rest_number_field}": candidate},
                    "select": select,
                },
            )
            result = payload.get("result") if isinstance(payload, dict) else payload
            items = result.get("items") if isinstance(result, dict) else []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not item_matches_order_identity(
                    item,
                    source_type_field=rest_source_type_field,
                    expected_source_type=expected_source_type,
                    source_date_field=rest_source_date_field,
                    expected_year=expected_year,
                ):
                    continue
                item_id = clean_string(item.get("id"))
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


def batch_id_exists(
    api: Any,
    *,
    mapping: dict[str, Any],
    batch_id: str,
    exclude_item_id: str = "",
) -> bool:
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    batch_field = field_name(mapping, "pilot_batch_id")
    if not entity_type_id or not batch_field or not batch_id:
        return False
    rest_batch_field = crm_item_rest_field_name(batch_field)
    payload = api.call(
        "crm.item.list",
        {
            "entityTypeId": entity_type_id,
            "filter": {f"={rest_batch_field}": batch_id},
            "select": ["id", rest_batch_field],
        },
    )
    result = payload.get("result") if isinstance(payload, dict) else payload
    items = result.get("items") if isinstance(result, dict) else []
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = clean_string(item.get("id"))
        if item_id and item_id != clean_string(exclude_item_id):
            return True
    return False


def unique_batch_id(
    api: Any,
    order: dict[str, Any],
    *,
    mapping: dict[str, Any],
    apply: bool,
    existing_item_id: str = "",
    used_batch_ids: set[str] | None = None,
) -> str:
    explicit = clean_string(order.get("pilot_batch_id") or order.get("batch_id"))
    if explicit:
        return explicit
    base = base_generated_batch_id(order)
    if not base:
        return ""
    used_batch_ids = used_batch_ids if used_batch_ids is not None else set()
    candidate = base
    suffix = 2
    while candidate in used_batch_ids or (
        apply
        and batch_id_exists(
            api,
            mapping=mapping,
            batch_id=candidate,
            exclude_item_id=existing_item_id,
        )
    ):
        candidate = f"{base}-{suffix:02d}"
        suffix += 1
    used_batch_ids.add(candidate)
    return candidate


def bitrix_item_url(api: Any, *, entity_type_id: int, item_id: str) -> str:
    webhook_base = clean_string(getattr(api, "webhook_base", ""))
    portal_base = webhook_base.split("/rest/", 1)[0] if "/rest/" in webhook_base else ""
    if not portal_base or not entity_type_id or not item_id:
        return ""
    return f"{portal_base}/crm/type/{entity_type_id}/details/{item_id}/"


def cargo_payment_task_title(order: dict[str, Any], *, batch_id: str) -> str:
    number = source_number(order) or "без номера"
    batch = batch_id or "без партии"
    return f"Оплатить карго-заявку: {number} / {batch}"[:255]


def cargo_payment_task_description(
    order: dict[str, Any],
    *,
    batch_id: str,
    item_url: str,
) -> str:
    supplier = order.get("supplier") if isinstance(order.get("supplier"), dict) else {}
    supplier_title = clean_string(supplier.get("title") or supplier.get("name")) or "не указан"
    amount = formatted_amount(order) or decimal_string(order.get("amount") or order.get("Сумма"))
    currency = order_currency(order)
    lines = [
        "Автоматическая заявка на оплату по карго.",
        f"Заказ 1С: {source_number(order) or 'не указан'}",
        f"Партия: {batch_id or 'не указана'}",
        f"Поставщик: {supplier_title}",
        f"Сумма сданного товара: {amount or 'не указана'}",
        f"Валюта: {currency or 'не указана'}",
        "Ожидаемые транши: обычно 3 транша по условиям поставщика.",
    ]
    if item_url:
        lines.append(f"Карточка закупки: {item_url}")
    return "\n".join(lines)


def extract_task_id(payload: Any) -> str:
    result = payload.get("result") if isinstance(payload, dict) else payload
    if isinstance(result, dict):
        task = result.get("task") if isinstance(result.get("task"), dict) else result
        return clean_string(task.get("id") or task.get("ID"))
    return clean_string(result)


def create_cargo_payment_task(
    api: Any,
    order: dict[str, Any],
    *,
    entity_type_id: int,
    item_id: str,
    batch_id: str,
    finance_user_id: str,
) -> str:
    payload = api.call(
        "tasks.task.add",
        {
            "fields": {
                "TITLE": cargo_payment_task_title(order, batch_id=batch_id),
                "DESCRIPTION": cargo_payment_task_description(
                    order,
                    batch_id=batch_id,
                    item_url=bitrix_item_url(api, entity_type_id=entity_type_id, item_id=item_id),
                ),
                "RESPONSIBLE_ID": finance_user_id,
                "ALLOW_CHANGE_DEADLINE": "Y",
            }
        },
    )
    task_id = extract_task_id(payload)
    if not task_id:
        raise RuntimeError("Bitrix24 tasks.task.add returned empty result")
    return task_id


def import_order(
    api: Any,
    order: dict[str, Any],
    *,
    mapping: dict[str, Any],
    apply: bool,
    assigned_by_id: str = "",
    finance_user_id: str = DEFAULT_CARGO_FINANCE_USER_ID,
    supplier_conflict_mode: str = "create_card_with_blocker",
    supplier_result: dict[str, Any] | None = None,
    existing_item_id: str | None = None,
    used_batch_ids: set[str] | None = None,
) -> dict[str, Any]:
    supplier = order.get("supplier") if isinstance(order.get("supplier"), dict) else {}
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    if existing_item_id is not None:
        existing_id = clean_string(existing_item_id)
    else:
        existing_id = existing_procurement_item_id(api, order, mapping) if apply else ""
    current_item = (
        get_procurement_item(api, entity_type_id=entity_type_id, item_id=existing_id)
        if apply and existing_id
        else {}
    )
    batch_id = unique_batch_id(
        api,
        order,
        mapping=mapping,
        apply=apply,
        existing_item_id=existing_id,
        used_batch_ids=used_batch_ids,
    )
    enriched_order = {**order}
    if batch_id:
        enriched_order["pilot_batch_id"] = batch_id
    if supplier_result is None:
        supplier_result = sync_supplier_to_crm(
            api,
            supplier,
            mapping=mapping,
            apply=apply,
            assigned_by_id=assigned_by_id or None,
        )
    payload = build_procurement_order_bitrix_fields(
        {**enriched_order, "supplier": supplier},
        supplier_result,
        mapping=mapping,
        on_supplier_conflict=supplier_conflict_mode,
    )
    fields = dict(payload["fields"])
    add_order_scalar_fields(fields, enriched_order, mapping)
    if assigned_by_id and "assignedById" not in fields:
        fields["assignedById"] = assigned_by_id

    rest_payment_task_field = crm_item_rest_field_name(field_name(mapping, "payment_task_id"))
    existing_payment_task_id = clean_string(
        enriched_order.get("payment_task_id")
        or (current_item.get(rest_payment_task_field) if rest_payment_task_field else "")
    )
    has_cargo_dropoff = (
        bool(cargo_dropoff_value(enriched_order)) and payload["logical_key"] == "cargo"
    )
    has_payment_done = (
        bool(payment_date_value(enriched_order))
        or clean_string(enriched_order.get("payment_task_status")).casefold() == "done"
    )
    payment_stage_id = mapped_stage_id(mapping, payload["logical_key"], "payment_work")
    payment_task_action = "not_required"
    payment_task_id = existing_payment_task_id
    payment_update_fields: dict[str, Any] = {}
    final_stage_key = clean_string(payload["stage_key"])
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if has_cargo_dropoff and has_payment_done:
        payment_task_action = "skipped_payment_done"
        payment_update_fields.update(
            payment_field_updates(mapping, task_id=payment_task_id, status="done")
        )
        if payment_stage_id:
            payment_update_fields["stageId"] = payment_stage_id
            final_stage_key = "payment_work"
    elif has_cargo_dropoff and existing_payment_task_id:
        payment_task_action = "exists"
        payment_update_fields.update(
            payment_field_updates(
                mapping,
                task_id=existing_payment_task_id,
                status="created",
                created_at=iso_value(enriched_order.get("payment_request_created_at")),
            )
        )
        if payment_stage_id:
            payment_update_fields["stageId"] = payment_stage_id
            final_stage_key = "payment_work"
    elif has_cargo_dropoff and not finance_user_id:
        payment_task_action = "skipped_no_finance_user"
        payment_update_fields.update(
            payment_field_updates(mapping, status="skipped", created_at=created_at)
        )
    elif has_cargo_dropoff:
        payment_task_action = "dry_run_create"
        payment_update_fields.update(
            payment_field_updates(mapping, status="created", created_at=created_at)
        )
        if payment_stage_id:
            payment_update_fields["stageId"] = payment_stage_id
            final_stage_key = "payment_work"

    action = "dry_run_update_or_create"
    item_id = existing_id
    if apply:
        # Existing payment state is known before the CRM write, so merge it into
        # the lifecycle payload. Otherwise every sync briefly moves the card to
        # the lifecycle stage and then back to payment_work in a second update.
        payment_update_pending = payment_task_action == "dry_run_create"
        fields_for_write = dict(fields)
        if not payment_update_pending:
            fields_for_write.update(payment_update_fields)
        rest_fields = crm_item_rest_fields(fields_for_write)
        if existing_id:
            fields_to_update = changed_rest_fields(current_item, rest_fields)
            if fields_to_update:
                api.call(
                    "crm.item.update",
                    {"entityTypeId": entity_type_id, "id": existing_id, "fields": fields_to_update},
                )
                current_item.update(fields_to_update)
                action = "updated"
            else:
                action = "noop"
        else:
            created = api.call(
                "crm.item.add", {"entityTypeId": entity_type_id, "fields": rest_fields}
            )
            result = created.get("result") if isinstance(created, dict) else created
            item = result.get("item") if isinstance(result, dict) else {}
            item_id = clean_string(item.get("id"))
            action = "created"
        if payment_task_action == "dry_run_create":
            try:
                payment_task_id = create_cargo_payment_task(
                    api,
                    enriched_order,
                    entity_type_id=entity_type_id,
                    item_id=item_id,
                    batch_id=batch_id,
                    finance_user_id=finance_user_id,
                )
                payment_task_action = "created"
                payment_update_fields.update(
                    payment_field_updates(
                        mapping,
                        task_id=payment_task_id,
                        status="created",
                        created_at=created_at,
                    )
                )
            except Exception:
                payment_update_fields.pop("stageId", None)
                final_stage_key = clean_string(payload["stage_key"])
                payment_update_fields.update(
                    payment_field_updates(mapping, status="error", created_at=created_at)
                )
                if item_id and payment_update_fields:
                    payment_rest_fields = crm_item_rest_fields(payment_update_fields)
                    api.call(
                        "crm.item.update",
                        {
                            "entityTypeId": entity_type_id,
                            "id": item_id,
                            "fields": payment_rest_fields,
                        },
                    )
                raise
        if item_id and payment_update_fields and payment_update_pending:
            payment_rest_fields = crm_item_rest_fields(payment_update_fields)
            if existing_id:
                payment_rest_fields = changed_rest_fields(current_item, payment_rest_fields)
            if payment_rest_fields:
                api.call(
                    "crm.item.update",
                    {
                        "entityTypeId": entity_type_id,
                        "id": item_id,
                        "fields": payment_rest_fields,
                    },
                )
                if action == "noop":
                    action = "updated"
    fields.update(payment_update_fields)
    return {
        "source_number": source_number(order),
        "action": action,
        "item_id": item_id,
        "contour": payload["logical_key"],
        "initial_stage_key": payload["stage_key"],
        "stage_key": final_stage_key,
        "batch_id": batch_id,
        "payment_task_action": payment_task_action,
        "payment_task_id": payment_task_id,
        "blocked_supplier": payload["blocked_supplier"],
        "supplier_status": supplier_result.get("status"),
        "supplier_company_id": supplier_result.get("company_id"),
        "field_names": sorted(fields),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    webhook_base = (args.webhook_url or "").strip()
    if not webhook_base:
        env = load_env(args.env_file)
        webhook_base = (
            env.get("PROCUREMENT_BITRIX_WEBHOOK_URL")
            or env.get("BITRIX_BOX_WEBHOOK_BASE")
            or env.get("BITRIX24_BOX_WEBHOOK_URL")
            or ""
        ).strip()
    else:
        env = load_env(args.env_file)
    if not webhook_base:
        raise SystemExit(
            f"Bitrix webhook is not configured. Set PROCUREMENT_BITRIX_WEBHOOK_URL "
            f"or BITRIX_BOX_WEBHOOK_BASE in {args.env_file}"
        )
    finance_user_id = (
        args.finance_user_id
        or env.get("PROCUREMENT_CARGO_FINANCE_USER_ID")
        or env.get("PROCUREMENT_FINANCE_USER_ID")
        or DEFAULT_CARGO_FINANCE_USER_ID
    ).strip()
    mapping = load_mapping(args.mapping_path)
    api = BitrixRestApi(webhook_base)
    used_batch_ids: set[str] = set()
    rows = [
        import_order(
            api,
            order,
            mapping=mapping,
            apply=args.apply,
            assigned_by_id=args.assigned_by_id,
            finance_user_id=finance_user_id,
            supplier_conflict_mode=args.supplier_conflict_mode,
            used_batch_ids=used_batch_ids,
        )
        for order in load_orders(args.input_json)
    ]
    result = {"mode": "apply" if args.apply else "dry-run", "rows": rows}
    args.result_path.parent.mkdir(parents=True, exist_ok=True)
    args.result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": result["mode"], "rows": len(rows)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
