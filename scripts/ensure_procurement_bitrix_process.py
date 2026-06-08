#!/usr/bin/env python3
"""Ensure Bitrix smart-process setup for procurement order contours.

Dry-run by default. Use --apply to update Bitrix Box.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.ensure_expertise_bitrix_process as bitrix_setup  # noqa: E402

DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_MAPPING_PATH = REPO_ROOT / "build/bitrix/procurement_order_mapping.json"
DEFAULT_DETAILS_CONFIG_PATH = REPO_ROOT / "build/bitrix/procurement_order_details_configuration.json"
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/procurement_order_ensure_result.json"
DEFAULT_PROCESS_TITLE = "Закупка/Заказ"
DEFAULT_PROCESS_CODE = "procurement_order"

CONTOUR_ENUM = [
    {"xml_id": "ordinary", "value": "Обычный", "default": True},
    {"xml_id": "cargo", "value": "Cargo"},
    {"xml_id": "ved_import", "value": "ВЭД импорт"},
]

CATEGORY_SPECS = [
    {
        "logical_key": "ordinary",
        "name": "Обычный",
        "sort": 100,
        "reuse_default": False,
        "stages": [
            {"logical_key": "need", "code": "NEED", "name": "Потребность", "sort": 100},
            {
                "logical_key": "supplier_terms",
                "code": "TERMS",
                "name": "Согласование условий",
                "sort": 200,
            },
            {
                "logical_key": "supplier_order",
                "code": "ORDER",
                "name": "Заказ поставщику",
                "sort": 300,
            },
            {
                "logical_key": "waiting_delivery",
                "code": "WAITING",
                "name": "Ожидаем поставку",
                "sort": 400,
            },
            {
                "logical_key": "receiving",
                "code": "RECEIVING",
                "name": "Приемка на склад",
                "sort": 500,
            },
            {"logical_key": "closed", "code": "CLOSED", "name": "Закрыто", "sort": 900, "semantics": "S"},
            {
                "logical_key": "cancelled",
                "code": "CANCELLED",
                "name": "Отменено / не пошло",
                "sort": 1000,
                "semantics": "F",
            },
        ],
    },
    {
        "logical_key": "cargo",
        "name": "Cargo",
        "sort": 200,
        "reuse_default": False,
        "stages": [
            {"logical_key": "need", "code": "NEED", "name": "Потребность", "sort": 100},
            {
                "logical_key": "supplier_terms",
                "code": "TERMS",
                "name": "Согласование с поставщиком",
                "sort": 200,
            },
            {
                "logical_key": "supplier_order",
                "code": "ORDER",
                "name": "Заказан товар",
                "sort": 300,
            },
            {
                "logical_key": "cargo_dropoff",
                "code": "CARGO",
                "name": "Сдано в карго",
                "sort": 400,
            },
            {"logical_key": "in_transit", "code": "TRANSIT", "name": "В пути", "sort": 500},
            {
                "logical_key": "receiving",
                "code": "RECEIVING",
                "name": "Приемка на склад",
                "sort": 600,
            },
            {"logical_key": "closed", "code": "CLOSED", "name": "Закрыто", "sort": 900, "semantics": "S"},
            {
                "logical_key": "exception",
                "code": "EXCEPTION",
                "name": "Проблема / отмена",
                "sort": 1000,
                "semantics": "F",
            },
        ],
    },
    {
        "logical_key": "ved_import",
        "name": "ВЭД импорт",
        "sort": 300,
        "reuse_default": True,
        "stages": [
            {"logical_key": "need", "code": "NEED", "name": "Потребность", "sort": 100},
            {
                "logical_key": "docs_collection",
                "code": "DOCS",
                "name": "Сбор документов",
                "sort": 200,
            },
            {
                "logical_key": "docs_checked",
                "code": "DOCS_OK",
                "name": "Документы проверены",
                "sort": 300,
            },
            {
                "logical_key": "supplier_order",
                "code": "ORDER",
                "name": "Заказ поставщику",
                "sort": 400,
            },
            {
                "logical_key": "payment_agent",
                "code": "PAYMENT",
                "name": "Оплата / платежный агент",
                "sort": 500,
            },
            {
                "logical_key": "logistics_customs",
                "code": "LOGISTICS",
                "name": "Логистика / таможня",
                "sort": 600,
            },
            {
                "logical_key": "customs_clearance",
                "code": "CUSTOMS",
                "name": "Растаможка",
                "sort": 700,
            },
            {
                "logical_key": "receiving",
                "code": "RECEIVING",
                "name": "Приемка на склад",
                "sort": 800,
            },
            {"logical_key": "closed", "code": "CLOSED", "name": "Закрыто", "sort": 900, "semantics": "S"},
            {
                "logical_key": "blocked",
                "code": "BLOCKED",
                "name": "Блокер / отмена",
                "sort": 1000,
                "semantics": "F",
            },
        ],
    },
]

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "procurement_contour",
        "title": "Контур закупки",
        "type": "enumeration",
        "enum": CONTOUR_ENUM,
        "required": True,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "pilot_batch_id",
        "title": "Партия / batch id",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "onec_source_type",
        "title": "1С тип документа",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "onec_source_number",
        "title": "1С номер документа",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "onec_source_date",
        "title": "1С дата документа",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "onec_posted",
        "title": "1С проведен",
        "type": "boolean",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "supplier_company",
        "title": "Поставщик (CRM)",
        "type": "crm_company",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "broker_company",
        "title": "Брокер / логист (CRM)",
        "type": "crm_company",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "payment_agent_company",
        "title": "Платежный агент (CRM)",
        "type": "crm_company",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "currency",
        "title": "Валюта",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "amount",
        "title": "Сумма",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "planned_warehouse",
        "title": "Плановый склад",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "expected_receipt_date",
        "title": "Плановая дата поступления",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "expects_import_gtd",
        "title": "Ожидается ГТД по импорту",
        "type": "boolean",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "gtd_number",
        "title": "Номер ГТД",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "blocker_comment",
        "title": "Блокер / комментарий",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
]

BUILTIN_FIELD_MAPPING = {
    "title": "TITLE",
    "assigned_by": "ASSIGNED_BY_ID",
    "stage": "STAGE_ID",
}

DETAIL_SECTION_SPECS = [
    {
        "name": "main",
        "title": "Основное",
        "elements": [
            "title",
            "stage",
            "assigned_by",
            "procurement_contour",
            "pilot_batch_id",
        ],
    },
    {
        "name": "onec",
        "title": "Связь с 1С",
        "elements": [
            "onec_source_type",
            "onec_source_number",
            "onec_source_date",
            "onec_posted",
            "planned_warehouse",
            "expected_receipt_date",
        ],
    },
    {
        "name": "counterparties",
        "title": "Участники",
        "elements": [
            "supplier_company",
            "broker_company",
            "payment_agent_company",
        ],
    },
    {
        "name": "money_customs",
        "title": "Деньги и таможня",
        "elements": [
            "currency",
            "amount",
            "expects_import_gtd",
            "gtd_number",
            "blocker_comment",
        ],
    },
]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def field_xml_id_for_spec(spec: dict[str, Any]) -> str:
    return f"UF_CRM_PROCUREMENT_{bitrix_setup._slug_suffix(spec['logical_key'])}"


def field_config_for_spec(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if spec["type"] == "crm_company":
        return "crm", {"LEAD": "N", "CONTACT": "N", "COMPANY": "Y", "DEAL": "N"}
    return bitrix_setup._field_config_for_spec(spec)


def configure_generic_setup() -> None:
    bitrix_setup.CUSTOM_FIELD_SPECS = CUSTOM_FIELD_SPECS
    bitrix_setup.BUILTIN_FIELD_MAPPING = BUILTIN_FIELD_MAPPING
    bitrix_setup.DETAIL_SECTION_SPECS = DETAIL_SECTION_SPECS
    bitrix_setup._field_xml_id_for_spec = field_xml_id_for_spec


def desired_stage_specs(category_spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in category_spec["stages"]:
        rows.append({"semantics": None, **item})
    return rows


def list_categories(webhook_base: str, *, entity_type_id: int) -> list[dict[str, Any]]:
    response = bitrix_setup.bitrix_call(
        webhook_base,
        "crm.category.list",
        {"entityTypeId": entity_type_id},
    )
    return (response.get("result") or {}).get("categories") or []


def ensure_category_for_spec(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_spec: dict[str, Any],
) -> dict[str, Any]:
    categories = list_categories(webhook_base, entity_type_id=entity_type_id)
    name = str(category_spec["name"])
    existing = next((item for item in categories if str(item.get("name") or "") == name), None)
    if existing is None and category_spec.get("reuse_default"):
        existing = next((item for item in categories if str(item.get("isDefault") or "") == "Y"), None)
    if existing is None:
        response = bitrix_setup.bitrix_call(
            webhook_base,
            "crm.category.add",
            {
                "entityTypeId": entity_type_id,
                "fields": {"name": name, "sort": int(category_spec["sort"])},
            },
        )
        return (response.get("result") or {}).get("category") or {}

    fields: dict[str, Any] = {}
    if str(existing.get("name") or "") != name:
        fields["name"] = name
    if int(existing.get("sort") or 0) != int(category_spec["sort"]):
        fields["sort"] = int(category_spec["sort"])
    if fields:
        bitrix_setup.bitrix_call(
            webhook_base,
            "crm.category.update",
            {"entityTypeId": entity_type_id, "id": int(existing["id"]), "fields": fields},
        )
        refreshed = list_categories(webhook_base, entity_type_id=entity_type_id)
        return next(
            (item for item in refreshed if int(item.get("id") or 0) == int(existing["id"])),
            {**existing, **fields},
        )
    return existing


def category_plan(
    categories: list[dict[str, Any]],
    *,
    category_spec: dict[str, Any],
) -> dict[str, Any]:
    name = str(category_spec["name"])
    existing = next((item for item in categories if str(item.get("name") or "") == name), None)
    source = "name"
    if existing is None and category_spec.get("reuse_default"):
        existing = next((item for item in categories if str(item.get("isDefault") or "") == "Y"), None)
        source = "default" if existing else "missing"
    action = "add" if existing is None else "update"
    if existing is not None and str(existing.get("name") or "") == name and int(existing.get("sort") or 0) == int(category_spec["sort"]):
        action = "keep"
    return {
        "logical_key": category_spec["logical_key"],
        "name": name,
        "sort": category_spec["sort"],
        "action": action,
        "matched_by": source,
        "existing": None
        if existing is None
        else {
            "id": existing.get("id"),
            "name": existing.get("name"),
            "sort": existing.get("sort"),
            "isDefault": existing.get("isDefault"),
        },
    }


def stage_plan(
    stages: list[dict[str, Any]],
    *,
    entity_type_id: int,
    category_id: int,
    category_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    current_by_status = {str(item.get("STATUS_ID") or ""): item for item in stages}
    process_stages = sorted(
        [item for item in stages if not item.get("SEMANTICS")],
        key=lambda item: int(item.get("SORT") or 0),
    )
    success_stage = next((item for item in stages if item.get("SEMANTICS") == "S"), None)
    failure_stages = sorted(
        [item for item in stages if item.get("SEMANTICS") == "F"],
        key=lambda item: int(item.get("SORT") or 0),
    )
    process_iter = iter(process_stages)
    failure_iter = iter(failure_stages)
    expected_status_ids = {
        f"DT{entity_type_id}_{category_id}:{spec['code']}"
        for spec in desired_stage_specs(category_spec)
    }
    used_status_ids: set[str] = set()

    def next_unused_stage(iterator) -> dict[str, Any] | None:
        for stage in iterator:
            status_id = str(stage.get("STATUS_ID") or "")
            if status_id not in used_status_ids and status_id not in expected_status_ids:
                return stage
        return None

    rows: list[dict[str, Any]] = []
    for spec in desired_stage_specs(category_spec):
        status_id = f"DT{entity_type_id}_{category_id}:{spec['code']}"
        current = current_by_status.get(status_id)
        if current is None and spec.get("semantics"):
            if spec["semantics"] == "S":
                current = success_stage
            else:
                current = next_unused_stage(failure_iter)
        elif current is None:
            current = next_unused_stage(process_iter)
        if current is not None and str(current.get("STATUS_ID") or "") in used_status_ids:
            current = None
        action = "add" if current is None else "update"
        if current is not None:
            used_status_ids.add(str(current.get("STATUS_ID") or ""))
            same_name = str(current.get("NAME") or "") == str(spec["name"])
            same_sort = int(current.get("SORT") or 0) == int(spec["sort"])
            same_semantics = str(current.get("SEMANTICS") or "") == str(spec.get("semantics") or "")
            if same_name and same_sort and same_semantics:
                action = "keep"
        rows.append(
            {
                "logical_key": spec["logical_key"],
                "status_id": status_id,
                "name": spec["name"],
                "sort": spec["sort"],
                "semantics": spec.get("semantics"),
                "action": action,
                "existing": None
                if current is None
                else {
                    "ID": current.get("ID"),
                    "STATUS_ID": current.get("STATUS_ID"),
                    "NAME": current.get("NAME"),
                    "SORT": current.get("SORT"),
                    "SEMANTICS": current.get("SEMANTICS"),
                },
            }
        )
    return rows


def field_plan(webhook_base: str, *, process_type: dict[str, Any]) -> list[dict[str, Any]]:
    entity_id = bitrix_setup.smart_process_userfield_entity_id(int(process_type["id"]))
    fields = bitrix_setup.list_userfields(webhook_base, entity_id=entity_id)
    by_name = {str(item.get("fieldName") or ""): item for item in fields}
    by_xml_id = {str(item.get("xmlId") or ""): item for item in fields if item.get("xmlId")}
    rows: list[dict[str, Any]] = []
    for spec in CUSTOM_FIELD_SPECS:
        field_name = bitrix_setup._field_name_for_spec(entity_id, spec)
        xml_id = field_xml_id_for_spec(spec)
        current = by_xml_id.get(xml_id) or by_name.get(field_name)
        rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": spec["title"],
                "type": spec["type"],
                "xml_id": xml_id,
                "field_name": field_name,
                "action": "add" if current is None else "update",
                "existing": None
                if current is None
                else {
                    "id": current.get("id"),
                    "fieldName": current.get("fieldName"),
                    "xmlId": current.get("xmlId"),
                    "userTypeId": current.get("userTypeId"),
                },
            }
        )
    return rows


def desired_enum_options(spec: dict[str, Any]) -> list[dict[str, Any]]:
    options = []
    for index, option in enumerate(spec.get("enum") or [], start=1):
        options.append(
            {
                "value": str(option["value"]),
                "xmlId": str(option.get("xml_id") or option.get("xmlId") or ""),
                "def": "Y" if option.get("default") else "N",
                "sort": 100 + index * 100,
            }
        )
    return options


def enum_values(field: dict[str, Any]) -> list[str]:
    return [
        str(item.get("value") or item.get("VALUE") or "").strip()
        for item in field.get("enum") or []
        if str(item.get("value") or item.get("VALUE") or "").strip()
    ]


def enum_options_for_update(spec: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    current_by_value = {
        str(item.get("value") or item.get("VALUE") or "").strip(): item
        for item in current.get("enum") or []
    }
    rows: list[dict[str, Any]] = []
    for option in desired_enum_options(spec):
        existing = current_by_value.get(option["value"])
        if existing:
            enum_id = existing.get("id") or existing.get("ID")
            if enum_id:
                option["id"] = str(enum_id)
            option["xmlId"] = str(existing.get("xmlId") or existing.get("XML_ID") or option["xmlId"])
        rows.append(option)
    return rows


def ensure_procurement_custom_fields(
    webhook_base: str,
    *,
    process_type: dict[str, Any],
) -> list[dict[str, Any]]:
    type_id = int(process_type["id"])
    entity_id = bitrix_setup.smart_process_userfield_entity_id(type_id)
    existing_fields = bitrix_setup.list_userfields(webhook_base, entity_id=entity_id)
    existing_by_name = {str(item.get("fieldName") or ""): item for item in existing_fields}
    existing_by_xml_id = {
        str(item.get("xmlId") or ""): item for item in existing_fields if item.get("xmlId")
    }
    rows: list[dict[str, Any]] = []

    for index, spec in enumerate(CUSTOM_FIELD_SPECS, start=1):
        title = str(spec["title"])
        field_name = bitrix_setup._field_name_for_spec(entity_id, spec)
        xml_id = field_xml_id_for_spec(spec)
        user_type_id, settings = field_config_for_spec(spec)
        current = existing_by_xml_id.get(xml_id) or existing_by_name.get(field_name)

        if current is None:
            field = {
                "entityId": entity_id,
                "fieldName": field_name,
                "userTypeId": user_type_id,
                "xmlId": xml_id,
                "multiple": "N",
                "mandatory": "N",
                "showFilter": bitrix_setup._spec_show_filter(spec),
                "isSearchable": "Y" if bitrix_setup._spec_searchable(spec) else "N",
                "editInList": "Y" if bitrix_setup._spec_edit_in_list(spec) else "N",
                "sort": 100 + index * 10,
                "settings": settings,
            }
            if user_type_id == "enumeration":
                field["enum"] = desired_enum_options(spec)
            response = bitrix_setup.bitrix_call(
                webhook_base,
                "userfieldconfig.add",
                {"moduleId": "crm", "field": field},
            )
            current = (response.get("result") or {}).get("field") or {}
            action = "created"
        else:
            action = "updated"

        update_field = {
            "languageId": "ru",
            "xmlId": xml_id,
            "showFilter": bitrix_setup._spec_show_filter(spec),
            "isSearchable": "Y" if bitrix_setup._spec_searchable(spec) else "N",
            "editInList": "Y" if bitrix_setup._spec_edit_in_list(spec) else "N",
            "editFormLabel": {"ru": title},
            "listColumnLabel": {"ru": title},
            "listFilterLabel": {"ru": title},
        }
        if user_type_id != "enumeration":
            update_field["userTypeId"] = user_type_id

        bitrix_setup.bitrix_call(
            webhook_base,
            "userfieldconfig.update",
            {
                "moduleId": "crm",
                "id": current["id"],
                "field": update_field,
            },
        )

        refreshed_response = bitrix_setup.bitrix_call(
            webhook_base,
            "userfieldconfig.get",
            {"moduleId": "crm", "id": current["id"]},
        )
        current = (refreshed_response.get("result") or {}).get("field") or current

        if user_type_id == "enumeration":
            expected_values = [option["value"] for option in desired_enum_options(spec)]
            if enum_values(current) != expected_values:
                bitrix_setup.bitrix_call(
                    webhook_base,
                    "userfieldconfig.update",
                    {
                        "moduleId": "crm",
                        "id": current["id"],
                        "field": {
                            "userTypeId": "enumeration",
                            "enum": enum_options_for_update(spec, current),
                        },
                    },
                )
                refreshed_response = bitrix_setup.bitrix_call(
                    webhook_base,
                    "userfieldconfig.get",
                    {"moduleId": "crm", "id": current["id"]},
                )
                current = (refreshed_response.get("result") or {}).get("field") or current

        rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": title,
                "field_name": current.get("fieldName") or field_name,
                "field_id": current.get("id"),
                "xml_id": xml_id,
                "enum_map": bitrix_setup._enum_map_from_field(current),
                "action": action,
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

    return rows


def build_read_only_plan(
    webhook_base: str,
    *,
    title: str,
) -> dict[str, Any]:
    process_type = bitrix_setup.find_type_by_title(webhook_base, title)
    if process_type is None:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "process": {"title": title, "action": "add"},
            "categories": [category_plan([], category_spec=item) for item in CATEGORY_SPECS],
            "fields": [],
            "stages": {},
        }

    entity_type_id = int(process_type["entityTypeId"])
    categories = list_categories(webhook_base, entity_type_id=entity_type_id)
    category_plans = [category_plan(categories, category_spec=item) for item in CATEGORY_SPECS]
    stage_plans: dict[str, list[dict[str, Any]]] = {}
    for spec, plan in zip(CATEGORY_SPECS, category_plans, strict=True):
        existing = plan.get("existing")
        if not existing:
            stage_plans[spec["logical_key"]] = [
                {
                    "logical_key": item["logical_key"],
                    "name": item["name"],
                    "sort": item["sort"],
                    "semantics": item.get("semantics"),
                    "action": "add_after_category",
                }
                for item in desired_stage_specs(spec)
            ]
            continue
        category_id = int(existing["id"])
        stages = bitrix_setup.list_stages(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
        )
        stage_plans[spec["logical_key"]] = stage_plan(
            stages,
            entity_type_id=entity_type_id,
            category_id=category_id,
            category_spec=spec,
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "process": {
            "title": process_type.get("title"),
            "code": process_type.get("code"),
            "type_id": int(process_type["id"]),
            "entity_type_id": entity_type_id,
            "action": "update",
        },
        "categories": category_plans,
        "fields": field_plan(webhook_base, process_type=process_type),
        "stages": stage_plans,
    }


def build_mapping(
    *,
    process_type: dict[str, Any],
    categories: dict[str, dict[str, Any]],
    category_stages: dict[str, dict[str, dict[str, Any]]],
    custom_fields: list[dict[str, Any]],
    details_paths: dict[str, str],
) -> dict[str, Any]:
    field_map = dict(BUILTIN_FIELD_MAPPING)
    for row in custom_fields:
        field_map[row["logical_key"]] = row["field_name"]
    enum_map = {row["logical_key"]: row["enum_map"] for row in custom_fields if row.get("enum_map")}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "process": {
            "title": process_type.get("title"),
            "code": process_type.get("code"),
            "type_id": int(process_type["id"]),
            "entity_type_id": int(process_type["entityTypeId"]),
        },
        "category_map": {
            key: {
                "id": int(value["id"]),
                "name": value.get("name"),
                "sort": value.get("sort"),
            }
            for key, value in categories.items()
        },
        "stage_map": {
            key: {
                stage_key: str(stage.get("STATUS_ID") or stage.get("statusId") or "")
                for stage_key, stage in stages.items()
            }
            for key, stages in category_stages.items()
        },
        "field_map": field_map,
        "enum_map": enum_map,
        "details_configuration_paths": details_paths,
        "env": {
            "PROCUREMENT_BITRIX_ENTITY_TYPE_ID": int(process_type["entityTypeId"]),
            "PROCUREMENT_BITRIX_CATEGORY_MAP": json.dumps(
                {key: int(value["id"]) for key, value in categories.items()},
                ensure_ascii=False,
            ),
            "PROCUREMENT_BITRIX_STAGE_MAP": json.dumps(
                {
                    key: {
                        stage_key: str(stage.get("STATUS_ID") or stage.get("statusId") or "")
                        for stage_key, stage in stages.items()
                    }
                    for key, stages in category_stages.items()
                },
                ensure_ascii=False,
            ),
            "PROCUREMENT_BITRIX_FIELD_MAP": json.dumps(field_map, ensure_ascii=False),
            "PROCUREMENT_BITRIX_ENUM_MAP": json.dumps(enum_map, ensure_ascii=False),
        },
    }


def apply_setup(
    webhook_base: str,
    *,
    title: str,
    code: str,
    mapping_path: Path,
    details_config_path: Path,
    skip_details_config: bool,
) -> dict[str, Any]:
    configure_generic_setup()
    process_type = bitrix_setup.ensure_type(webhook_base, title=title, code=code)
    entity_type_id = int(process_type["entityTypeId"])
    categories: dict[str, dict[str, Any]] = {}
    category_stages: dict[str, dict[str, dict[str, Any]]] = {}
    details_paths: dict[str, str] = {}

    for category_spec in CATEGORY_SPECS:
        category = ensure_category_for_spec(
            webhook_base,
            entity_type_id=entity_type_id,
            category_spec=category_spec,
        )
        logical_key = str(category_spec["logical_key"])
        categories[logical_key] = category
        bitrix_setup.STAGE_SPECS = desired_stage_specs(category_spec)
        category_id = int(category["id"])
        category_stages[logical_key] = bitrix_setup.ensure_stages(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
        )

    custom_fields = ensure_procurement_custom_fields(webhook_base, process_type=process_type)
    mapping = build_mapping(
        process_type=process_type,
        categories=categories,
        category_stages=category_stages,
        custom_fields=custom_fields,
        details_paths=details_paths,
    )

    if not skip_details_config:
        for logical_key, category in categories.items():
            details_mapping = {
                "process": {
                    "entity_type_id": entity_type_id,
                    "category_id": int(category["id"]),
                },
                "fields": mapping["field_map"],
            }
            path = details_config_path.with_name(
                f"{details_config_path.stem}_{logical_key}{details_config_path.suffix}"
            )
            _, saved_path = bitrix_setup.ensure_common_details_configuration(
                webhook_base,
                mapping=details_mapping,
                path=path,
            )
            details_paths[logical_key] = str(saved_path)
        mapping["details_configuration_paths"] = details_paths

    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapping


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--title", default=DEFAULT_PROCESS_TITLE)
    parser.add_argument("--code", default=DEFAULT_PROCESS_CODE)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--details-config-path", type=Path, default=DEFAULT_DETAILS_CONFIG_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--skip-details-config", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Update Bitrix. Default is read-only plan.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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

    configure_generic_setup()
    if args.apply:
        mapping = apply_setup(
            webhook_base,
            title=args.title,
            code=args.code,
            mapping_path=args.mapping_path,
            details_config_path=args.details_config_path,
            skip_details_config=args.skip_details_config,
        )
        result = {"mode": "apply", "mapping_path": str(args.mapping_path), "mapping": mapping}
    else:
        plan = build_read_only_plan(webhook_base, title=args.title)
        result = {"mode": "dry-run", "plan": plan}

    args.result_path.parent.mkdir(parents=True, exist_ok=True)
    args.result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
