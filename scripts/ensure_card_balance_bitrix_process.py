#!/usr/bin/env python3
"""Create/update Bitrix Box smart-process for card balance reconciliation."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings  # noqa: E402

DEFAULT_MAPPING_PATH = REPO_ROOT / "build/bitrix/card_balance_reconciliation_mapping.json"
DEFAULT_DETAILS_CONFIG_PATH = (
    REPO_ROOT / "build/bitrix/card_balance_reconciliation_details_configuration.json"
)
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/card_balance_reconciliation_ensure_result.json"
DEFAULT_PROCESS_TITLE = "Сверка балансов карт менеджеров"
DEFAULT_PROCESS_CODE = "card_balance_reconciliation"
DEFAULT_CATEGORY_NAME = "Ежедневная сверка"

STAGE_SPECS = [
    {
        "logical_key": "waiting_screenshot",
        "code": "NEW",
        "name": "Ожидаем скрин",
        "sort": 100,
        "semantics": None,
    },
    {
        "logical_key": "screenshot_received",
        "code": "RECEIVED",
        "name": "Скрин получен",
        "sort": 200,
        "semantics": None,
    },
    {
        "logical_key": "recognition",
        "code": "RECOGNITION",
        "name": "Распознавание",
        "sort": 300,
        "semantics": None,
    },
    {
        "logical_key": "mismatch",
        "code": "MISMATCH",
        "name": "Расхождение",
        "sort": 400,
        "semantics": None,
    },
    {
        "logical_key": "manual_review",
        "code": "MANUAL",
        "name": "Требует ручной проверки",
        "sort": 500,
        "semantics": None,
    },
    {
        "logical_key": "closed_fincontrol",
        "code": "CLOSED",
        "name": "Закрыто финконтролем",
        "sort": 600,
        "semantics": None,
    },
    {
        "logical_key": "overdue",
        "code": "OVERDUE",
        "name": "Просрочено",
        "sort": 700,
        "semantics": None,
    },
    {
        "logical_key": "matched",
        "code": "MATCHED",
        "name": "Сошлось с 1С",
        "sort": 900,
        "semantics": "S",
    },
    {
        "logical_key": "cancelled",
        "code": "CANCELLED",
        "name": "Отменено / неактивная карта",
        "sort": 1000,
        "semantics": "F",
    },
]

BUILTIN_FIELD_MAPPING = {"title": "TITLE", "assigned_by": "ASSIGNED_BY_ID"}

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "business_date",
        "title": "Дата сверки",
        "type": "date",
        "required": True,
        "searchable": True,
    },
    {
        "logical_key": "employee_user",
        "title": "Сотрудник",
        "type": "employee",
        "required": False,
        "searchable": True,
    },
    {
        "logical_key": "employee_id",
        "title": "Тех. ID сотрудника",
        "type": "string",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "employee_name",
        "title": "Сотрудник (текст)",
        "type": "string",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "employee_last_name",
        "title": "Фамилия сотрудника",
        "type": "string",
        "required": False,
        "searchable": True,
    },
    {
        "logical_key": "card_last4",
        "title": "Последние 4 цифры карты",
        "type": "string",
        "required": False,
        "searchable": True,
    },
    {
        "logical_key": "onec_cashbox_code",
        "title": "Код кассы 1С",
        "type": "string",
        "required": False,
        "searchable": True,
    },
    {
        "logical_key": "onec_cashbox_name",
        "title": "Название кассы 1С",
        "type": "string",
        "required": False,
        "searchable": True,
    },
    {
        "logical_key": "screenshot_file",
        "title": "Скрин баланса",
        "type": "file",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "manual_balance",
        "title": "Баланс введен вручную",
        "type": "double",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "recognized_balance",
        "title": "Баланс распознан OCR",
        "type": "double",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "recognition_confidence",
        "title": "Уверенность OCR",
        "type": "double",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "onec_balance",
        "title": "Остаток кассы в 1С",
        "type": "double",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "diff_amount",
        "title": "Разница",
        "type": "double",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "status",
        "title": "Тех. статус backend",
        "type": "string",
        "required": True,
        "searchable": True,
    },
    {
        "logical_key": "reviewer_id",
        "title": "Тех. ID проверяющего",
        "type": "string",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "resolution_comment",
        "title": "Комментарий финконтроля",
        "type": "text",
        "required": False,
        "searchable": False,
    },
    {
        "logical_key": "due_at",
        "title": "Срок закрытия",
        "type": "datetime",
        "required": False,
        "searchable": False,
    },
]

DETAIL_SECTION_SPECS = [
    {
        "name": "main",
        "title": "Сверка",
        "elements": [
            "business_date",
            "employee_user",
            "card_last4",
            "screenshot_file",
            "manual_balance",
        ],
    },
    {
        "name": "onec",
        "title": "1С",
        "elements": [
            "onec_cashbox_code",
            "onec_cashbox_name",
            "onec_balance",
            "diff_amount",
            "status",
        ],
    },
    {
        "name": "review",
        "title": "Разбор",
        "elements": [
            "recognized_balance",
            "recognition_confidence",
            "resolution_comment",
            "due_at",
        ],
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure card balance reconciliation Bitrix smart-process."
    )
    parser.add_argument("--title", default=DEFAULT_PROCESS_TITLE)
    parser.add_argument("--code", default=DEFAULT_PROCESS_CODE)
    parser.add_argument("--category-name", default=DEFAULT_CATEGORY_NAME)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--details-config-path", type=Path, default=DEFAULT_DETAILS_CONFIG_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--skip-details-config", action="store_true")
    parser.add_argument(
        "--skip-category-cleanup",
        action="store_true",
        help="Do not delete extra empty categories for this smart-process.",
    )
    parser.add_argument(
        "--skip-stage-cleanup",
        action="store_true",
        help="Do not delete extra stages in the selected category.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write planned mapping/details without calling Bitrix.",
    )
    return parser.parse_args()


def bitrix_call(
    webhook_base: str, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = urllib.parse.urlencode(_flatten_params(params or {})).encode("utf-8")
    request = urllib.request.Request(
        webhook_base.rstrip("/") + f"/{method}.json",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bitrix API {method}: HTTP {exc.code} {body[:1000]}") from exc
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
    request = urllib.request.Request(
        webhook_base.rstrip("/") + f"/{method}.json",
        data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bitrix API {method}: HTTP {exc.code} {body[:1000]}") from exc
    if parsed.get("error"):
        raise RuntimeError(
            f"Bitrix API {method}: {parsed['error']} {parsed.get('error_description', '')}".strip()
        )
    return parsed


def _flatten_params(params: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for key, value in params.items():
        result.extend(_flatten_param(key, value))
    return result


def _flatten_param(prefix: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for child_key, child_value in value.items():
            result.extend(_flatten_param(f"{prefix}[{child_key}]", child_value))
        return result
    if isinstance(value, list):
        result = []
        for child_value in value:
            result.extend(_flatten_param(f"{prefix}[]", child_value))
        return result
    if value is None:
        return [(prefix, "")]
    return [(prefix, str(value))]


def find_type_by_title(webhook_base: str, title: str) -> dict[str, Any] | None:
    response = bitrix_call(webhook_base, "crm.type.list", {"filter": {"title": title}})
    types = (response.get("result") or {}).get("types") or []
    return next((item for item in types if item.get("title") == title), None)


def ensure_type(webhook_base: str, *, title: str, code: str) -> dict[str, Any]:
    existing = find_type_by_title(webhook_base, title)
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
        "isLinkWithProductsEnabled": "N",
    }
    if existing:
        bitrix_call(webhook_base, "crm.type.update", {"id": existing["id"], "fields": fields})
        return find_type_by_title(webhook_base, title) or existing
    response = bitrix_call(webhook_base, "crm.type.add", {"fields": fields})
    item = (response.get("result") or {}).get("type")
    if not item:
        raise RuntimeError("crm.type.add returned empty result")
    return item


def ensure_category(webhook_base: str, *, entity_type_id: int, name: str) -> dict[str, Any]:
    response = bitrix_call(webhook_base, "crm.category.list", {"entityTypeId": entity_type_id})
    categories = (response.get("result") or {}).get("categories") or []
    existing = next((item for item in categories if item.get("name") == name), None)
    if existing:
        return existing
    response = bitrix_call(
        webhook_base, "crm.category.add", {"entityTypeId": entity_type_id, "fields": {"name": name}}
    )
    return (response.get("result") or {}).get("category") or {}


def list_categories(webhook_base: str, *, entity_type_id: int) -> list[dict[str, Any]]:
    response = bitrix_call(webhook_base, "crm.category.list", {"entityTypeId": entity_type_id})
    return (response.get("result") or {}).get("categories") or []


def _category_has_items(webhook_base: str, *, entity_type_id: int, category_id: int) -> bool:
    response = bitrix_call(
        webhook_base,
        "crm.item.list",
        {
            "entityTypeId": entity_type_id,
            "select": ["id"],
            "filter": {"categoryId": category_id},
            "start": 0,
        },
    )
    items = (response.get("result") or {}).get("items") or []
    return bool(items)


def cleanup_extra_categories(
    webhook_base: str,
    *,
    entity_type_id: int,
    keep_category_id: int,
) -> dict[str, list[dict[str, Any]]]:
    deleted: list[dict[str, Any]] = []
    skipped_non_empty: list[dict[str, Any]] = []
    skipped_error: list[dict[str, Any]] = []
    categories = list_categories(webhook_base, entity_type_id=entity_type_id)
    for category in categories:
        category_id = int(category.get("id") or 0)
        if category_id == keep_category_id:
            continue
        if _category_has_items(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
        ):
            skipped_non_empty.append({"id": category_id, "name": str(category.get("name") or "")})
            continue
        try:
            bitrix_call(
                webhook_base,
                "crm.category.delete",
                {"entityTypeId": entity_type_id, "id": category_id},
            )
            deleted.append({"id": category_id, "name": str(category.get("name") or "")})
        except Exception as exc:
            skipped_error.append(
                {
                    "id": category_id,
                    "name": str(category.get("name") or ""),
                    "error": str(exc),
                }
            )
    return {
        "deleted": deleted,
        "skipped_non_empty": skipped_non_empty,
        "skipped_error": skipped_error,
    }


def _status_entity_id(entity_type_id: int, category_id: int) -> str:
    return f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"


def list_stages(
    webhook_base: str, *, entity_type_id: int, category_id: int
) -> list[dict[str, Any]]:
    response = bitrix_call(
        webhook_base,
        "crm.status.list",
        {"filter": {"ENTITY_ID": _status_entity_id(entity_type_id, category_id)}},
    )
    return response.get("result") or []


def ensure_stages(
    webhook_base: str, *, entity_type_id: int, category_id: int
) -> dict[str, dict[str, Any]]:
    entity_id = _status_entity_id(entity_type_id, category_id)
    stages = list_stages(webhook_base, entity_type_id=entity_type_id, category_id=category_id)
    by_status = {item.get("STATUS_ID"): item for item in stages}
    for stage in stages:
        if stage.get("SEMANTICS") == "S":
            bitrix_call(
                webhook_base,
                "crm.status.update",
                {"id": stage["ID"], "fields": {"SORT": 990}},
            )
        elif stage.get("SEMANTICS") == "F":
            bitrix_call(
                webhook_base,
                "crm.status.update",
                {"id": stage["ID"], "fields": {"SORT": 1000}},
            )
    stages = list_stages(webhook_base, entity_type_id=entity_type_id, category_id=category_id)
    by_status = {item.get("STATUS_ID"): item for item in stages}
    desired: dict[str, dict[str, Any]] = {}
    ordered_specs = [spec for spec in STAGE_SPECS if spec.get("semantics") is None] + [
        spec for spec in STAGE_SPECS if spec.get("semantics") is not None
    ]
    for spec in ordered_specs:
        status_id = f"DT{entity_type_id}_{category_id}:{spec['code']}"
        current = by_status.get(status_id)
        if current is None and spec.get("semantics") is not None:
            current = next(
                (stage for stage in stages if stage.get("SEMANTICS") == spec.get("semantics")),
                None,
            )
        fields = {"NAME": spec["name"], "SORT": spec["sort"]}
        if spec.get("semantics") is not None:
            fields["SEMANTICS"] = spec["semantics"]
        if current:
            bitrix_call(webhook_base, "crm.status.update", {"id": current["ID"], "fields": fields})
            desired[spec["logical_key"]] = {**current, **fields}
        else:
            bitrix_call(
                webhook_base,
                "crm.status.add",
                {"fields": {"ENTITY_ID": entity_id, "STATUS_ID": status_id, **fields}},
            )
            desired[spec["logical_key"]] = next(
                item
                for item in list_stages(
                    webhook_base, entity_type_id=entity_type_id, category_id=category_id
                )
                if item.get("STATUS_ID") == status_id
            )
    return desired


def _list_stage_item_ids(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
    stage_id: str,
) -> list[int]:
    collected: list[int] = []
    start: int | None = 0
    while True:
        params: dict[str, Any] = {
            "entityTypeId": entity_type_id,
            "filter": {"categoryId": category_id, "stageId": stage_id},
            "select": ["id"],
        }
        if start is not None:
            params["start"] = start
        response = bitrix_call(webhook_base, "crm.item.list", params)
        result = response.get("result") or {}
        items = result.get("items") or []
        for item in items:
            try:
                collected.append(int(item.get("id")))
            except (TypeError, ValueError):
                continue
        next_start = response.get("next")
        if next_start is None or not items:
            break
        start = int(next_start)
    return collected


def cleanup_extra_stages(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
    keep_status_ids: set[str],
    fallback_status_id: str | None,
) -> dict[str, Any]:
    stages = list_stages(webhook_base, entity_type_id=entity_type_id, category_id=category_id)
    deleted: list[dict[str, Any]] = []
    moved_items: list[dict[str, Any]] = []
    skipped_non_empty: list[dict[str, Any]] = []
    skipped_error: list[dict[str, Any]] = []
    for stage in stages:
        status_id = str(stage.get("STATUS_ID") or "")
        if not status_id or status_id in keep_status_ids:
            continue
        stage_db_id = stage.get("ID")
        stage_name = str(stage.get("NAME") or "")
        try:
            item_ids = _list_stage_item_ids(
                webhook_base,
                entity_type_id=entity_type_id,
                category_id=category_id,
                stage_id=status_id,
            )
            if item_ids:
                if not fallback_status_id:
                    skipped_non_empty.append(
                        {
                            "id": stage_db_id,
                            "status_id": status_id,
                            "name": stage_name,
                            "item_count": len(item_ids),
                        }
                    )
                    continue
                for item_id in item_ids:
                    bitrix_call(
                        webhook_base,
                        "crm.item.update",
                        {
                            "entityTypeId": entity_type_id,
                            "id": item_id,
                            "fields": {"stageId": fallback_status_id},
                        },
                    )
                moved_items.append(
                    {
                        "from_stage_id": status_id,
                        "to_stage_id": fallback_status_id,
                        "item_count": len(item_ids),
                    }
                )
            bitrix_call(webhook_base, "crm.status.delete", {"id": stage_db_id, "FORCED": "Y"})
            deleted.append(
                {
                    "id": stage_db_id,
                    "status_id": status_id,
                    "name": stage_name,
                }
            )
        except Exception as exc:
            skipped_error.append(
                {
                    "id": stage_db_id,
                    "status_id": status_id,
                    "name": stage_name,
                    "error": str(exc),
                }
            )
    return {
        "deleted": deleted,
        "moved_items": moved_items,
        "skipped_non_empty": skipped_non_empty,
        "skipped_error": skipped_error,
    }


def smart_process_userfield_entity_id(type_id: int) -> str:
    return f"CRM_{type_id}"


def list_userfields(webhook_base: str, *, entity_id: str) -> list[dict[str, Any]]:
    response = bitrix_call(
        webhook_base, "userfieldconfig.list", {"moduleId": "crm", "filter": {"entityId": entity_id}}
    )
    return (response.get("result") or {}).get("fields") or []


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9_]+", "_", value.upper())).strip("_")[:32]


def _field_name(entity_id: str, logical_key: str) -> str:
    return f"UF_{entity_id}_{_slug(logical_key).replace('_', '')}"


def _xml_id(logical_key: str) -> str:
    return f"UF_CRM_CARD_BALANCE_{_slug(logical_key)}"


def _field_config(field_type: str) -> tuple[str, dict[str, Any]]:
    if field_type == "string":
        return "string", {"DEFAULT_VALUE": "", "SIZE": 20, "ROWS": 1}
    if field_type == "text":
        return "string", {"DEFAULT_VALUE": "", "SIZE": 50, "ROWS": 5}
    if field_type == "double":
        return "double", {"SIZE": 20, "PRECISION": 2, "DEFAULT_VALUE": None}
    if field_type == "date":
        return "date", {"DEFAULT_VALUE": {"TYPE": "NONE", "VALUE": ""}}
    if field_type == "datetime":
        return "datetime", {
            "DEFAULT_VALUE": {"TYPE": "NONE", "VALUE": ""},
            "USE_SECOND": "Y",
            "USE_TIMEZONE": "N",
        }
    if field_type == "file":
        return "file", {"SIZE": 20, "LIST_WIDTH": 0, "LIST_HEIGHT": 0, "MAX_ALLOWED_SIZE": 0}
    if field_type == "employee":
        return "employee", {}
    raise RuntimeError(f"Unsupported field type: {field_type}")


def ensure_custom_fields(
    webhook_base: str, *, process_type: dict[str, Any]
) -> list[dict[str, Any]]:
    entity_id = smart_process_userfield_entity_id(int(process_type["id"]))
    existing = list_userfields(webhook_base, entity_id=entity_id)
    by_xml = {str(item.get("xmlId") or ""): item for item in existing if item.get("xmlId")}
    rows: list[dict[str, Any]] = []
    for index, spec in enumerate(CUSTOM_FIELD_SPECS, start=1):
        user_type_id, settings = _field_config(spec["type"])
        xml_id = _xml_id(spec["logical_key"])
        field = by_xml.get(xml_id)
        if field is None:
            response = bitrix_call(
                webhook_base,
                "userfieldconfig.add",
                {
                    "moduleId": "crm",
                    "field": {
                        "entityId": entity_id,
                        "fieldName": _field_name(entity_id, spec["logical_key"]),
                        "userTypeId": user_type_id,
                        "xmlId": xml_id,
                        "multiple": "N",
                        "mandatory": "N",
                        "showFilter": "E" if spec.get("searchable") else "N",
                        "isSearchable": "Y" if spec.get("searchable") else "N",
                        "editInList": "Y",
                        "sort": 100 + index * 10,
                        "settings": settings,
                    },
                },
            )
            field = (response.get("result") or {}).get("field") or {}
            action = "created"
        else:
            action = "updated"
        bitrix_call(
            webhook_base,
            "userfieldconfig.update",
            {
                "moduleId": "crm",
                "id": field["id"],
                "field": {
                    "languageId": "ru",
                    "xmlId": xml_id,
                    "editFormLabel": {"ru": spec["title"]},
                    "listColumnLabel": {"ru": spec["title"]},
                    "listFilterLabel": {"ru": spec["title"]},
                },
            },
        )
        rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": spec["title"],
                "field_name": field.get("fieldName"),
                "xml_id": xml_id,
                "action": action,
            }
        )
    return rows


def discover_field_mapping(
    webhook_base: str, *, process_type: dict[str, Any]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    entity_id = smart_process_userfield_entity_id(int(process_type["id"]))
    fields = list_userfields(webhook_base, entity_id=entity_id)
    by_xml = {str(item.get("xmlId") or ""): item for item in fields if item.get("xmlId")}
    resolved = dict(BUILTIN_FIELD_MAPPING)
    specs = []
    for spec in CUSTOM_FIELD_SPECS:
        field = by_xml.get(_xml_id(spec["logical_key"]))
        field_name = str((field or {}).get("fieldName") or "") or None
        if field_name:
            resolved[spec["logical_key"]] = field_name
        specs.append({**spec, "bitrix_code": field_name, "found": bool(field_name)})
    return resolved, specs


def build_mapping(
    *,
    process_type: dict[str, Any],
    category: dict[str, Any],
    stages: dict[str, dict[str, Any]],
    fields: dict[str, str],
    field_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "process": {
            "title": process_type.get("title"),
            "code": process_type.get("code"),
            "type_id": int(process_type["id"]),
            "entity_type_id": int(process_type["entityTypeId"]),
            "category_id": int(category["id"]),
        },
        "stage_map": {key: str(value.get("STATUS_ID") or "") for key, value in stages.items()},
        "fields": fields,
        "field_specs": field_specs,
        "missing_fields": [
            item["title"] for item in field_specs if item.get("required") and not item.get("found")
        ],
    }


def build_dry_run_mapping(*, title: str, code: str, category_name: str) -> dict[str, Any]:
    process_type = {"id": 0, "entityTypeId": 0, "title": title, "code": code}
    category = {"id": 0, "name": category_name}
    stages = {
        spec["logical_key"]: {
            "STATUS_ID": f"DT0_0:{spec['code']}",
            "NAME": spec["name"],
            "SORT": spec["sort"],
            "SEMANTICS": spec.get("semantics"),
        }
        for spec in STAGE_SPECS
    }
    fields = dict(BUILTIN_FIELD_MAPPING)
    fields.update(
        {spec["logical_key"]: _xml_id(spec["logical_key"]) for spec in CUSTOM_FIELD_SPECS}
    )
    field_specs = [
        {**spec, "bitrix_code": fields[spec["logical_key"]], "found": True}
        for spec in CUSTOM_FIELD_SPECS
    ]
    mapping = build_mapping(
        process_type=process_type,
        category=category,
        stages=stages,
        fields=fields,
        field_specs=field_specs,
    )
    mapping["dry_run"] = True
    return mapping


def _detail_name(logical_key: str, mapping: dict[str, Any]) -> str | None:
    return (mapping.get("fields") or {}).get(logical_key)


def build_details_configuration(*, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    configuration = []
    for section in DETAIL_SECTION_SPECS:
        elements = [
            {"name": name, "optionFlags": 1}
            for key in section["elements"]
            if (name := _detail_name(key, mapping))
        ]
        if elements:
            configuration.append(
                {
                    "name": section["name"],
                    "title": section["title"],
                    "type": "section",
                    "elements": elements,
                }
            )
    return configuration


def ensure_details_configuration(
    webhook_base: str, *, mapping: dict[str, Any], path: Path
) -> tuple[list[dict[str, Any]], Path]:
    configuration = build_details_configuration(mapping=mapping)
    bitrix_call_json(
        webhook_base,
        "crm.item.details.configuration.set",
        {
            "entityTypeId": mapping["process"]["entity_type_id"],
            "scope": "C",
            "extras": {"categoryId": mapping["process"]["category_id"]},
            "data": configuration,
        },
    )
    bitrix_call_json(
        webhook_base,
        "crm.item.details.configuration.forcecommonscopeforall",
        {
            "entityTypeId": mapping["process"]["entity_type_id"],
            "extras": {"categoryId": mapping["process"]["category_id"]},
        },
    )
    verification = bitrix_call_json(
        webhook_base,
        "crm.item.details.configuration.get",
        {
            "entityTypeId": mapping["process"]["entity_type_id"],
            "scope": "C",
            "extras": {"categoryId": mapping["process"]["category_id"]},
        },
    )
    applied = verification.get("result") or configuration
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(applied, ensure_ascii=False, indent=2), encoding="utf-8")
    return applied, path


def write_result(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.dry_run:
        mapping = build_dry_run_mapping(
            title=args.title,
            code=args.code,
            category_name=args.category_name,
        )
        args.mapping_path.parent.mkdir(parents=True, exist_ok=True)
        args.mapping_path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        details_path = None
        if not args.skip_details_config:
            configuration = build_details_configuration(mapping=mapping)
            args.details_config_path.parent.mkdir(parents=True, exist_ok=True)
            args.details_config_path.write_text(
                json.dumps(configuration, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            details_path = args.details_config_path
        result = {
            "dry_run": True,
            "process_title": args.title,
            "mapping_path": str(args.mapping_path),
            "details_configuration_path": (None if details_path is None else str(details_path)),
            "missing_fields": mapping["missing_fields"],
            "env_ready": {
                "CARD_BALANCE_BITRIX_ENTITY_TYPE_ID": 0,
                "CARD_BALANCE_BITRIX_CATEGORY_ID": 0,
                "CARD_BALANCE_BITRIX_STAGE_MAP": mapping["stage_map"],
                "CARD_BALANCE_BITRIX_FIELD_MAP": mapping["fields"],
            },
        }
        write_result(result, args.result_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    settings = get_settings()
    webhook_base = settings.card_balance_bitrix_webhook_url
    if not webhook_base:
        raise RuntimeError("Не задан CARD_BALANCE_BITRIX_WEBHOOK_URL в pricing-service/.env")
    process_type = ensure_type(webhook_base, title=args.title, code=args.code)
    category = ensure_category(
        webhook_base, entity_type_id=int(process_type["entityTypeId"]), name=args.category_name
    )
    cleanup_result = {"deleted": [], "skipped_non_empty": [], "skipped_error": []}
    if not args.skip_category_cleanup:
        cleanup_result = cleanup_extra_categories(
            webhook_base,
            entity_type_id=int(process_type["entityTypeId"]),
            keep_category_id=int(category["id"]),
        )
    stages = ensure_stages(
        webhook_base,
        entity_type_id=int(process_type["entityTypeId"]),
        category_id=int(category["id"]),
    )
    stage_cleanup_result = {
        "deleted": [],
        "moved_items": [],
        "skipped_non_empty": [],
        "skipped_error": [],
    }
    if not args.skip_stage_cleanup:
        keep_status_ids = {str(stage.get("STATUS_ID") or "") for stage in stages.values()}
        fallback_status_id = (
            str((stages.get("waiting_screenshot") or {}).get("STATUS_ID") or "") or None
        )
        stage_cleanup_result = cleanup_extra_stages(
            webhook_base,
            entity_type_id=int(process_type["entityTypeId"]),
            category_id=int(category["id"]),
            keep_status_ids=keep_status_ids,
            fallback_status_id=fallback_status_id,
        )
    created_fields = ensure_custom_fields(webhook_base, process_type=process_type)
    fields, field_specs = discover_field_mapping(webhook_base, process_type=process_type)
    mapping = build_mapping(
        process_type=process_type,
        category=category,
        stages=stages,
        fields=fields,
        field_specs=field_specs,
    )
    args.mapping_path.parent.mkdir(parents=True, exist_ok=True)
    args.mapping_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    details_path = None
    if not args.skip_details_config:
        _, details_path = ensure_details_configuration(
            webhook_base, mapping=mapping, path=args.details_config_path
        )
    result = {
        "process_title": process_type.get("title"),
        "type_id": process_type.get("id"),
        "entity_type_id": process_type.get("entityTypeId"),
        "category_id": category.get("id"),
        "category_cleanup": cleanup_result,
        "stage_cleanup": stage_cleanup_result,
        "mapping_path": str(args.mapping_path),
        "details_configuration_path": None if details_path is None else str(details_path),
        "created_fields": created_fields,
        "missing_fields": mapping["missing_fields"],
        "env_ready": {
            "CARD_BALANCE_BITRIX_ENTITY_TYPE_ID": int(process_type["entityTypeId"]),
            "CARD_BALANCE_BITRIX_CATEGORY_ID": int(category["id"]),
            "CARD_BALANCE_BITRIX_STAGE_MAP": mapping["stage_map"],
            "CARD_BALANCE_BITRIX_FIELD_MAP": mapping["fields"],
        },
    }
    write_result(result, args.result_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
