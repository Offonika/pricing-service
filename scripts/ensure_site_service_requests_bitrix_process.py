#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.services.expertise_bitrix import BitrixRestClient

REQUEST_TYPE_FIELD_NAME = "UF_CRM_36_CUSTOMERREQUESTCHOICE"
REQUEST_TYPE_ENUM_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "warranty": ("expertise", ("Нужна экспертиза",)),
    "refund_money": ("refund_money", ("Вернуть деньги", "Возврат денег")),
    "replacement": ("replacement", ("Замена товара", "Замена")),
    "delivery_return": ("logistics_return", ("Доставка / возврат",)),
    "consultation": ("clarify", ("Разобраться", "Консультация")),
    "other": ("other", ("Другое", "Прочее")),
}

FIELD_SPECS: tuple[dict[str, Any], ...] = (
    {"key": "site_ticket_id", "title": "ID тикета сайта", "type": "string"},
    {"key": "site_ticket_url", "title": "Открыть тикет сайта", "type": "url"},
    {"key": "site_history", "title": "История переписки сайта", "type": "string"},
    {
        "key": "site_sync_status",
        "title": "Синхронизация с сайтом",
        "type": "enumeration",
        "enum": (
            ("pending", "Ожидает синхронизации"),
            ("synced", "Синхронизировано"),
            ("client_match_required", "Требуется связать клиента"),
            ("order_match_required", "Требуется связать заказ"),
            ("order_not_found", "Заказ не найден"),
            ("file_sync_error", "Ошибка файла"),
            ("assignment_waiting", "Ожидает назначения"),
            ("error", "Техническая ошибка"),
        ),
    },
    {"key": "site_reply_text", "title": "Ответ клиенту", "type": "string"},
    {
        "key": "site_reply_action",
        "title": "Действие с ответом",
        "type": "enumeration",
        "enum": (("draft", "Черновик"), ("send", "Отправить клиенту")),
    },
    {
        "key": "site_reply_status",
        "title": "Статус ответа",
        "type": "enumeration",
        "enum": (
            ("none", "Нет ответа"),
            ("pending", "Ожидает отправки"),
            ("sent", "Отправлено"),
            ("error", "Ошибка отправки"),
        ),
    },
    {"key": "site_last_sync_at", "title": "Последняя синхронизация", "type": "datetime"},
    {"key": "first_response_due_at", "title": "Срок первого ответа", "type": "datetime"},
    {"key": "first_response_at", "title": "Первый ответ доставлен", "type": "datetime"},
    {"key": "site_sync_error", "title": "Техническая ошибка", "type": "string"},
)

FORM_SECTIONS = (
    (
        "site_request",
        "Обращение сайта",
        (
            "TITLE",
            "site_ticket_id",
            "site_ticket_url",
            "site_sync_status",
            "first_response_due_at",
            "first_response_at",
        ),
    ),
    (
        "customer_context",
        "Клиент и обращение",
        (
            "UF_CRM_36_CUSTOMERCONTACT",
            "UF_CRM_36_CRMCONTACT",
            "UF_CRM_36_CRMCOMPANY",
            "UF_CRM_36_CRMDEAL",
            "UF_CRM_36_ORDERREFS",
            "UF_CRM_36_PROBLEMDESCRIPTION",
            "UF_CRM_36_CUSTOMERREQUESTCHOICE",
            "UF_CRM_36_CLIENTFILES",
            "site_history",
        ),
    ),
    (
        "site_reply",
        "Ответ клиенту",
        ("site_reply_text", "site_reply_action", "site_reply_status", "site_last_sync_at"),
    ),
    (
        "technical",
        "Технические данные",
        (
            "UF_CRM_36_BACKENDCASEID",
            "UF_CRM_36_IDEMPOTENCYKEY",
            "site_sync_error",
        ),
    ),
)

REQUIRED_STAGE_CODES = {
    "new": "NEW",
    "success": "SUCCESS",
    "failure": "FAIL",
}


class BitrixJsonApi(Protocol):
    def call_json(self, method: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EnsurePlan:
    entity_type_id: int
    type_id: int
    entity_id: str
    category_id: int
    missing_fields: tuple[str, ...]
    type_mismatches: tuple[str, ...]
    missing_stages: tuple[str, ...]
    missing_enum_mappings: tuple[str, ...]
    field_map: dict[str, str]
    enum_map: dict[str, str]
    stage_map: dict[str, str]
    form: list[dict[str, Any]]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or ensure task #3223 fields and form in smart process 1134."
    )
    parser.add_argument("--apply", action="store_true", help="Create fields and update form.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local/site-service-requests/bitrix-mapping.json"),
    )
    return parser.parse_args(argv)


