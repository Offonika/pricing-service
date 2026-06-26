#!/usr/bin/env python3
"""Dry-run/apply import of 1C ЗаказПоставщику into Bitrix procurement smart-process."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
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


class BitrixRestApi:
    def __init__(self, webhook_base: str) -> None:
        self.webhook_base = webhook_base

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
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


def order_title_prefix(order: dict[str, Any]) -> str:
    contour = clean_string(order.get("procurement_contour") or order.get("КонтурЗакупки"))
    compact = contour.casefold().replace(" ", "").replace("_", "")
    if compact in {"cargo", "карго"}:
        return "Cargo"
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
        "onec_source_date": iso_value(order.get("date") or order.get("onec_source_date")),
        "onec_posted": order.get("posted"),
        "currency": clean_string(order.get("currency")),
        "amount": order.get("amount"),
        "planned_warehouse": clean_string(order.get("planned_warehouse") or order.get("warehouse")),
        "expects_import_gtd": order.get("expects_import_gtd"),
        "gtd_number": clean_string(order.get("gtd_number")),
    }
    for logical_key, value in pairs.items():
        if value in ("", None):
            continue
        target = field_name(mapping, logical_key)
        if target:
            fields[target] = value


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
        for field in ["id", "title", rest_number_field, rest_source_type_field, rest_source_date_field]
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


def import_order(
    api: Any,
    order: dict[str, Any],
    *,
    mapping: dict[str, Any],
    apply: bool,
    assigned_by_id: str = "",
    supplier_conflict_mode: str = "create_card_with_blocker",
    supplier_result: dict[str, Any] | None = None,
    existing_item_id: str | None = None,
) -> dict[str, Any]:
    supplier = order.get("supplier") if isinstance(order.get("supplier"), dict) else {}
    if supplier_result is None:
        supplier_result = sync_supplier_to_crm(
            api,
            supplier,
            mapping=mapping,
            apply=apply,
            assigned_by_id=assigned_by_id or None,
        )
    payload = build_procurement_order_bitrix_fields(
        {**order, "supplier": supplier},
        supplier_result,
        mapping=mapping,
        on_supplier_conflict=supplier_conflict_mode,
    )
    fields = dict(payload["fields"])
    add_order_scalar_fields(fields, order, mapping)
    if assigned_by_id and "assignedById" not in fields:
        fields["assignedById"] = assigned_by_id
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    if existing_item_id is not None:
        existing_id = clean_string(existing_item_id)
    else:
        existing_id = existing_procurement_item_id(api, order, mapping) if apply else ""
    action = "dry_run_update_or_create"
    item_id = existing_id
    if apply:
        rest_fields = crm_item_rest_fields(fields)
        if existing_id:
            api.call(
                "crm.item.update",
                {"entityTypeId": entity_type_id, "id": existing_id, "fields": rest_fields},
            )
            action = "updated"
        else:
            created = api.call(
                "crm.item.add", {"entityTypeId": entity_type_id, "fields": rest_fields}
            )
            result = created.get("result") if isinstance(created, dict) else created
            item = result.get("item") if isinstance(result, dict) else {}
            item_id = clean_string(item.get("id"))
            action = "created"
    return {
        "source_number": source_number(order),
        "action": action,
        "item_id": item_id,
        "contour": payload["logical_key"],
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
    if not webhook_base:
        raise SystemExit(
            f"Bitrix webhook is not configured. Set PROCUREMENT_BITRIX_WEBHOOK_URL "
            f"or BITRIX_BOX_WEBHOOK_BASE in {args.env_file}"
        )
    mapping = load_mapping(args.mapping_path)
    api = BitrixRestApi(webhook_base)
    rows = [
        import_order(
            api,
            order,
            mapping=mapping,
            apply=args.apply,
            assigned_by_id=args.assigned_by_id,
            supplier_conflict_mode=args.supplier_conflict_mode,
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
