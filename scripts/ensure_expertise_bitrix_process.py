#!/usr/bin/env python3
"""Create/update Bitrix Box smart-process for Expertise Wave 1 and save live mapping."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_MAPPING_PATH = REPO_ROOT / "build/bitrix/expertise_mapping.json"
DEFAULT_DETAILS_CONFIG_PATH = REPO_ROOT / "build/bitrix/expertise_details_configuration.json"
DEFAULT_PROCESS_TITLE = "Экспертиза"
DEFAULT_PROCESS_CODE = "expertise"
DEFAULT_CATEGORY_NAME = "Общая воронка"

STAGE_SPECS = [
    {
        "logical_key": "created",
        "code": "CREATED",
        "name": "Создано",
        "sort": 100,
        "semantics": None,
    },
    {
        "logical_key": "received_by_okk",
        "code": "RECEIVED",
        "name": "Принято в ОКК",
        "sort": 200,
        "semantics": None,
    },
    {
        "logical_key": "under_review",
        "code": "REVIEW",
        "name": "На рассмотрении",
        "sort": 300,
        "semantics": None,
    },
    {
        "logical_key": "manual_review",
        "code": "MANUAL",
        "name": "Ручной разбор",
        "sort": 350,
        "semantics": None,
    },
    {
        "logical_key": "decision_ready",
        "code": "DECISION",
        "name": "Решение готово",
        "sort": 400,
        "semantics": None,
    },
    {
        "logical_key": "client_notified",
        "code": "NOTIFIED",
        "name": "Клиент уведомлен",
        "sort": 500,
        "semantics": None,
    },
    {
        "logical_key": "returned_to_store",
        "code": "RETURNED",
        "name": "Возвращено в подразделение",
        "sort": 600,
        "semantics": "S",
    },
]

BUILTIN_FIELD_MAPPING = {
    "title": "TITLE",
    "assigned_by": "ASSIGNED_BY_ID",
}

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "expertise_ref",
        "title": "Тех. ref экспертизы 1С",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "expertise_number",
        "title": "Код экспертизы 1С",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "case_id",
        "title": "Тех. ID кейса backend",
        "type": "integer",
        "required": True,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "sale_ref",
        "title": "Тех. ref реализации 1С",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "sale_number",
        "title": "Код реализации 1С",
        "type": "string",
        "required": False,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "order_ref",
        "title": "Тех. ref заказа покупателя",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "order_number",
        "title": "Код заказа покупателя 1С",
        "type": "string",
        "required": False,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "organization_ref",
        "title": "Тех. ref организации 1С",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "contract_ref",
        "title": "Тех. ref договора 1С",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "store",
        "title": "Подразделение",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "customer",
        "title": "Клиент",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "phone",
        "title": "Телефон клиента",
        "type": "string",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "problem",
        "title": "Описание проблемы",
        "type": "text",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "decision_code",
        "title": "Тех. код решения",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "decision_label",
        "title": "Решение",
        "type": "string",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "decision_comment",
        "title": "Комментарий решения",
        "type": "text",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "status",
        "title": "Тех. статус backend",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "owner_ext",
        "title": "Тех. внешний ID ответственного",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "owner_name",
        "title": "Ответственный 1С",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "due_at",
        "title": "Дедлайн кейса",
        "type": "datetime",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "overdue",
        "title": "Просрочено",
        "type": "boolean",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "client_notified",
        "title": "Клиент уведомлен",
        "type": "boolean",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "sync_at",
        "title": "Тех. время синхронизации",
        "type": "datetime",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "source",
        "title": "Тех. источник",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "folder_url",
        "title": "Ссылка на папку Bitrix Disk",
        "type": "url",
        "required": False,
        "edit_in_list": True,
        "searchable": False,
    },
]

COMPATIBLE_BITRIX_TYPES = {
    "string": {"string", "crm"},
    "text": {"text", "string"},
    "double": {"double", "integer"},
    "enumeration": {"enumeration"},
    "integer": {"integer", "double"},
    "boolean": {"boolean", "char"},
    "datetime": {"datetime", "date"},
    "url": {"string", "url"},
}

DETAIL_BUILTIN_ELEMENT_NAMES = {
    "stage": "STAGE_ID",
    "title": "TITLE",
    "assigned_by": "ASSIGNED_BY_ID",
}

DETAIL_SECTION_SPECS = [
    {
        "name": "main",
        "title": "Основное",
        "elements": [
            "stage",
            "title",
            "assigned_by",
        ],
    },
    {
        "name": "codes",
        "title": "Коды 1С",
        "elements": [
            "expertise_number",
            "sale_number",
            "order_number",
        ],
    },
    {
        "name": "client",
        "title": "Клиент и подразделение",
        "elements": [
            "store",
            "customer",
            "phone",
            "problem",
        ],
    },
    {
        "name": "decision",
        "title": "Решение и контроль",
        "elements": [
            "decision_label",
            "decision_comment",
            "client_notified",
            "due_at",
            "overdue",
        ],
    },
    {
        "name": "materials",
        "title": "Материалы",
        "elements": [
            "folder_url",
        ],
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/update Bitrix Box smart-process for expertise and save mapping."
    )
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--title", default=DEFAULT_PROCESS_TITLE)
    parser.add_argument("--code", default=DEFAULT_PROCESS_CODE)
    parser.add_argument("--category-name", default=DEFAULT_CATEGORY_NAME)
    return parser.parse_args()


def load_settings():
    from app.core.config import Settings

    return Settings(_env_file=REPO_ROOT / ".env")


def _flatten_bitrix_param(prefix: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for child_key, child_value in value.items():
            result.extend(_flatten_bitrix_param(f"{prefix}[{child_key}]", child_value))
        return result
    if isinstance(value, (list, tuple, set)):
        result: list[tuple[str, str]] = []
        for child_value in value:
            result.extend(_flatten_bitrix_param(f"{prefix}[]", child_value))
        return result
    if value is None:
        return []
    if isinstance(value, bool):
        return [(prefix, "Y" if value else "N")]
    if isinstance(value, (datetime, date)):
        return [(prefix, value.isoformat())]
    return [(prefix, str(value))]


def bitrix_call(
    webhook_base: str,
    method: str,
    params: list[tuple[str, Any]] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = params
    pairs: list[tuple[str, str]] = []
    if payload:
        items = payload.items() if isinstance(payload, dict) else payload
        for key, value in items:
            pairs.extend(_flatten_bitrix_param(key, value))
    data = urllib.parse.urlencode(pairs, doseq=True).encode("utf-8") if pairs else b""
    request = urllib.request.Request(
        webhook_base.rstrip("/") + f"/{method}.json",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Bitrix API {method}: HTTP {exc.code} {error_body}") from exc
    parsed = json.loads(raw)
    if parsed.get("error"):
        raise RuntimeError(
            f"Bitrix API {method}: {parsed['error']} {parsed.get('error_description', '')}".strip()
        )
    return parsed


def bitrix_call_json(
    webhook_base: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_base.rstrip("/") + f"/{method}.json",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Bitrix API {method}: HTTP {exc.code} {error_body}") from exc
    parsed = json.loads(raw)
    if parsed.get("error"):
        raise RuntimeError(
            f"Bitrix API {method}: {parsed['error']} {parsed.get('error_description', '')}".strip()
        )
    return parsed


def find_type_by_title(webhook_base: str, title: str) -> dict[str, Any] | None:
    response = bitrix_call(webhook_base, "crm.type.list", {"filter[title]": title})
    types = (response.get("result") or {}).get("types") or []
    return types[0] if types else None


def ensure_type(webhook_base: str, *, title: str, code: str) -> dict[str, Any]:
    existing = find_type_by_title(webhook_base, title)
    fields = {
        "title": title,
        "code": code,
        "isCategoriesEnabled": True,
        "isStagesEnabled": True,
        "isBeginCloseDatesEnabled": True,
        "isUseInUserfieldEnabled": True,
        "isObserversEnabled": True,
        "isAutomationEnabled": True,
        "isBizProcEnabled": True,
        "isCountersEnabled": True,
        "isLinkWithProductsEnabled": False,
    }
    if existing:
        bitrix_call(webhook_base, "crm.type.update", {"id": existing["id"], "fields": fields})
        refreshed = find_type_by_title(webhook_base, title)
        if refreshed:
            return refreshed
    response = bitrix_call(webhook_base, "crm.type.add", {"fields": fields})
    created = (response.get("result") or {}).get("type") or {}
    if not created:
        raise RuntimeError(f"Bitrix API crm.type.add returned empty type for title={title!r}")
    return created


def ensure_category(webhook_base: str, *, entity_type_id: int, name: str) -> dict[str, Any]:
    response = bitrix_call(webhook_base, "crm.category.list", {"entityTypeId": entity_type_id})
    categories = (response.get("result") or {}).get("categories") or []
    for category in categories:
        if str(category.get("name") or "").strip() == name:
            return category
    created = bitrix_call(
        webhook_base,
        "crm.category.add",
        {"entityTypeId": entity_type_id, "fields": {"name": name, "sort": 100}},
    )
    category = (created.get("result") or {}).get("category") or {}
    if not category:
        raise RuntimeError(f"Bitrix API crm.category.add returned empty category for name={name!r}")
    return category


def _status_entity_id(entity_type_id: int, category_id: int) -> str:
    return f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"


def list_stages(
    webhook_base: str, *, entity_type_id: int, category_id: int
) -> list[dict[str, Any]]:
    response = bitrix_call(
        webhook_base,
        "crm.status.list",
        {"filter[ENTITY_ID]": _status_entity_id(entity_type_id, category_id)},
    )
    return list(response.get("result") or [])


def ensure_stages(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
) -> dict[str, dict[str, Any]]:
    entity_id = _status_entity_id(entity_type_id, category_id)
    stages = list_stages(webhook_base, entity_type_id=entity_type_id, category_id=category_id)
    by_status = {str(item.get("STATUS_ID")): item for item in stages}
    process_stages = sorted(
        [item for item in stages if not item.get("SEMANTICS")],
        key=lambda item: int(item.get("SORT") or 0),
    )
    success_stage = next((item for item in stages if item.get("SEMANTICS") == "S"), None)
    failure_stages = sorted(
        [item for item in stages if item.get("SEMANTICS") == "F"],
        key=lambda item: int(item.get("SORT") or 0),
    )

    if success_stage is not None:
        bitrix_call(
            webhook_base, "crm.status.update", {"id": success_stage["ID"], "fields": {"SORT": 990}}
        )
    for offset, failure_stage in enumerate(failure_stages, start=1):
        bitrix_call(
            webhook_base,
            "crm.status.update",
            {"id": failure_stage["ID"], "fields": {"SORT": 1000 + offset}},
        )

    stages = list_stages(webhook_base, entity_type_id=entity_type_id, category_id=category_id)
    by_status = {str(item.get("STATUS_ID")): item for item in stages}
    process_stages = sorted(
        [item for item in stages if not item.get("SEMANTICS")],
        key=lambda item: int(item.get("SORT") or 0),
    )
    success_stage = next((item for item in stages if item.get("SEMANTICS") == "S"), None)
    failure_stages = sorted(
        [item for item in stages if item.get("SEMANTICS") == "F"],
        key=lambda item: int(item.get("SORT") or 0),
    )

    desired: dict[str, dict[str, Any]] = {}
    existing_process_iter = iter(process_stages)
    existing_failure_iter = iter(failure_stages)
    used_status_ids: set[str] = set()
    expected_status_ids = {
        f"DT{entity_type_id}_{category_id}:{spec['code']}" for spec in STAGE_SPECS
    }

    def next_unused_stage(iterator) -> dict[str, Any] | None:
        for stage in iterator:
            status_id = str(stage.get("STATUS_ID") or "")
            if status_id not in used_status_ids and status_id not in expected_status_ids:
                return stage
        return None

    for spec in STAGE_SPECS:
        expected_status_id = f"DT{entity_type_id}_{category_id}:{spec['code']}"
        current = by_status.get(expected_status_id)
        if current is None:
            if spec["semantics"] is None:
                current = next_unused_stage(existing_process_iter)
            elif spec["semantics"] == "S":
                current = (
                    success_stage
                    if success_stage is not None
                    and str(success_stage.get("STATUS_ID") or "") not in used_status_ids
                    else None
                )
            else:
                current = next_unused_stage(existing_failure_iter)
        elif str(current.get("STATUS_ID") or "") in used_status_ids:
            current = None

        fields = {"NAME": spec["name"], "SORT": spec["sort"]}
        if spec["semantics"] is not None:
            fields["SEMANTICS"] = spec["semantics"]

        if current is not None:
            used_status_ids.add(str(current.get("STATUS_ID") or ""))
            bitrix_call(webhook_base, "crm.status.update", {"id": current["ID"], "fields": fields})
            refreshed_stages = list_stages(
                webhook_base, entity_type_id=entity_type_id, category_id=category_id
            )
            refreshed = next(
                (
                    item
                    for item in refreshed_stages
                    if str(item.get("NAME") or "").strip() == spec["name"]
                    and str(item.get("SEMANTICS") or "") == str(spec["semantics"] or "")
                ),
                None,
            )
            if refreshed is not None:
                desired[spec["logical_key"]] = refreshed
                by_status[str(refreshed.get("STATUS_ID"))] = refreshed
                continue

        add_fields = {
            "ENTITY_ID": entity_id,
            "STATUS_ID": expected_status_id,
            "NAME": spec["name"],
            "SORT": spec["sort"],
        }
        if spec["semantics"] is not None:
            add_fields["SEMANTICS"] = spec["semantics"]
        bitrix_call(webhook_base, "crm.status.add", {"fields": add_fields})
        used_status_ids.add(expected_status_id)
        refreshed_stages = list_stages(
            webhook_base, entity_type_id=entity_type_id, category_id=category_id
        )
        created = next(
            (item for item in refreshed_stages if item.get("STATUS_ID") == expected_status_id),
            None,
        )
        if created is None:
            raise RuntimeError(f"Не удалось найти stage {expected_status_id} после создания")
        desired[spec["logical_key"]] = created

    return desired


def field_type_matches_expected(expected_type: str, actual_type: str | None) -> bool:
    if not actual_type:
        return False
    return actual_type in COMPATIBLE_BITRIX_TYPES.get(expected_type, {expected_type})


def get_item_field_catalog(webhook_base: str, *, entity_type_id: int) -> dict[str, dict[str, Any]]:
    response = bitrix_call(webhook_base, "crm.item.fields", {"entityTypeId": entity_type_id})
    return (response.get("result") or {}).get("fields") or {}


def smart_process_userfield_entity_id(type_id: int) -> str:
    return f"CRM_{type_id}"


def list_userfields(webhook_base: str, *, entity_id: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    start: int | None = 0
    while start is not None:
        params: dict[str, Any] = {"moduleId": "crm", "filter": {"entityId": entity_id}}
        if start:
            params["start"] = start
        response = bitrix_call(webhook_base, "userfieldconfig.list", params)
        result = response.get("result") or {}
        fields.extend(result.get("fields") or [])
        next_start = response.get("next")
        start = int(next_start) if str(next_start or "").isdigit() else None
    return fields


def _slug_suffix(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]+", "_", value.upper())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:32] or "FIELD"


def _field_name_for_spec(entity_id: str, spec: dict[str, Any]) -> str:
    compact_suffix = _slug_suffix(spec["logical_key"]).replace("_", "")
    return f"UF_{entity_id}_{compact_suffix}"


def _field_xml_id_for_spec(spec: dict[str, Any]) -> str:
    return f"UF_CRM_EXPERTISE_{_slug_suffix(spec['logical_key'])}"


def _field_config_for_spec(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    field_type = spec["type"]
    if field_type == "string":
        return "string", {"DEFAULT_VALUE": "", "SIZE": 20, "ROWS": 1}
    if field_type == "text":
        return "string", {"DEFAULT_VALUE": "", "SIZE": 50, "ROWS": 5}
    if field_type == "double":
        return "double", {"SIZE": 20, "PRECISION": 2, "DEFAULT_VALUE": None}
    if field_type == "enumeration":
        return "enumeration", {"DISPLAY": "LIST", "LIST_HEIGHT": 1, "SHOW_NO_VALUE": "Y"}
    if field_type == "integer":
        return "integer", {"SIZE": 20, "MIN_VALUE": 0, "MAX_VALUE": 0, "DEFAULT_VALUE": None}
    if field_type == "boolean":
        return (
            "boolean",
            {
                "DEFAULT_VALUE": 0,
                "DISPLAY": "CHECKBOX",
                "LABEL": ["Нет", "Да"],
                "LABEL_CHECKBOX": "",
            },
        )
    if field_type == "datetime":
        return (
            "datetime",
            {
                "DEFAULT_VALUE": {"TYPE": "NONE", "VALUE": ""},
                "USE_SECOND": "Y",
                "USE_TIMEZONE": "N",
            },
        )
    if field_type == "url":
        return "url", {"DEFAULT_VALUE": ""}
    raise RuntimeError(f"Unsupported expertise field type: {field_type}")


def _spec_searchable(spec: dict[str, Any]) -> bool:
    return bool(spec.get("searchable", False))


def _spec_edit_in_list(spec: dict[str, Any]) -> bool:
    return bool(spec.get("edit_in_list", True))


def _spec_show_filter(spec: dict[str, Any]) -> str:
    return "E" if _spec_searchable(spec) else "N"


def _enum_options_for_spec(spec: dict[str, Any]) -> list[dict[str, Any]]:
    options = spec.get("enum") or []
    result: list[dict[str, Any]] = []
    for index, option in enumerate(options, start=1):
        if isinstance(option, str):
            value = option
            xml_id = _slug_suffix(option)
            is_default = False
        else:
            value = str(option["value"])
            xml_id = str(option.get("xml_id") or option.get("xmlId") or _slug_suffix(value))
            is_default = bool(option.get("default", False))
        result.append(
            {
                "value": value,
                "xmlId": xml_id,
                "def": "Y" if is_default else "N",
                "sort": 100 + index * 100,
            }
        )
    return result


def _enum_map_from_field(field: dict[str, Any]) -> dict[str, str]:
    enum_items = field.get("enum") or []
    result: dict[str, str] = {}
    for item in enum_items:
        xml_id = str(item.get("xmlId") or item.get("XML_ID") or "").strip()
        enum_id = str(item.get("id") or item.get("ID") or "").strip()
        if xml_id and enum_id:
            result[xml_id] = enum_id
    return result


def ensure_custom_fields(
    webhook_base: str,
    *,
    process_type: dict[str, Any],
) -> list[dict[str, Any]]:
    type_id = int(process_type["id"])
    entity_id = smart_process_userfield_entity_id(type_id)
    existing_fields = list_userfields(webhook_base, entity_id=entity_id)
    existing_by_name = {str(item.get("fieldName") or ""): item for item in existing_fields}
    existing_by_xml_id = {
        str(item.get("xmlId") or ""): item for item in existing_fields if item.get("xmlId")
    }
    sync_rows: list[dict[str, Any]] = []

    for index, spec in enumerate(CUSTOM_FIELD_SPECS, start=1):
        title = spec["title"]
        field_name = _field_name_for_spec(entity_id, spec)
        xml_id = _field_xml_id_for_spec(spec)
        user_type_id, settings = _field_config_for_spec(spec)
        current = existing_by_xml_id.get(xml_id) or existing_by_name.get(field_name)

        if current is None:
            field = {
                "entityId": entity_id,
                "fieldName": field_name,
                "userTypeId": user_type_id,
                "xmlId": xml_id,
                "multiple": "N",
                "mandatory": "N",
                "showFilter": _spec_show_filter(spec),
                "isSearchable": "Y" if _spec_searchable(spec) else "N",
                "editInList": "Y" if _spec_edit_in_list(spec) else "N",
                "sort": 100 + index * 10,
                "settings": settings,
            }
            if user_type_id == "enumeration":
                field["enum"] = _enum_options_for_spec(spec)
            response = bitrix_call(
                webhook_base,
                "userfieldconfig.add",
                {"moduleId": "crm", "field": field},
            )
            current = (response.get("result") or {}).get("field") or {}
            sync_action = "created"
        else:
            sync_action = "updated"

        bitrix_call(
            webhook_base,
            "userfieldconfig.update",
            {
                "moduleId": "crm",
                "id": current["id"],
                "field": {
                    "userTypeId": user_type_id,
                    "languageId": "ru",
                    "xmlId": xml_id,
                    "showFilter": _spec_show_filter(spec),
                    "isSearchable": "Y" if _spec_searchable(spec) else "N",
                    "editInList": "Y" if _spec_edit_in_list(spec) else "N",
                    "editFormLabel": {"ru": title},
                    "listColumnLabel": {"ru": title},
                    "listFilterLabel": {"ru": title},
                },
            },
        )
        if user_type_id == "enumeration":
            bitrix_call(
                webhook_base,
                "userfieldconfig.update",
                {
                    "moduleId": "crm",
                    "id": current["id"],
                    "field": {
                        "userTypeId": "enumeration",
                        "enum": _enum_options_for_spec(spec),
                    },
                },
            )
            refreshed_response = bitrix_call(
                webhook_base,
                "userfieldconfig.get",
                {"moduleId": "crm", "id": current["id"]},
            )
            current = (refreshed_response.get("result") or {}).get("field") or current
        sync_rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": title,
                "field_name": current.get("fieldName") or field_name,
                "field_id": current.get("id"),
                "xml_id": xml_id,
                "enum_map": _enum_map_from_field(current),
                "action": sync_action,
            }
        )
        refreshed = {
            "id": current.get("id"),
            "fieldName": current.get("fieldName") or field_name,
            "userTypeId": current.get("userTypeId") or user_type_id,
            "xmlId": xml_id,
        }
        existing_by_name[refreshed["fieldName"]] = refreshed
        existing_by_xml_id[xml_id] = refreshed

    return sync_rows


def discover_field_mapping(
    webhook_base: str,
    process_type: dict[str, Any],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    entity_id = smart_process_userfield_entity_id(int(process_type["id"]))
    fields = list_userfields(webhook_base, entity_id=entity_id)
    fields_by_name = {str(item.get("fieldName") or ""): item for item in fields}
    fields_by_xml_id = {str(item.get("xmlId") or ""): item for item in fields if item.get("xmlId")}
    resolved = dict(BUILTIN_FIELD_MAPPING)
    spec_rows: list[dict[str, Any]] = []
    for spec in CUSTOM_FIELD_SPECS:
        field_name = _field_name_for_spec(entity_id, spec)
        xml_id = _field_xml_id_for_spec(spec)
        field_info = fields_by_xml_id.get(xml_id) or fields_by_name.get(field_name)
        field_code = str((field_info or {}).get("fieldName") or "") or None
        actual_type = None
        type_matches = None
        if field_code:
            actual_type = str((field_info or {}).get("userTypeId") or "").strip() or None
            type_matches = field_type_matches_expected(spec["type"], actual_type)
            resolved[spec["logical_key"]] = field_code
        spec_rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": spec["title"],
                "type": spec["type"],
                "required": spec["required"],
                "bitrix_code": field_code,
                "found": bool(field_code),
                "actual_type": actual_type,
                "type_matches": type_matches,
            }
        )
    return resolved, spec_rows


def build_mapping_payload(
    *,
    process_type: dict[str, Any],
    category: dict[str, Any],
    stages: dict[str, dict[str, Any]],
    field_mapping: dict[str, str],
    field_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    entity_type_id = int(process_type["entityTypeId"])
    category_id = int(category["id"])
    stage_map = {
        key: str(value.get("STATUS_ID") or "")
        for key, value in stages.items()
        if value.get("STATUS_ID")
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "process": {
            "title": process_type.get("title"),
            "code": process_type.get("code"),
            "type_id": int(process_type["id"]),
            "entity_type_id": entity_type_id,
            "category_id": category_id,
            "stage_entity_id": _status_entity_id(entity_type_id, category_id),
        },
        "stage_map": stage_map,
        "fields": field_mapping,
        "field_specs": field_specs,
        "stages": {
            key: {
                "id": value.get("ID"),
                "status_id": value.get("STATUS_ID"),
                "name": value.get("NAME"),
                "semantics": value.get("SEMANTICS"),
                "sort": value.get("SORT"),
            }
            for key, value in stages.items()
        },
        "missing_fields": [
            item["title"] for item in field_specs if item["required"] and not item["found"]
        ],
        "type_mismatches": [
            {
                "title": item["title"],
                "expected_type": item["type"],
                "actual_type": item["actual_type"],
                "bitrix_code": item["bitrix_code"],
            }
            for item in field_specs
            if item["found"] and item["type_matches"] is False
        ],
        "notes": [
            "Пользовательские поля smart-process синхронизируются через userfieldconfig для entityId вида CRM_<type_id>.",
            "Маппинг custom fields строится по fieldName/xmlId, а не по crm.item.fields.",
            "Общий макет карточки настраивается автоматически через crm.item.details.configuration.set.",
            "Поле assigned_by использует встроенное ASSIGNED_BY_ID.",
        ],
    }


def save_mapping(mapping: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def detail_element_name_for_logical_key(
    logical_key: str,
    *,
    mapping: dict[str, Any],
) -> str | None:
    builtin = DETAIL_BUILTIN_ELEMENT_NAMES.get(logical_key)
    if builtin:
        return builtin
    field_code = (mapping.get("fields") or {}).get(logical_key)
    if not field_code:
        return None
    return field_code


def build_details_configuration(*, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    configuration: list[dict[str, Any]] = []
    for section_spec in DETAIL_SECTION_SPECS:
        section_elements: list[dict[str, Any]] = []
        for logical_key in section_spec["elements"]:
            detail_name = detail_element_name_for_logical_key(logical_key, mapping=mapping)
            if not detail_name:
                continue
            section_elements.append({"name": detail_name, "optionFlags": 1})
        if not section_elements:
            continue
        configuration.append(
            {
                "name": section_spec["name"],
                "title": section_spec["title"],
                "type": "section",
                "elements": section_elements,
            }
        )
    return configuration


def save_details_configuration(
    configuration: list[dict[str, Any]],
    path: Path = DEFAULT_DETAILS_CONFIG_PATH,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ensure_common_details_configuration(
    webhook_base: str,
    *,
    mapping: dict[str, Any],
    path: Path = DEFAULT_DETAILS_CONFIG_PATH,
) -> tuple[list[dict[str, Any]], Path]:
    entity_type_id = int(mapping["process"]["entity_type_id"])
    category_id = int(mapping["process"]["category_id"])
    configuration = build_details_configuration(mapping=mapping)
    if not configuration:
        raise RuntimeError("Не удалось собрать конфигурацию карточки Bitrix: нет ни одной секции")

    bitrix_call_json(
        webhook_base,
        "crm.item.details.configuration.set",
        {
            "entityTypeId": entity_type_id,
            "scope": "C",
            "extras": {"categoryId": category_id},
            "data": configuration,
        },
    )
    bitrix_call_json(
        webhook_base,
        "crm.item.details.configuration.forcecommonscopeforall",
        {"entityTypeId": entity_type_id, "extras": {"categoryId": category_id}},
    )
    verification = bitrix_call_json(
        webhook_base,
        "crm.item.details.configuration.get",
        {"entityTypeId": entity_type_id, "scope": "C", "extras": {"categoryId": category_id}},
    )
    applied_configuration = verification.get("result") or configuration
    saved_path = save_details_configuration(applied_configuration, path=path)
    return applied_configuration, saved_path


def main() -> None:
    args = parse_args()
    settings = load_settings()
    webhook_base = settings.expertise_bitrix_webhook_url
    if not webhook_base:
        raise RuntimeError("Не задан EXPERTISE_BITRIX_WEBHOOK_URL в pricing-service/.env")

    current_user = bitrix_call(webhook_base, "user.current").get("result") or {}
    process_type = ensure_type(webhook_base, title=args.title, code=args.code)
    entity_type_id = int(process_type["entityTypeId"])
    category = ensure_category(
        webhook_base,
        entity_type_id=entity_type_id,
        name=args.category_name,
    )
    stages = ensure_stages(
        webhook_base,
        entity_type_id=entity_type_id,
        category_id=int(category["id"]),
    )
    created_fields = ensure_custom_fields(
        webhook_base,
        process_type=process_type,
    )
    field_mapping, field_specs = discover_field_mapping(
        webhook_base,
        process_type,
    )
    mapping = build_mapping_payload(
        process_type=process_type,
        category=category,
        stages=stages,
        field_mapping=field_mapping,
        field_specs=field_specs,
    )
    save_mapping(mapping, path=args.mapping_path)
    details_configuration, details_config_path = ensure_common_details_configuration(
        webhook_base,
        mapping=mapping,
    )

    summary = {
        "process_title": process_type.get("title"),
        "type_id": process_type.get("id"),
        "entity_type_id": process_type.get("entityTypeId"),
        "category_id": category.get("id"),
        "current_webhook_user_id": current_user.get("ID"),
        "current_webhook_user_name": " ".join(
            item for item in [current_user.get("NAME"), current_user.get("LAST_NAME")] if item
        ),
        "mapping_path": str(args.mapping_path),
        "details_configuration_path": str(details_config_path),
        "details_sections": [
            str(item.get("title") or item.get("name") or "") for item in details_configuration
        ],
        "created_fields": created_fields,
        "env_ready": {
            "EXPERTISE_BITRIX_ENTITY_TYPE_ID": int(process_type["entityTypeId"]),
            "EXPERTISE_BITRIX_CATEGORY_ID": int(category["id"]),
            "EXPERTISE_BITRIX_STAGE_MAP": mapping["stage_map"],
        },
        "missing_fields": mapping.get("missing_fields"),
        "type_mismatches": mapping.get("type_mismatches"),
        "field_map_found_count": len(field_mapping),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