def build_plan(
    *,
    entity_type_id: int,
    type_id: int,
    category_id: int,
    fields: list[dict[str, Any]],
    stages: list[dict[str, Any]],
    existing_form: list[dict[str, Any]] | None = None,
) -> EnsurePlan:
    entity_id = f"CRM_{type_id}"
    by_xml_id = {str(field.get("xmlId") or ""): field for field in fields}
    field_map: dict[str, str] = {}
    enum_map: dict[str, str] = {}
    missing: list[str] = []
    mismatches: list[str] = []
    for spec in FIELD_SPECS:
        xml_id = _xml_id(spec["key"])
        current = by_xml_id.get(xml_id)
        if current is None:
            missing.append(spec["key"])
            continue
        actual_type = str(current.get("userTypeId") or "")
        if actual_type != spec["type"]:
            mismatches.append(spec["key"])
        field_name = str(current.get("fieldName") or "")
        if field_name:
            field_map[spec["key"]] = field_name
        enum_map.update(_enum_mapping(spec, current))
    request_type_field = next(
        (
            field
            for field in fields
            if _normalized_field_name(field.get("fieldName"))
            == _normalized_field_name(REQUEST_TYPE_FIELD_NAME)
        ),
        None,
    )
    if request_type_field is not None:
        field_name = str(request_type_field.get("fieldName") or "").strip()
        if field_name:
            field_map["request_type"] = field_name
        enum_map.update(_request_type_enum_mapping(request_type_field))
    stage_map = _stage_mapping(stages)
    form = merge_form(existing_form or [], build_form(field_map))
    missing_enum_mappings = tuple(
        f"request_type_{key}"
        for key in REQUEST_TYPE_ENUM_TARGETS
        if not enum_map.get(f"request_type_{key}")
    )
    return EnsurePlan(
        entity_type_id=entity_type_id,
        type_id=type_id,
        entity_id=entity_id,
        category_id=category_id,
        missing_fields=tuple(missing),
        type_mismatches=tuple(mismatches),
        missing_stages=tuple(key for key in REQUIRED_STAGE_CODES if key not in stage_map),
        missing_enum_mappings=missing_enum_mappings,
        field_map=field_map,
        enum_map=enum_map,
        stage_map=stage_map,
        form=form,
    )


