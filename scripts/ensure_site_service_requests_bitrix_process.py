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
    {"key": "mail_activity_id", "title": "ID email-активности", "type": "string"},
    {"key": "mail_activity_url", "title": "Открыть письмо в CRM", "type": "url"},
    {"key": "mail_thread_key", "title": "Технический ключ email-цепочки", "type": "string"},
)

FORM_SECTIONS = (
    (
        "site_request",
        "Обращение сайта",
        (
            "TITLE",
            "site_ticket_id",
            "site_ticket_url",
            "mail_activity_id",
            "mail_activity_url",
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
            "mail_thread_key",
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
    expected_xml_id_by_field_name = {
        _normalized_field_name(_field_name(entity_id, spec["key"])): _xml_id(spec["key"])
        for spec in FIELD_SPECS
    }
    by_xml_id: dict[str, dict[str, Any]] = {}
    for field in fields:
        normalized_field_name = _normalized_field_name(field.get("fieldName"))
        expected_xml_id = expected_xml_id_by_field_name.get(normalized_field_name)
        raw_xml_id = _strict_aliased_string(
            field,
            "xmlId",
            "XML_ID",
            error_code="site_service_request_field_readback_unrecognized",
        )
        if expected_xml_id is not None and raw_xml_id != expected_xml_id:
            raise RuntimeError("site_service_request_field_readback_unrecognized")
        xml_id = (raw_xml_id or "").strip()
        if not xml_id:
            continue
        if xml_id in by_xml_id:
            raise RuntimeError("site_service_request_field_readback_ambiguous")
        by_xml_id[xml_id] = field
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
        raw_type = current.get("userTypeId")
        raw_field_name = current.get("fieldName")
        if not isinstance(raw_type, str) or not isinstance(raw_field_name, str):
            raise RuntimeError("site_service_request_field_readback_unrecognized")
        actual_type = raw_type.strip()
        if actual_type != spec["type"]:
            mismatches.append(spec["key"])
        field_name = raw_field_name.strip()
        if not field_name:
            raise RuntimeError("site_service_request_field_readback_unrecognized")
        field_map[spec["key"]] = field_name
        if actual_type == spec["type"]:
            enum_map.update(_enum_mapping(spec, current))
    request_type_fields = [
        field
        for field in fields
        if _normalized_field_name(field.get("fieldName"))
        == _normalized_field_name(REQUEST_TYPE_FIELD_NAME)
    ]
    if len(request_type_fields) > 1:
        raise RuntimeError("site_service_request_request_type_field_ambiguous")
    request_type_field = request_type_fields[0] if request_type_fields else None
    if request_type_field is not None:
        raw_field_name = request_type_field.get("fieldName")
        raw_type = request_type_field.get("userTypeId")
        if not isinstance(raw_field_name, str) or not isinstance(raw_type, str):
            raise RuntimeError("site_service_request_field_readback_unrecognized")
        field_name = raw_field_name.strip()
        if not field_name:
            raise RuntimeError("site_service_request_field_readback_unrecognized")
        if raw_type.strip() != "enumeration":
            mismatches.append("request_type")
        else:
            enum_map.update(_request_type_enum_mapping(request_type_field))
        field_map["request_type"] = field_name
    stage_map = _stage_mapping(
        stages,
        entity_type_id=entity_type_id,
        category_id=category_id,
    )
    form = merge_form(existing_form or [], build_form(field_map))
    required_enum_mappings = [
        f"{spec['key'].removeprefix('site_')}_{key}"
        for spec in FIELD_SPECS
        if spec["type"] == "enumeration" and spec["key"] not in missing
        for key, _title in spec["enum"]
    ]
    required_enum_mappings.extend(f"request_type_{key}" for key in REQUEST_TYPE_ENUM_TARGETS)
    missing_enum_mappings = tuple(
        mapping_key for mapping_key in required_enum_mappings if not enum_map.get(mapping_key)
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
        if isinstance(section, dict)
        for element in (
            section.get("elements") if isinstance(section.get("elements"), list) else []
        )
        if isinstance(element, dict)
    }
    sections_by_name = {
        str(section.get("name") or ""): section
        for section in merged
        if isinstance(section, dict)
        and section.get("type") == "section"
        and isinstance(section.get("elements"), list)
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
    type_id = _require_process_type_id(
        type_response,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
    )
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
    existing_form = _require_recognized_form(configuration_response)
    if apply and not settings.site_service_requests_bitrix_writes_enabled:
        raise RuntimeError("site_service_request_bitrix_writes_disabled")
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
    if plan.type_mismatches:
        raise RuntimeError("site_service_request_field_type_mismatch")
    if plan.missing_stages:
        raise RuntimeError("site_service_request_required_stage_missing")
    if any(
        not mapping_key.startswith("request_type_") for mapping_key in plan.missing_enum_mappings
    ):
        raise RuntimeError("site_service_request_enum_mapping_incomplete")
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
    if not _form_contains(refreshed.form, _require_recognized_form(form_readback)):
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
    seen_starts: set[int] = {0}
    page_count = 0
    while True:
        page_count += 1
        response = api.call_json("userfieldconfig.list", payload)
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("fields"), list):
            raise RuntimeError("site_service_request_fields_readback_unrecognized")
        page = result["fields"]
        for item in page:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("fieldName"), str)
                or not item["fieldName"].strip()
                or not isinstance(item.get("userTypeId"), str)
                or not item["userTypeId"].strip()
            ):
                raise RuntimeError("site_service_request_fields_readback_unrecognized")
            _strict_aliased_positive_int(
                item,
                "id",
                "ID",
                error_code="site_service_request_fields_readback_unrecognized",
            )
        fields.extend(page)
        start = _resolve_pagination_offset(
            top_next=response.get("next"),
            nested_next=result.get("next"),
            error_code="site_service_request_fields_pagination_invalid",
        )
        if start is None:
            return fields
        if page_count >= 100:
            raise RuntimeError("site_service_request_fields_pagination_invalid")
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
    expected_prefix = f"DT{entity_type_id}_{category_id}:"
    payload: dict[str, Any] = {
        "filter": {"ENTITY_ID": f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"}
    }
    stages: list[dict[str, Any]] = []
    seen_starts: set[int] = {0}
    page_count = 0
    while True:
        page_count += 1
        response = api.call_json("crm.status.list", payload)
        result = response.get("result")
        if isinstance(result, list):
            page = result
            nested_next = None
        elif isinstance(result, dict):
            page_keys = [key for key in ("statuses", "items") if key in result]
            if len(page_keys) != 1 or not isinstance(result[page_keys[0]], list):
                raise RuntimeError("site_service_request_stages_readback_unrecognized")
            page = result[page_keys[0]]
            nested_next = result.get("next")
        else:
            raise RuntimeError("site_service_request_stages_readback_unrecognized")
        for item in page:
            if not isinstance(item, dict):
                raise RuntimeError("site_service_request_stages_readback_unrecognized")
            _strict_stage_id(item, expected_prefix=expected_prefix)
        stages.extend(page)
        start = _resolve_pagination_offset(
            top_next=response.get("next"),
            nested_next=nested_next,
            error_code="site_service_request_stages_pagination_invalid",
        )
        if start is None:
            return stages
        if page_count >= 100:
            raise RuntimeError("site_service_request_stages_pagination_invalid")
        if start in seen_starts:
            raise RuntimeError("site_service_request_stages_pagination_loop")
        seen_starts.add(start)
        payload = {**payload, "start": start}


def _stage_mapping(
    stages: list[dict[str, Any]],
    *,
    entity_type_id: int,
    category_id: int,
) -> dict[str, str]:
    expected_prefix = f"DT{entity_type_id}_{category_id}:"
    by_code: dict[str, str] = {}
    seen_stage_ids: set[str] = set()
    success_stage_ids: set[str] = set()
    failure_stage_ids: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict):
            raise RuntimeError("site_service_request_stages_readback_unrecognized")
        stage_id = _strict_stage_id(stage, expected_prefix=expected_prefix)
        if stage_id in seen_stage_ids:
            raise RuntimeError("site_service_request_stage_mapping_ambiguous")
        seen_stage_ids.add(stage_id)
        code = stage_id.rsplit(":", 1)[-1].upper()
        existing_stage_id = by_code.get(code)
        if existing_stage_id is not None and existing_stage_id != stage_id:
            raise RuntimeError("site_service_request_stage_mapping_ambiguous")
        by_code[code] = stage_id
        semantics = _strict_stage_semantics(stage)
        if semantics == "S":
            success_stage_ids.add(stage_id)
        if semantics == "F":
            failure_stage_ids.add(stage_id)
    mapping = {
        logical_key: by_code[code]
        for logical_key, code in REQUIRED_STAGE_CODES.items()
        if code in by_code
    }
    if "success" not in mapping:
        if len(success_stage_ids) > 1:
            raise RuntimeError("site_service_request_stage_mapping_ambiguous")
        if success_stage_ids:
            mapping["success"] = next(iter(success_stage_ids))
    if "failure" not in mapping:
        if len(failure_stage_ids) > 1:
            raise RuntimeError("site_service_request_stage_mapping_ambiguous")
        if failure_stage_ids:
            mapping["failure"] = next(iter(failure_stage_ids))
    return mapping


def _require_recognized_form(response: dict[str, Any]) -> list[dict[str, Any]]:
    form = _raw_configuration_result(response)
    if not isinstance(form, list) or not form:
        raise RuntimeError("site_service_request_form_readback_unrecognized")
    section_names: set[str] = set()
    element_names: set[str] = set()
    for section in form:
        section_name = section.get("name") if isinstance(section, dict) else None
        if (
            not isinstance(section, dict)
            or not isinstance(section_name, str)
            or not section_name.strip()
            or section.get("type") != "section"
            or not isinstance(section.get("elements"), list)
            or any(
                not isinstance(element, dict)
                or not isinstance(element.get("name"), str)
                or not element["name"].strip()
                for element in section.get("elements") or []
            )
        ):
            raise RuntimeError("site_service_request_form_readback_unrecognized")
        if section_name in section_names:
            raise RuntimeError("site_service_request_form_readback_ambiguous")
        section_names.add(section_name)
        for element in section["elements"]:
            element_name = element["name"].strip()
            if element_name in element_names:
                raise RuntimeError("site_service_request_form_readback_ambiguous")
            element_names.add(element_name)
    return form


def _raw_configuration_result(response: dict[str, Any]) -> Any:
    result: Any = response.get("result")
    if isinstance(result, dict):
        if "data" in result:
            return result["data"]
        if "configuration" in result:
            return result["configuration"]
        return None
    return result


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
    enum_rows = _require_enum_rows(field)
    mapping: dict[str, str] = {}
    for key, _title in spec["enum"]:
        xml_id = f"MM_SITE_{spec['key'].upper()}_{key.upper()}"
        enum_ids = {
            _strict_enum_row_id(item)
            for item in enum_rows
            if (_strict_optional_enum_string(item, "xmlId", "XML_ID") or "").strip() == xml_id
        }
        if len(enum_ids) > 1:
            raise RuntimeError("site_service_request_enum_mapping_ambiguous")
        if enum_ids:
            prefix = spec["key"].removeprefix("site_")
            mapping[f"{prefix}_{key}"] = next(iter(enum_ids))
    return mapping


def _request_type_enum_mapping(field: dict[str, Any]) -> dict[str, str]:
    enum_rows = _require_enum_rows(field)
    mapping: dict[str, str] = {}
    for request_type, (logical_xml_id, labels) in REQUEST_TYPE_ENUM_TARGETS.items():
        label_values = {label.casefold() for label in labels}
        exact_rows = [
            row
            for row in enum_rows
            if (_strict_optional_enum_string(row, "xmlId", "XML_ID") or "").strip().lower()
            == logical_xml_id
        ]
        suffix_rows = [
            row
            for row in enum_rows
            if (_strict_optional_enum_string(row, "xmlId", "XML_ID") or "")
            .strip()
            .lower()
            .endswith(f"_{logical_xml_id}")
        ]
        label_rows = [
            row
            for row in enum_rows
            if (_strict_optional_enum_string(row, "value", "VALUE") or "").strip().casefold()
            in label_values
        ]
        candidates = exact_rows or suffix_rows or label_rows
        enum_ids = {_strict_enum_row_id(row) for row in candidates}
        if len(enum_ids) > 1:
            raise RuntimeError("site_service_request_request_type_enum_ambiguous")
        if enum_ids:
            mapping[f"request_type_{request_type}"] = next(iter(enum_ids))
    return mapping


def _require_process_type_id(response: dict[str, Any], *, entity_type_id: int) -> int:
    if not isinstance(response, dict):
        raise RuntimeError("site_service_request_type_readback_unrecognized")
    result = response.get("result")
    process_type = result.get("type") if isinstance(result, dict) else None
    if not isinstance(process_type, dict):
        raise RuntimeError("site_service_request_type_readback_unrecognized")
    type_id = _strict_pagination_offset(
        process_type.get("id"),
        error_code="site_service_request_type_readback_unrecognized",
    )
    returned_entity_type_id = _strict_pagination_offset(
        process_type.get("entityTypeId"),
        error_code="site_service_request_type_readback_unrecognized",
    )
    if type_id <= 0 or returned_entity_type_id != entity_type_id:
        raise RuntimeError("site_service_request_type_readback_unrecognized")
    return type_id


def _require_enum_rows(field: dict[str, Any]) -> list[dict[str, Any]]:
    rows = field.get("enum")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("site_service_request_enum_readback_unrecognized")
    enum_ids = [_strict_enum_row_id(row) for row in rows]
    for row in rows:
        _strict_optional_enum_string(row, "xmlId", "XML_ID")
        if _strict_optional_enum_string(row, "value", "VALUE") is None:
            raise RuntimeError("site_service_request_enum_readback_unrecognized")
    if len(enum_ids) != len(set(enum_ids)):
        raise RuntimeError("site_service_request_enum_readback_ambiguous")
    return rows


def _strict_enum_row_id(row: dict[str, Any]) -> str:
    values = [row[field_name] for field_name in ("id", "ID") if field_name in row]
    if not values:
        raise RuntimeError("site_service_request_enum_readback_unrecognized")
    parsed_values = [_positive_int(value) for value in values]
    if any(value is None for value in parsed_values):
        raise RuntimeError("site_service_request_enum_readback_unrecognized")
    first_value = parsed_values[0]
    if any(value != first_value for value in parsed_values[1:]):
        raise RuntimeError("site_service_request_enum_readback_ambiguous")
    assert first_value is not None
    return str(first_value)


def _strict_optional_enum_string(
    row: dict[str, Any],
    *field_names: str,
) -> str | None:
    values = [row[field_name] for field_name in field_names if field_name in row]
    if not values:
        return None
    if any(not isinstance(value, str) for value in values):
        raise RuntimeError("site_service_request_enum_readback_unrecognized")
    first_value = values[0]
    if any(value != first_value for value in values[1:]):
        raise RuntimeError("site_service_request_enum_readback_ambiguous")
    return first_value


def _strict_aliased_string(
    item: dict[str, Any],
    *field_names: str,
    error_code: str,
) -> str | None:
    values = [item[field_name] for field_name in field_names if field_name in item]
    if not values:
        return None
    if any(not isinstance(value, str) for value in values):
        raise RuntimeError(error_code)
    first_value = values[0]
    if any(value != first_value for value in values[1:]):
        raise RuntimeError(error_code)
    return first_value


def _strict_stage_id(item: dict[str, Any], *, expected_prefix: str) -> str:
    stage_id = _strict_aliased_string(
        item,
        "STATUS_ID",
        "statusId",
        error_code="site_service_request_stages_readback_unrecognized",
    )
    if (
        stage_id is None
        or not stage_id
        or stage_id.strip() != stage_id
        or not stage_id.startswith(expected_prefix)
    ):
        raise RuntimeError("site_service_request_stages_readback_unrecognized")
    return stage_id


def _strict_stage_semantics(item: dict[str, Any]) -> str:
    values = [item[field_name] for field_name in ("SEMANTICS", "semantics") if field_name in item]
    if not values:
        return ""
    normalized_values: list[str] = []
    for value in values:
        if value is None:
            normalized_values.append("")
            continue
        if not isinstance(value, str) or value.strip() != value:
            raise RuntimeError("site_service_request_stages_readback_unrecognized")
        normalized = value.upper()
        if normalized not in {"", "S", "F"}:
            raise RuntimeError("site_service_request_stages_readback_unrecognized")
        normalized_values.append(normalized)
    first_value = normalized_values[0]
    if any(value != first_value for value in normalized_values[1:]):
        raise RuntimeError("site_service_request_stages_readback_unrecognized")
    return first_value


def _strict_aliased_positive_int(
    item: dict[str, Any],
    *field_names: str,
    error_code: str,
) -> int:
    values = [item[field_name] for field_name in field_names if field_name in item]
    if not values:
        raise RuntimeError(error_code)
    parsed_values = [_positive_int(value) for value in values]
    if any(value is None for value in parsed_values):
        raise RuntimeError(error_code)
    first_value = parsed_values[0]
    if any(value != first_value for value in parsed_values[1:]):
        raise RuntimeError(error_code)
    assert first_value is not None
    return first_value


def _positive_int(value: Any) -> int | None:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized or not normalized.isascii() or not normalized.isdigit():
            return None
        parsed = int(normalized)
    else:
        return None
    return parsed if parsed > 0 else None


def _strict_pagination_offset(value: Any, *, error_code: str) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise RuntimeError(error_code)
    if parsed < 0:
        raise RuntimeError(error_code)
    return parsed


def _resolve_pagination_offset(
    *,
    top_next: Any,
    nested_next: Any,
    error_code: str,
) -> int | None:
    parsed_top = (
        _strict_pagination_offset(top_next, error_code=error_code) if top_next is not None else None
    )
    parsed_nested = (
        _strict_pagination_offset(nested_next, error_code=error_code)
        if nested_next is not None
        else None
    )
    if parsed_top is not None and parsed_nested is not None and parsed_top != parsed_nested:
        raise RuntimeError(error_code)
    return parsed_nested if parsed_nested is not None else parsed_top


def _normalized_field_name(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _field_name(entity_id: str, key: str) -> str:
    compact_key = "".join(character for character in key.upper() if character.isalnum())
    return f"UF_{entity_id}_{compact_key}"


def _xml_id(key: str) -> str:
    return f"MM_SITE_SERVICE_{key.upper()}"


if __name__ == "__main__":
    raise SystemExit(main())
