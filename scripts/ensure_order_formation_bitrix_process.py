#!/usr/bin/env python3
"""Create or update the Bitrix smart process "Формирование заказа".

The script is dry-run by default. Pass --apply for Bitrix mutations.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.ensure_expertise_bitrix_process as bitrix_setup  # noqa: E402
from app.core.config import get_settings  # noqa: E402

DEFAULT_TITLE = "Формирование заказа"
DEFAULT_CODE = "procurement_order_formation"
DEFAULT_CATEGORY = "Заказы поставщикам"
DEFAULT_MAPPING_PATH = REPO_ROOT / "build/bitrix/order_formation_mapping.json"
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/order_formation_ensure_result.json"
DEFAULT_CATALOG_MAPPING = {
    "catalog_id": 17,
    "product_id": "ID",
    "name": "NAME",
    "xml_id": "XML_ID",
    "assortment_status": "PROPERTY_789",
    "status_changed_at": "PROPERTY_783",
    "status_approved_by": "PROPERTY_784",
    "status_reason": "PROPERTY_785",
    "status_source": "PROPERTY_786",
    "manual_minimum": "PROPERTY_787",
    "procurement_profile": "PROPERTY_790",
    "quality": "PROPERTY_482",
}

STAGE_SPECS = (
    ("draft", "NEW", "Черновик сформирован", 100, None),
    ("review", "REVIEW", "На проверке", 200, None),
    ("approved", "APPROVED", "Согласовано к 1С", 300, None),
    ("transmitting", "TRANSMITTING", "Передача в 1С", 400, None),
    ("transmitted", "TRANSMITTED", "Передано в 1С", 900, "S"),
    ("deferred", "DEFERRED", "Отложено / отменено", 1000, "F"),
    ("error", "ERROR", "Ошибка передачи", 800, None),
)

BUILTIN_FIELDS = {"title": "TITLE", "assigned_by": "ASSIGNED_BY_ID"}
CUSTOM_FIELD_SPECS = (
    ("backend_order_id", "ID заказа pricing-service", "string", True),
    ("stable_key", "Стабильный ключ заказа", "string", True),
    ("version", "Версия заказа", "string", True),
    ("supplier_ref", "GUID поставщика 1С", "string", False),
    ("supplier_code", "Код поставщика 1С", "string", False),
    ("supplier_name", "Поставщик", "string", True),
    ("contract_ref", "GUID договора 1С", "string", False),
    ("contract_code", "Код договора 1С", "string", False),
    ("contract_name", "Договор", "string", True),
    ("currency", "Валюта", "string", True),
    ("warehouse_ref", "GUID склада 1С", "string", False),
    ("warehouse_code", "Код склада 1С", "string", False),
    ("warehouse_name", "Склад", "string", True),
    ("procurement_contour", "Контур закупки", "string", True),
    ("route", "Маршрут", "string", True),
    ("batch_id", "Партия", "string", True),
    ("order_date", "Дата заказа", "date", True),
    ("calculation_id", "ID расчёта", "string", True),
    ("source_run_id", "ID запуска расчёта", "string", False),
    ("approved_version", "Согласованная версия", "string", False),
    ("approved_by", "Согласовал", "string", False),
    ("approved_at", "Дата согласования", "datetime", False),
    ("connector_status", "Статус коннектора 1С", "string", True),
    ("onec_message_id", "ID сообщения 1С", "string", False),
    ("onec_document_ref", "GUID заказа поставщику 1С", "string", False),
    ("onec_document_number", "Номер заказа поставщику 1С", "string", False),
    ("onec_error", "Ошибка передачи в 1С", "text", False),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply changes in Bitrix24")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--code", default=DEFAULT_CODE)
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    return parser.parse_args()


def build_dry_run_mapping(
    *, title: str = DEFAULT_TITLE, code: str = DEFAULT_CODE, category: str = DEFAULT_CATEGORY
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": True,
        "process": {
            "title": title,
            "code": code,
            "type_id": 0,
            "entity_type_id": 0,
            "owner_type": "T0",
            "category_id": 0,
            "category_name": category,
            "products_enabled": True,
        },
        "stage_map": {
            logical_key: f"DT0_0:{code_value}"
            for logical_key, code_value, _name, _sort, _semantics in STAGE_SPECS
        },
        "fields": {
            **BUILTIN_FIELDS,
            **{
                logical_key: _xml_id(logical_key)
                for logical_key, _title, _type, _required in CUSTOM_FIELD_SPECS
            },
        },
        "field_specs": [
            {
                "logical_key": logical_key,
                "title": title_value,
                "type": field_type,
                "required": required,
                "found": True,
            }
            for logical_key, title_value, field_type, required in CUSTOM_FIELD_SPECS
        ],
        "catalog": dict(DEFAULT_CATALOG_MAPPING),
    }


def ensure_type(webhook: str, *, title: str, code: str) -> dict[str, Any]:
    response = bitrix_setup.bitrix_call(webhook, "crm.type.list", {"filter": {"title": title}})
    types = (response.get("result") or {}).get("types") or []
    existing = next((item for item in types if item.get("title") == title), None)
    fields = {
        "title": title,
        "code": code,
        "isUseInUserfieldEnabled": "Y",
        "isCategoriesEnabled": "Y",
        "isStagesEnabled": "Y",
        "isBeginCloseDatesEnabled": "Y",
        "isObserversEnabled": "Y",
        "isAutomationEnabled": "Y",
        "isBizProcEnabled": "Y",
        "isCountersEnabled": "Y",
        "isLinkWithProductsEnabled": "Y",
    }
    if existing:
        bitrix_setup.bitrix_call(
            webhook, "crm.type.update", {"id": existing["id"], "fields": fields}
        )
    else:
        added = bitrix_setup.bitrix_call(webhook, "crm.type.add", {"fields": fields})
        existing = (added.get("result") or {}).get("type")
    if not existing:
        raise RuntimeError("crm.type.add returned empty result")
    refreshed = bitrix_setup.bitrix_call(webhook, "crm.type.list", {"filter": {"title": title}})
    return next(
        item
        for item in ((refreshed.get("result") or {}).get("types") or [])
        if item.get("title") == title
    )


def ensure_category(webhook: str, *, entity_type_id: int, name: str) -> dict[str, Any]:
    response = bitrix_setup.bitrix_call(
        webhook, "crm.category.list", {"entityTypeId": entity_type_id}
    )
    categories = (response.get("result") or {}).get("categories") or []
    existing = next((item for item in categories if item.get("name") == name), None)
    if existing:
        return existing
    response = bitrix_setup.bitrix_call(
        webhook,
        "crm.category.add",
        {"entityTypeId": entity_type_id, "fields": {"name": name}},
    )
    category = (response.get("result") or {}).get("category")
    if not category:
        raise RuntimeError("crm.category.add returned empty result")
    return category


def ensure_stages(webhook: str, *, entity_type_id: int, category_id: int) -> dict[str, str]:
    entity_id = f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"
    response = bitrix_setup.bitrix_call(
        webhook, "crm.status.list", {"filter": {"ENTITY_ID": entity_id}}
    )
    stages = response.get("result") or []
    for stage in stages:
        if stage.get("SEMANTICS") == "S":
            bitrix_setup.bitrix_call(
                webhook,
                "crm.status.update",
                {"id": stage["ID"], "fields": {"SORT": 990}},
            )
        elif stage.get("SEMANTICS") == "F":
            bitrix_setup.bitrix_call(
                webhook,
                "crm.status.update",
                {"id": stage["ID"], "fields": {"SORT": 1000}},
            )
    response = bitrix_setup.bitrix_call(
        webhook, "crm.status.list", {"filter": {"ENTITY_ID": entity_id}}
    )
    stages = response.get("result") or []
    by_status_id = {str(item.get("STATUS_ID") or ""): item for item in stages}
    claimed_ids: set[str] = set()
    mapping: dict[str, str] = {}
    ordered_specs = [item for item in STAGE_SPECS if item[4] is None] + [
        item for item in STAGE_SPECS if item[4] is not None
    ]
    for logical_key, code, name, sort, semantics in ordered_specs:
        desired_status_id = f"DT{entity_type_id}_{category_id}:{code}"
        current = by_status_id.get(desired_status_id)
        if current is None and semantics:
            current = next(
                (
                    item
                    for item in stages
                    if item.get("SEMANTICS") == semantics
                    and str(item.get("ID") or "") not in claimed_ids
                ),
                None,
            )
        fields: dict[str, Any] = {"NAME": name, "SORT": sort}
        if semantics:
            fields["SEMANTICS"] = semantics
        if current:
            bitrix_setup.bitrix_call(
                webhook, "crm.status.update", {"id": current["ID"], "fields": fields}
            )
            status_id = str(current.get("STATUS_ID") or desired_status_id)
            claimed_ids.add(str(current.get("ID") or ""))
        else:
            bitrix_setup.bitrix_call(
                webhook,
                "crm.status.add",
                {"fields": {"ENTITY_ID": entity_id, "STATUS_ID": desired_status_id, **fields}},
            )
            status_id = desired_status_id
        mapping[logical_key] = status_id
    return mapping


def cleanup_extra_stages(
    webhook: str,
    *,
    entity_type_id: int,
    category_id: int,
    keep_status_ids: set[str],
    fallback_stage_id: str,
) -> list[str]:
    entity_id = f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"
    response = bitrix_setup.bitrix_call(
        webhook, "crm.status.list", {"filter": {"ENTITY_ID": entity_id}}
    )
    deleted: list[str] = []
    for stage in response.get("result") or []:
        status_id = str(stage.get("STATUS_ID") or "")
        if status_id in keep_status_ids:
            continue
        items = bitrix_setup.bitrix_call(
            webhook,
            "crm.item.list",
            {
                "entityTypeId": entity_type_id,
                "select": ["id"],
                "filter": {"categoryId": category_id, "stageId": status_id},
                "start": 0,
            },
        )
        item_rows = (items.get("result") or {}).get("items") or []
        for item in item_rows:
            bitrix_setup.bitrix_call(
                webhook,
                "crm.item.update",
                {
                    "entityTypeId": entity_type_id,
                    "id": item["id"],
                    "fields": {"stageId": fallback_stage_id},
                },
            )
        if status_id.endswith(":NEW"):
            continue
        bitrix_setup.bitrix_call(
            webhook,
            "crm.status.delete",
            {"id": stage["ID"], "forced": "Y"},
        )
        deleted.append(status_id)
    return deleted


def ensure_fields(webhook: str, *, type_id: int) -> tuple[dict[str, str], list[dict[str, Any]]]:
    entity_id = f"CRM_{type_id}"
    existing = _list_userfields(webhook, entity_id=entity_id)
    by_xml_id = {str(item.get("xmlId") or ""): item for item in existing if item.get("xmlId")}
    rows: list[dict[str, Any]] = []
    for index, (logical_key, title, field_type, required) in enumerate(CUSTOM_FIELD_SPECS, start=1):
        xml_id = _xml_id(logical_key)
        field = by_xml_id.get(xml_id)
        if field is None:
            response = bitrix_setup.bitrix_call(
                webhook,
                "userfieldconfig.add",
                {
                    "moduleId": "crm",
                    "field": {
                        "entityId": entity_id,
                        "fieldName": _field_name(entity_id, logical_key),
                        "userTypeId": _user_type(field_type),
                        "xmlId": xml_id,
                        "multiple": "N",
                        "mandatory": "N",
                        "showFilter": "E",
                        "isSearchable": "Y",
                        "editInList": "Y",
                        "sort": 100 + index * 10,
                        "settings": _field_settings(field_type),
                    },
                },
            )
            field = (response.get("result") or {}).get("field") or {}
            action = "created"
        else:
            action = "existing"
        bitrix_setup.bitrix_call(
            webhook,
            "userfieldconfig.update",
            {
                "moduleId": "crm",
                "id": field["id"],
                "field": {
                    "languageId": "ru",
                    "xmlId": xml_id,
                    "editFormLabel": {"ru": title},
                    "listColumnLabel": {"ru": title},
                    "listFilterLabel": {"ru": title},
                },
            },
        )
        rows.append(
            {
                "logical_key": logical_key,
                "title": title,
                "type": field_type,
                "required": required,
                "field_name": field.get("fieldName"),
                "xml_id": xml_id,
                "action": action,
            }
        )
    refreshed = _list_userfields(webhook, entity_id=entity_id)
    refreshed_by_xml_id = {
        str(item.get("xmlId") or ""): item for item in refreshed if item.get("xmlId")
    }
    fields = dict(BUILTIN_FIELDS)
    for logical_key, _title, _field_type, _required in CUSTOM_FIELD_SPECS:
        field_name = str(
            (refreshed_by_xml_id.get(_xml_id(logical_key)) or {}).get("fieldName") or ""
        )
        if field_name:
            fields[logical_key] = field_name
    return fields, rows


def resolve_owner_type(webhook: str, *, entity_type_id: int) -> str:
    rows = bitrix_setup.bitrix_call(webhook, "crm.enum.ownertype", {}).get("result") or []
    item = next(
        (row for row in rows if str(row.get("ID") or "") == str(entity_type_id)),
        None,
    )
    owner_type = str((item or {}).get("SYMBOL_CODE_SHORT") or "").strip()
    if not owner_type:
        raise RuntimeError("crm.enum.ownertype did not return SYMBOL_CODE_SHORT")
    return owner_type


def _list_userfields(webhook: str, *, entity_id: str) -> list[dict[str, Any]]:
    response = bitrix_setup.bitrix_call(
        webhook,
        "userfieldconfig.list",
        {"moduleId": "crm", "filter": {"entityId": entity_id}},
    )
    return (response.get("result") or {}).get("fields") or []


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9_]+", "_", value.upper())).strip("_")[:32]


def _xml_id(logical_key: str) -> str:
    return f"UF_CRM_ORDER_FORMATION_{_slug(logical_key)}"


def _field_name(entity_id: str, logical_key: str) -> str:
    return f"UF_{entity_id}_{_slug(logical_key).replace('_', '')}"


def _user_type(field_type: str) -> str:
    return "string" if field_type in {"string", "text"} else field_type


def _field_settings(field_type: str) -> dict[str, Any]:
    if field_type == "text":
        return {"DEFAULT_VALUE": "", "SIZE": 50, "ROWS": 5}
    if field_type == "string":
        return {"DEFAULT_VALUE": "", "SIZE": 30, "ROWS": 1}
    if field_type == "date":
        return {"DEFAULT_VALUE": {"TYPE": "NONE", "VALUE": ""}}
    if field_type == "datetime":
        return {
            "DEFAULT_VALUE": {"TYPE": "NONE", "VALUE": ""},
            "USE_SECOND": "Y",
            "USE_TIMEZONE": "N",
        }
    raise ValueError(f"unsupported Bitrix field type: {field_type}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_catalog_mapping(webhook: str) -> dict[str, Any]:
    response = bitrix_setup.bitrix_call(webhook, "crm.product.fields", {})
    fields = response.get("result") or {}
    by_title = {
        str((details or {}).get("title") or "").strip(): str(code)
        for code, details in fields.items()
        if isinstance(details, dict)
    }
    mapping = dict(DEFAULT_CATALOG_MAPPING)
    titles = {
        "assortment_status": "Статус ассортимента",
        "status_changed_at": "Дата изменения статуса ассортимента",
        "status_approved_by": "Утвердил статус ассортимента",
        "status_reason": "Причина статуса ассортимента",
        "status_source": "Источник статуса ассортимента",
        "manual_minimum": "Ручной минимальный остаток",
        "procurement_profile": "Профиль закупочного поведения",
        "quality": "Качество",
    }
    for logical_key, title in titles.items():
        if by_title.get(title):
            mapping[logical_key] = by_title[title]
    enum_values: dict[str, dict[str, str]] = {}
    for logical_key, field_code in mapping.items():
        match = re.fullmatch(r"PROPERTY_(\d+)", str(field_code))
        if not match:
            continue
        details = (
            bitrix_setup.bitrix_call(
                webhook, "crm.product.property.get", {"id": int(match.group(1))}
            ).get("result")
            or {}
        )
        values = details.get("VALUES") or {}
        decoded = {
            str(item.get("ID") or value_id): str(item.get("VALUE") or "").strip()
            for value_id, item in values.items()
            if isinstance(item, dict) and str(item.get("VALUE") or "").strip()
        }
        if decoded:
            enum_values[logical_key] = decoded
    mapping["enum_values"] = enum_values
    return mapping


def main() -> int:
    args = parse_args()
    if not args.apply:
        mapping = build_dry_run_mapping(title=args.title, code=args.code, category=args.category)
        _write_json(args.mapping_path, mapping)
        result = {
            "dry_run": True,
            "mapping_path": str(args.mapping_path),
            "stages": list(mapping["stage_map"]),
            "products_enabled": True,
        }
        _write_json(args.result_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    settings = get_settings()
    webhook = (
        settings.procurement_labels_bitrix_webhook_url
        or settings.procurement_bitrix_webhook_url
        or settings.bitrix_box_webhook_base
    )
    if not webhook:
        raise RuntimeError("Bitrix procurement webhook is not configured")
    process_type = ensure_type(webhook, title=args.title, code=args.code)
    entity_type_id = int(process_type["entityTypeId"])
    type_id = int(process_type["id"])
    category = ensure_category(webhook, entity_type_id=entity_type_id, name=args.category)
    category_id = int(category["id"])
    stage_map = ensure_stages(webhook, entity_type_id=entity_type_id, category_id=category_id)
    deleted_stages = cleanup_extra_stages(
        webhook,
        entity_type_id=entity_type_id,
        category_id=category_id,
        keep_status_ids=set(stage_map.values()),
        fallback_stage_id=stage_map["draft"],
    )
    fields, field_specs = ensure_fields(webhook, type_id=type_id)
    owner_type = resolve_owner_type(webhook, entity_type_id=entity_type_id)
    mapping = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": False,
        "process": {
            "title": process_type.get("title"),
            "code": process_type.get("code"),
            "type_id": type_id,
            "entity_type_id": entity_type_id,
            "owner_type": owner_type,
            "category_id": category_id,
            "category_name": category.get("name"),
            "products_enabled": True,
        },
        "stage_map": stage_map,
        "fields": fields,
        "field_specs": field_specs,
        "catalog": discover_catalog_mapping(webhook),
        "missing_fields": [
            title
            for logical_key, title, _field_type, required in CUSTOM_FIELD_SPECS
            if required and logical_key not in fields
        ],
    }
    _write_json(args.mapping_path, mapping)
    result = {
        "dry_run": False,
        "mapping_path": str(args.mapping_path),
        "entity_type_id": entity_type_id,
        "owner_type": owner_type,
        "category_id": category_id,
        "products_enabled": True,
        "missing_fields": mapping["missing_fields"],
        "deleted_extra_stages": deleted_stages,
    }
    _write_json(args.result_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