def build_form(field_map: dict[str, str]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for name, title, keys in FORM_SECTIONS:
        elements = []
        for key in keys:
            field_name = field_map.get(key, key if key.startswith(("UF_", "TITLE")) else "")
            if field_name:
                elements.append({"name": field_name, "optionFlags": 1})
        if elements:
            sections.append({"name": name, "title": title, "type": "section", "elements": elements})
    return sections


def merge_form(
    existing: list[dict[str, Any]],
    desired: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = json.loads(json.dumps(existing, ensure_ascii=False))
    existing_names = {
        str(element.get("name") or "")
        for section in merged
        for element in section.get("elements") or []
        if isinstance(element, dict)
    }
    sections_by_name = {
        str(section.get("name") or ""): section for section in merged if isinstance(section, dict)
    }
    for desired_section in desired:
        section_name = str(desired_section.get("name") or "")
        target = sections_by_name.get(section_name)
        if target is None:
            target = {
                **desired_section,
                "elements": [],
            }
            merged.append(target)
            sections_by_name[section_name] = target
        target_elements = target.setdefault("elements", [])
        target_names = {
            str(element.get("name") or "")
            for element in target_elements
            if isinstance(element, dict)
        }
        for element in desired_section.get("elements") or []:
            element_name = str(element.get("name") or "")
            if not element_name or element_name in existing_names or element_name in target_names:
                continue
            target_elements.append(element)
            target_names.add(element_name)
            existing_names.add(element_name)
    return merged


def ensure(
    api: BitrixJsonApi,
    *,
    settings: Settings,
    apply: bool,
) -> EnsurePlan:
    type_response = api.call_json(
        "crm.type.get",
        {"entityTypeId": settings.site_service_requests_bitrix_entity_type_id},
    )
    process_type = (type_response.get("result") or {}).get("type") or {}
    type_id = int(process_type.get("id") or 0)
    if type_id <= 0:
        raise RuntimeError("smart_process_type_not_found")
    entity_id = f"CRM_{type_id}"
    fields = _list_fields(api, entity_id=entity_id)
    stages = _list_stages(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
    )
    configuration_payload = {
        "entityTypeId": settings.site_service_requests_bitrix_entity_type_id,
        "scope": "C",
        "extras": {"categoryId": settings.site_service_requests_bitrix_working_category_id},
    }
    configuration_response = api.call_json(
        "crm.item.details.configuration.get",
        configuration_payload,
    )
    existing_form = _configuration_result(configuration_response)
    plan = build_plan(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        type_id=type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
        fields=fields,
        stages=stages,
        existing_form=existing_form,
    )
    if not apply:
        return plan
    if not settings.site_service_requests_bitrix_writes_enabled:
        raise RuntimeError("site_service_request_bitrix_writes_disabled")
    _require_recognized_form(configuration_response)
    if plan.type_mismatches:
        raise RuntimeError("site_service_request_field_type_mismatch")
    if plan.missing_stages:
        raise RuntimeError("site_service_request_required_stage_missing")
    if plan.missing_enum_mappings:
        raise RuntimeError("site_service_request_request_type_enum_mapping_incomplete")

    for spec in FIELD_SPECS:
        if spec["key"] not in plan.missing_fields:
            continue
        api.call_json(
            "userfieldconfig.add",
            {
                "moduleId": "crm",
                "field": _field_payload(entity_id=entity_id, spec=spec),
            },
        )
    refreshed = build_plan(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        type_id=type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
        fields=_list_fields(api, entity_id=entity_id),
        stages=_list_stages(
            api,
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            category_id=settings.site_service_requests_bitrix_working_category_id,
        ),
        existing_form=existing_form,
    )
    if (
        refreshed.missing_fields
        or refreshed.type_mismatches
        or refreshed.missing_stages
        or refreshed.missing_enum_mappings
    ):
        raise RuntimeError("site_service_request_fields_readback_failed")
    # Re-read immediately before the write so a concurrent administrator change
    # is merged instead of being replaced by the stale initial snapshot.
    latest_form = _require_recognized_form(
        api.call_json("crm.item.details.configuration.get", configuration_payload)
    )
    refreshed = replace(
        refreshed,
        form=merge_form(latest_form, build_form(refreshed.field_map)),
    )
    api.call_json(
        "crm.item.details.configuration.set",
        {
            "entityTypeId": refreshed.entity_type_id,
            "scope": "C",
            "extras": {"categoryId": refreshed.category_id},
            "data": refreshed.form,
        },
    )
    form_readback = api.call_json(
        "crm.item.details.configuration.get",
        {
            "entityTypeId": refreshed.entity_type_id,
            "scope": "C",
            "extras": {"categoryId": refreshed.category_id},
        },
    )
    if not _form_contains(refreshed.form, _configuration_result(form_readback)):
        raise RuntimeError("site_service_request_form_readback_failed")
    return refreshed


def plan_payload(plan: EnsurePlan, *, applied: bool) -> dict[str, Any]:
    return {
        "generatedAt": datetime.now(UTC).isoformat(),
        "applied": applied,
        "entityTypeId": plan.entity_type_id,
        "typeId": plan.type_id,
        "categoryId": plan.category_id,
        "missingFields": list(plan.missing_fields),
        "typeMismatches": list(plan.type_mismatches),
        "missingStages": list(plan.missing_stages),
        "missingEnumMappings": list(plan.missing_enum_mappings),
        "fieldMap": plan.field_map,
        "enumMap": plan.enum_map,
        "stageMap": plan.stage_map,
        "form": plan.form,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    webhook = settings.site_service_requests_bitrix_webhook_url
    if not webhook:
        raise RuntimeError("SITE_SERVICE_REQUESTS_BITRIX_WEBHOOK_URL is not configured")
    plan = ensure(
        BitrixRestClient(webhook),
        settings=settings,
        apply=args.apply,
    )
    payload = plan_payload(plan, applied=args.apply)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _list_fields(api: BitrixJsonApi, *, entity_id: str) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"moduleId": "crm", "filter": {"entityId": entity_id}}
    fields: list[dict[str, Any]] = []
    seen_starts: set[int] = set()
    while True:
        response = api.call_json("userfieldconfig.list", payload)
        result = response.get("result") or {}
        page = result.get("fields") if isinstance(result, dict) else None
        if isinstance(page, list):
            fields.extend(item for item in page if isinstance(item, dict))
        next_start = response.get("next")
        if next_start is None and isinstance(result, dict):
            next_start = result.get("next")
        if next_start is None:
            return fields
        start = int(next_start)
        if start in seen_starts:
            raise RuntimeError("site_service_request_fields_pagination_loop")
        seen_starts.add(start)
        payload = {**payload, "start": start}


def _list_stages(
    api: BitrixJsonApi,
    *,
    entity_type_id: int,
    category_id: int,
) -> list[dict[str, Any]]:
    response = api.call_json(
        "crm.status.list",
        {"filter": {"ENTITY_ID": f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"}},
    )
    result = response.get("result") or []
    if isinstance(result, dict):
        result = result.get("statuses") or result.get("items") or []
    return [item for item in result if isinstance(item, dict)]


def _stage_mapping(stages: list[dict[str, Any]]) -> dict[str, str]:
    by_code: dict[str, str] = {}
    success_stage_id = ""
    failure_stage_id = ""
    for stage in stages:
        stage_id = str(stage.get("STATUS_ID") or stage.get("statusId") or "").strip()
        if not stage_id:
            continue
        code = stage_id.rsplit(":", 1)[-1].upper()
        by_code.setdefault(code, stage_id)
        if str(stage.get("SEMANTICS") or stage.get("semantics") or "").upper() == "S":
            success_stage_id = stage_id
        if str(stage.get("SEMANTICS") or stage.get("semantics") or "").upper() == "F":
            failure_stage_id = stage_id
    mapping = {
        logical_key: by_code[code]
        for logical_key, code in REQUIRED_STAGE_CODES.items()
        if code in by_code
    }
    if "success" not in mapping and success_stage_id:
        mapping["success"] = success_stage_id
    if "failure" not in mapping and failure_stage_id:
        mapping["failure"] = failure_stage_id
    return mapping


def _configuration_result(response: dict[str, Any]) -> list[dict[str, Any]]:
    result: Any = response.get("result")
    if isinstance(result, dict):
        result = result.get("data") or result.get("configuration") or []
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def _require_recognized_form(response: dict[str, Any]) -> list[dict[str, Any]]:
    form = _configuration_result(response)
    if not form:
        raise RuntimeError("site_service_request_form_readback_unrecognized")
    for section in form:
        if (
            not str(section.get("name") or "").strip()
            or section.get("type") != "section"
            or not isinstance(section.get("elements"), list)
        ):
            raise RuntimeError("site_service_request_form_readback_unrecognized")
    return form


def _form_contains(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> bool:
    actual_sections = {str(section.get("name") or ""): section for section in actual}
    for expected_section in expected:
        actual_section = actual_sections.get(str(expected_section.get("name") or ""))
        if actual_section is None:
            return False
        expected_names = {
            str(element.get("name") or "") for element in expected_section.get("elements") or []
        }
        actual_names = {
            str(element.get("name") or "") for element in actual_section.get("elements") or []
        }
        if not expected_names.issubset(actual_names):
            return False
    return True


def _field_payload(*, entity_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entityId": entity_id,
        "fieldName": _field_name(entity_id, spec["key"]),
        "userTypeId": spec["type"],
        "xmlId": _xml_id(spec["key"]),
        "multiple": "N",
        "mandatory": "N",
        "showFilter": "E",
        "isSearchable": "Y",
        "editInList": "Y",
        "editFormLabel": {"ru": spec["title"]},
        "listColumnLabel": {"ru": spec["title"]},
        "listFilterLabel": {"ru": spec["title"]},
    }
    if spec["type"] == "string" and spec["key"] in {
        "site_history",
        "site_reply_text",
    }:
        payload["settings"] = {"ROWS": 10}
    if spec["type"] == "enumeration":
        payload["enum"] = [
            {
                "value": title,
                "xmlId": f"MM_SITE_{spec['key'].upper()}_{key.upper()}",
                "sort": 100 + index * 100,
            }
            for index, (key, title) in enumerate(spec["enum"])
        ]
    return payload


def _enum_mapping(spec: dict[str, Any], field: dict[str, Any]) -> dict[str, str]:
    if spec["type"] != "enumeration":
        return {}
    by_xml_id = {
        str(item.get("xmlId") or item.get("XML_ID") or ""): str(
            item.get("id") or item.get("ID") or ""
        )
        for item in field.get("enum") or []
    }
    mapping: dict[str, str] = {}
    for key, _title in spec["enum"]:
        xml_id = f"MM_SITE_{spec['key'].upper()}_{key.upper()}"
        enum_id = by_xml_id.get(xml_id)
        if enum_id:
            prefix = spec["key"].removeprefix("site_")
            mapping[f"{prefix}_{key}"] = enum_id
    return mapping


def _request_type_enum_mapping(field: dict[str, Any]) -> dict[str, str]:
    enum_rows = [row for row in field.get("enum") or [] if isinstance(row, dict)]
    mapping: dict[str, str] = {}
    for request_type, (logical_xml_id, labels) in REQUEST_TYPE_ENUM_TARGETS.items():
        for row in enum_rows:
            enum_id = str(row.get("id") or row.get("ID") or "").strip()
            xml_id = str(row.get("xmlId") or row.get("XML_ID") or "").strip().lower()
            value = str(row.get("value") or row.get("VALUE") or "").strip().casefold()
            if enum_id and (
                xml_id == logical_xml_id
                or xml_id.endswith(f"_{logical_xml_id}")
                or value in {label.casefold() for label in labels}
            ):
                mapping[f"request_type_{request_type}"] = enum_id
                break
    return mapping


def _normalized_field_name(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _field_name(entity_id: str, key: str) -> str:
    compact_key = "".join(character for character in key.upper() if character.isalnum())
    return f"UF_{entity_id}_{compact_key}"


def _xml_id(key: str) -> str:
    return f"MM_SITE_SERVICE_{key.upper()}"


if __name__ == "__main__":
    raise SystemExit(main())
