#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.services.expertise_bitrix import BitrixRestClient
from scripts.ensure_site_defect_archive_bitrix_process import (
    CUSTOM_FIELD_SPECS as LEGACY_FIELD_SPECS,
)
from scripts.ensure_site_defect_archive_bitrix_process import (
    _field_xml_id_for_spec as legacy_field_xml_id,
)

PROCESS_TITLE = "Сервисные обращения сайта"
WORKING_CATEGORY_TITLE = "Рабочие обращения сайта"
REQUEST_TYPE_FIELD_NAME = "UF_CRM_36_CUSTOMERREQUESTCHOICE"
REQUEST_TYPE_ENUM_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "warranty": (
        "expertise",
        ("Гарантия / проверка качества", "Нужна экспертиза"),
    ),
    "refund_money": ("refund_money", ("Возврат денег", "Вернуть деньги")),
    "replacement": ("replacement", ("Замена товара", "Замена")),
    "delivery_return": (
        "logistics_return",
        ("Доставка или возврат товара", "Доставка / возврат"),
    ),
    "consultation": (
        "clarify",
        ("Консультация / уточнение", "Разобраться", "Консультация"),
    ),
    "other": ("other", ("Другое", "Прочее")),
}
REQUEST_TYPE_ENUM_LABELS = {
    key: labels[0] for key, (_xml_id, labels) in REQUEST_TYPE_ENUM_TARGETS.items()
}

WORKING_STAGE_TITLES = {
    "NEW": "Новая",
    "PREPARATION": "В работе / уточнение",
    "CLARIFY": "В работе / уточнение",
    "CLIENT": "Ожидаем клиента или товар",
    "WAITING": "Ожидаем клиента или товар",
    "NEED_EXPERTISE": "Нужна экспертиза",
    "LINKED_EXPERTISE": "Экспертиза создана",
    "REFUND_DECISION": "Решение и возврат",
    "CLOSED": "Закрыто",
    "SUCCESS": "Закрыто",
    "FAIL": "Закрыто без решения",
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
    {"key": "site_sync_error", "title": "Ошибка синхронизации", "type": "string"},
    {
        "key": "return_decision_approved_by_user",
        "title": "Кто согласовал решение",
        "type": "employee",
    },
)

FORM_SECTIONS = (
    (
        "main",
        "Обращение",
        (
            "STAGE_ID",
            "TITLE",
            "ASSIGNED_BY_ID",
            "UF_CRM_36_CUSTOMERREQUESTCHOICE",
            "UF_CRM_36_PRIORITYCHOICE",
            "first_response_due_at",
            "site_sync_status",
        ),
    ),
    (
        "client",
        "Клиент и заказ",
        (
            "UF_CRM_36_CRMCONTACT",
            "UF_CRM_36_CRMCOMPANY",
            "UF_CRM_36_CUSTOMERCONTACT",
            "UF_CRM_36_CRMDEAL",
            "UF_CRM_36_ORDERREFS",
            "site_ticket_url",
        ),
    ),
    (
        "case",
        "Сообщение клиента",
        (
            "UF_CRM_36_PRODUCTMODEL",
            "UF_CRM_36_PROBLEMDESCRIPTION",
            "UF_CRM_36_PROBLEMTYPECHOICE",
            "UF_CRM_36_CLIENTFILES",
            "site_history",
        ),
    ),
    (
        "site_reply",
        "Ответ клиенту",
        (
            "site_reply_text",
            "site_reply_action",
            "site_reply_status",
            "first_response_at",
        ),
    ),
    (
        "return_economics",
        "Экономика возврата",
        (
            "UF_CRM_36_ITEMVALUE",
            "UF_CRM_36_ESTIMATEDRETURNCOST",
            "UF_CRM_36_RETURNECONOMICSRESULT",
            "UF_CRM_36_RETURNGOODSDECISION",
            "UF_CRM_36_RETURNLEAVEREASON",
            "return_decision_approved_by_user",
        ),
    ),
    (
        "work",
        "Решение и дальнейшие действия",
        (
            "UF_CRM_36_NEXTACTION",
            "UF_CRM_36_LINKEDEXPERTISECRM",
            "UF_CRM_36_RETURNCARRIER",
            "UF_CRM_36_RETURNTRACKINGNUMBER",
            "UF_CRM_36_RETURNTRACKINGCREATEDAT",
            "UF_CRM_36_RETURNSTATUS",
            "UF_CRM_36_DECISIONRESULT",
            "UF_CRM_36_WORKINGFILESURL",
        ),
    ),
    (
        "hints",
        "Подсказки для сотрудника",
        (
            "UF_CRM_36_NUMBERS",
            "UF_CRM_36_ANALYSISHINTS",
        ),
    ),
)

LEGACY_FIELD_TITLE_OVERRIDES = {
    "source": "Источник обращения",
    "customer_contact": "Телефон и e-mail клиента",
    "crm_contact": "Карточка клиента в CRM",
    "crm_company": "Компания клиента",
    "crm_deal": "Заказ / сделка в CRM",
    "order_refs": "Номер заказа или перемещения",
    "problem_description": "Сообщение клиента / что произошло",
    "customer_request": "Требование клиента (старое поле)",
    "customer_request_choice": "Тип обращения",
    "priority": "Приоритет (старое поле)",
    "next_action": "Следующее действие",
    "reaction_deadline": "Старый срок реакции (не используется)",
    "decision_result": "Решение по обращению",
    "client_files": "Файлы клиента",
    "working_files_url": "Рабочая папка с файлами",
    "return_decision_approved_by": "Кто согласовал решение (старое поле)",
    "analysis_hints": "Подсказки для сотрудника",
    "old_dialog_id": "ID старого диалога",
    "old_post_message_id": "ID сообщения старого чата",
    "old_comment_chat_id": "ID обсуждения старого чата",
    "search_text": "Текст для поиска",
    "problem_type": "Тип проблемы (старое поле)",
    "problem_type_choice": "Причина обращения",
    "linked_expertise": "Связанная экспертиза (старое поле)",
    "backend_case_id": "Внутренний номер обращения",
    "idempotency_key": "Ключ защиты от дублей",
}

TECHNICAL_FORM_FIELD_NAMES = {
    "UF_CRM_36_SOURCE",
    "UF_CRM_36_CUSTOMERREQUEST",
    "UF_CRM_36_PRIORITY",
    "UF_CRM_36_REACTIONDEADLINE",
    "UF_CRM_36_OLDDIALOGID",
    "UF_CRM_36_OLDPOSTMESSAGEID",
    "UF_CRM_36_OLDCOMMENTCHATID",
    "UF_CRM_36_SEARCHTEXT",
    "UF_CRM_36_PROBLEMTYPE",
    "UF_CRM_36_LINKEDEXPERTISE",
    "UF_CRM_36_BACKENDCASEID",
    "UF_CRM_36_IDEMPOTENCYKEY",
    "UF_CRM_36_SITETICKETID",
    "UF_CRM_36_SITELASTSYNCAT",
    "UF_CRM_36_SITESYNCERROR",
    "UF_CRM_36_RETURNDECISIONAPPROVEDBY",
}

REQUIRED_STAGE_CODES = {
    "new": "NEW",
    "success": "SUCCESS",
    "failure": "FAIL",
}


class BitrixJsonApi(Protocol):
    def call(
        self,
        method: str,
        params: list[tuple[str, str]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

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
    ux_mismatches: tuple[str, ...]
    label_advisories: tuple[str, ...]


def _localized_label(value: Any, *, language: str) -> str | None:
    if not isinstance(value, dict):
        return None
    label = value.get(language)
    return label if isinstance(label, str) and label.strip() == label and label else None


def _desired_field_titles_by_xml_id() -> dict[str, str]:
    titles = {
        legacy_field_xml_id(spec): LEGACY_FIELD_TITLE_OVERRIDES.get(
            str(spec["logical_key"]),
            str(spec["title"]),
        )
        for spec in LEGACY_FIELD_SPECS
    }
    titles.update({_xml_id(spec["key"]): str(spec["title"]) for spec in FIELD_SPECS})
    return titles


def _desired_field_title(field: dict[str, Any]) -> str | None:
    xml_id = str(field.get("xmlId") or field.get("XML_ID") or "").strip()
    title = _desired_field_titles_by_xml_id().get(xml_id)
    if title is not None:
        return title
    if _normalized_field_name(field.get("fieldName")) == _normalized_field_name(
        REQUEST_TYPE_FIELD_NAME
    ):
        return "Тип обращения"
    return None


def _managed_form_field_names(
    *,
    fields: list[dict[str, Any]],
    field_map: dict[str, str],
) -> set[str]:
    known_titles = _desired_field_titles_by_xml_id()
    managed = {
        "STAGE_ID",
        "TITLE",
        "ASSIGNED_BY_ID",
        REQUEST_TYPE_FIELD_NAME,
        *TECHNICAL_FORM_FIELD_NAMES,
        *field_map.values(),
    }
    for field in fields:
        xml_id = str(field.get("xmlId") or field.get("XML_ID") or "").strip()
        field_name = field.get("fieldName")
        if xml_id in known_titles and isinstance(field_name, str) and field_name.strip():
            managed.add(field_name.strip())
    return managed


def _stage_title(stage: dict[str, Any]) -> str | None:
    values = [stage[key] for key in ("NAME", "name") if key in stage]
    if not values:
        return None
    if any(not isinstance(value, str) or value.strip() != value or not value for value in values):
        raise RuntimeError("site_service_request_stages_readback_unrecognized")
    if any(value != values[0] for value in values[1:]):
        raise RuntimeError("site_service_request_stage_mapping_ambiguous")
    return values[0]


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
    parser.add_argument(
        "--snapshot-output",
        type=Path,
        help="Save a metadata-only process snapshot before ensure/apply.",
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
    process_title: str | None = None,
    category_title: str | None = None,
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
    desired_form = build_form(field_map)
    managed_form_fields = _managed_form_field_names(
        fields=fields,
        field_map=field_map,
    )
    form = merge_form(
        existing_form or [],
        desired_form,
        managed_field_names=managed_form_fields,
    )
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
    ux_mismatches: list[str] = []
    label_advisories: list[str] = []
    if process_title is not None and process_title != PROCESS_TITLE:
        ux_mismatches.append("process_title")
    if category_title is not None and category_title != WORKING_CATEGORY_TITLE:
        ux_mismatches.append("working_category_title")
    for field in fields:
        desired_title = _desired_field_title(field)
        if desired_title is None:
            continue
        field_name = str(field.get("fieldName") or "unknown")
        if _localized_label(field.get("editFormLabel"), language="ru") != desired_title:
            ux_mismatches.append(f"field_label:{field_name}:editFormLabel")
        for label_key in ("listColumnLabel", "listFilterLabel"):
            if _localized_label(field.get(label_key), language="ru") != desired_title:
                label_advisories.append(f"field_label:{field_name}:{label_key}")
        if field.get("fieldName") == REQUEST_TYPE_FIELD_NAME:
            request_mapping = _request_type_enum_mapping(field)
            enum_by_id = {_strict_enum_row_id(row): row for row in _require_enum_rows(field)}
            for logical_key, desired_label in REQUEST_TYPE_ENUM_LABELS.items():
                enum_id = request_mapping.get(f"request_type_{logical_key}")
                row = enum_by_id.get(str(enum_id or ""))
                actual_label = _strict_optional_enum_string(row, "value", "VALUE") if row else None
                if actual_label != desired_label:
                    ux_mismatches.append(f"request_type_label:{logical_key}")
    for stage in stages:
        stage_id = _strict_stage_id(
            stage,
            expected_prefix=f"DT{entity_type_id}_{category_id}:",
        )
        code = stage_id.rsplit(":", 1)[-1].upper()
        desired_title = WORKING_STAGE_TITLES.get(code)
        if desired_title is not None and _stage_title(stage) != desired_title:
            ux_mismatches.append(f"stage_title:{code}")
    if existing_form is not None and existing_form != form:
        ux_mismatches.append("working_form")
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
        ux_mismatches=tuple(sorted(set(ux_mismatches))),
        label_advisories=tuple(sorted(set(label_advisories))),
    )


def build_form(field_map: dict[str, str]) -> list[dict[str, Any]]:
    builtin_names = {"STAGE_ID", "TITLE", "ASSIGNED_BY_ID"}
    sections: list[dict[str, Any]] = []
    for name, title, keys in FORM_SECTIONS:
        elements = []
        for key in keys:
            field_name = field_map.get(
                key,
                key if key in builtin_names or key.startswith("UF_") else "",
            )
            if field_name:
                elements.append({"name": field_name, "optionFlags": 1})
        if elements:
            sections.append({"name": name, "title": title, "type": "section", "elements": elements})
    return sections


def merge_form(
    existing: list[dict[str, Any]],
    desired: list[dict[str, Any]],
    *,
    managed_field_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    managed = set(managed_field_names or ())
    merged = json.loads(json.dumps(desired, ensure_ascii=False))
    desired_sections = {
        str(section.get("name") or ""): section for section in merged if isinstance(section, dict)
    }
    desired_names = {
        str(element.get("name") or "")
        for section in merged
        if isinstance(section, dict)
        for element in section.get("elements") or []
        if isinstance(element, dict)
    }
    preserved_sections: list[dict[str, Any]] = []
    for source_section in existing:
        if not isinstance(source_section, dict):
            continue
        unknown_elements = [
            json.loads(json.dumps(element, ensure_ascii=False))
            for element in source_section.get("elements") or []
            if isinstance(element, dict)
            and str(element.get("name") or "") not in managed
            and str(element.get("name") or "") not in desired_names
        ]
        if not unknown_elements:
            continue
        section_name = str(source_section.get("name") or "")
        target = desired_sections.get(section_name)
        if target is not None:
            target.setdefault("elements", []).extend(unknown_elements)
            desired_names.update(str(element.get("name") or "") for element in unknown_elements)
            continue
        preserved = json.loads(json.dumps(source_section, ensure_ascii=False))
        preserved["elements"] = unknown_elements
        preserved_sections.append(preserved)
        desired_names.update(str(element.get("name") or "") for element in unknown_elements)
    merged.extend(preserved_sections)
    return merged


def _request_type_enum_update(field: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = _request_type_enum_mapping(field)
    desired_by_id = {
        str(mapping[f"request_type_{logical_key}"]): desired_label
        for logical_key, desired_label in REQUEST_TYPE_ENUM_LABELS.items()
    }
    rows: list[dict[str, Any]] = []
    for index, current in enumerate(_require_enum_rows(field), start=1):
        enum_id = _strict_enum_row_id(current)
        value = desired_by_id.get(
            enum_id,
            _strict_optional_enum_string(current, "value", "VALUE") or "",
        )
        xml_id = _strict_optional_enum_string(current, "xmlId", "XML_ID") or ""
        rows.append(
            {
                "id": enum_id,
                "value": value,
                "xmlId": xml_id,
                "sort": _strict_enum_sort(current, fallback=index * 100),
                "def": _strict_enum_default(current),
            }
        )
    return rows


def _field_metadata_update_payload(field: dict[str, Any]) -> dict[str, Any] | None:
    desired_title = _desired_field_title(field)
    if desired_title is None:
        return None
    update: dict[str, Any] = {"languageId": "ru"}
    if _localized_label(field.get("editFormLabel"), language="ru") != desired_title:
        update["editFormLabel"] = {"ru": desired_title}
    field_name = str(field.get("fieldName") or "").strip()
    if field_name in TECHNICAL_FORM_FIELD_NAMES:
        for key, desired in (
            ("showFilter", "N"),
            ("showInList", "N"),
            ("editInList", "N"),
        ):
            if str(field.get(key) or "") != desired:
                update[key] = desired
    if field_name == REQUEST_TYPE_FIELD_NAME:
        desired_enum = _request_type_enum_update(field)
        actual_labels = [
            _strict_optional_enum_string(row, "value", "VALUE") for row in _require_enum_rows(field)
        ]
        if actual_labels != [row["value"] for row in desired_enum]:
            update["userTypeId"] = "enumeration"
            update["enum"] = desired_enum
    return update if len(update) > 1 else None


def _ensure_field_metadata(
    api: BitrixJsonApi,
    *,
    fields: list[dict[str, Any]],
) -> None:
    for field in fields:
        update = _field_metadata_update_payload(field)
        if update is None:
            continue
        field_id = _strict_aliased_positive_int(
            field,
            "id",
            "ID",
            error_code="site_service_request_field_readback_unrecognized",
        )
        api.call_json(
            "userfieldconfig.update",
            {
                "moduleId": "crm",
                "id": field_id,
                "field": update,
            },
        )


def _ensure_process_title(
    api: BitrixJsonApi,
    *,
    process_type: dict[str, Any],
) -> None:
    if process_type["title"] == PROCESS_TITLE:
        return
    api.call_json(
        "crm.type.update",
        {"id": int(process_type["id"]), "fields": {"title": PROCESS_TITLE}},
    )


def _ensure_working_category_title(
    api: BitrixJsonApi,
    *,
    entity_type_id: int,
    category: dict[str, Any],
) -> None:
    if category["name"] == WORKING_CATEGORY_TITLE:
        return
    api.call_json(
        "crm.category.update",
        {
            "entityTypeId": entity_type_id,
            "id": int(category["id"]),
            "fields": {"name": WORKING_CATEGORY_TITLE},
        },
    )


def _ensure_working_stage_titles(
    api: BitrixJsonApi,
    *,
    entity_type_id: int,
    category_id: int,
    stages: list[dict[str, Any]],
) -> None:
    expected_prefix = f"DT{entity_type_id}_{category_id}:"
    for stage in stages:
        stage_id = _strict_stage_id(stage, expected_prefix=expected_prefix)
        code = stage_id.rsplit(":", 1)[-1].upper()
        desired_title = WORKING_STAGE_TITLES.get(code)
        if desired_title is None or _stage_title(stage) == desired_title:
            continue
        internal_id = _strict_aliased_positive_int(
            stage,
            "ID",
            "id",
            error_code="site_service_request_stages_readback_unrecognized",
        )
        api.call_json(
            "crm.status.update",
            {"id": internal_id, "fields": {"NAME": desired_title}},
        )


def build_process_snapshot(
    api: BitrixJsonApi,
    *,
    settings: Settings,
) -> dict[str, Any]:
    entity_type_id = settings.site_service_requests_bitrix_entity_type_id
    process_type = _require_process_type(api, entity_type_id=entity_type_id)
    entity_id = f"CRM_{int(process_type['id'])}"
    categories = _list_categories(api, entity_type_id=entity_type_id)
    fields = _read_fields(api, entity_id=entity_id)
    stages_by_category: dict[str, list[dict[str, Any]]] = {}
    forms_by_category: dict[str, list[dict[str, Any]]] = {}
    for category in categories:
        category_id = int(category["id"])
        stages_by_category[str(category_id)] = _list_stages(
            api,
            entity_type_id=entity_type_id,
            category_id=category_id,
        )
        forms_by_category[str(category_id)] = _require_recognized_form(
            api.call_json(
                "crm.item.details.configuration.get",
                {
                    "entityTypeId": entity_type_id,
                    "scope": "C",
                    "extras": {"categoryId": category_id},
                },
            )
        )
    return {
        "capturedAt": datetime.now(UTC).isoformat(),
        "entityTypeId": entity_type_id,
        "processType": process_type,
        "categories": categories,
        "fields": fields,
        "stagesByCategory": stages_by_category,
        "formsByCategory": forms_by_category,
    }


def ensure(
    api: BitrixJsonApi,
    *,
    settings: Settings,
    apply: bool,
) -> EnsurePlan:
    process_type = _require_process_type(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
    )
    type_id = int(process_type["id"])
    entity_id = f"CRM_{type_id}"
    category = _require_category(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
    )
    fields = _read_fields(api, entity_id=entity_id)
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
        process_title=str(process_type["title"]),
        category_title=str(category["name"]),
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
    if plan.missing_fields:
        refreshed_fields = _read_fields(api, entity_id=entity_id)
        refreshed_stages = _list_stages(
            api,
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            category_id=settings.site_service_requests_bitrix_working_category_id,
        )
    else:
        refreshed_fields = fields
        refreshed_stages = stages
    refreshed = build_plan(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        type_id=type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
        fields=refreshed_fields,
        stages=refreshed_stages,
        existing_form=existing_form,
        process_title=str(process_type["title"]),
        category_title=str(category["name"]),
    )
    if (
        refreshed.missing_fields
        or refreshed.type_mismatches
        or refreshed.missing_stages
        or refreshed.missing_enum_mappings
    ):
        raise RuntimeError("site_service_request_fields_readback_failed")
    _ensure_field_metadata(api, fields=refreshed_fields)
    _ensure_process_title(api, process_type=process_type)
    _ensure_working_category_title(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        category=category,
    )
    _ensure_working_stage_titles(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
        stages=refreshed_stages,
    )
    # Re-read immediately before the write so a concurrent administrator change
    # is merged instead of being replaced by the stale initial snapshot.
    latest_form = _require_recognized_form(
        api.call_json("crm.item.details.configuration.get", configuration_payload)
    )
    process_type = _require_process_type(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
    )
    category = _require_category(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
    )
    refreshed_fields = _read_fields(api, entity_id=entity_id)
    refreshed_stages = _list_stages(
        api,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
    )
    refreshed = build_plan(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        type_id=type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
        fields=refreshed_fields,
        stages=refreshed_stages,
        existing_form=latest_form,
        process_title=str(process_type["title"]),
        category_title=str(category["name"]),
    )
    non_form_ux_mismatches = [
        mismatch for mismatch in refreshed.ux_mismatches if mismatch != "working_form"
    ]
    if non_form_ux_mismatches:
        raise RuntimeError("site_service_request_ux_readback_failed")
    if latest_form != refreshed.form:
        api.call_json(
            "crm.item.details.configuration.set",
            {
                "entityTypeId": refreshed.entity_type_id,
                "scope": "C",
                "extras": {"categoryId": refreshed.category_id},
                "data": refreshed.form,
            },
        )
    final_form = _require_recognized_form(
        api.call_json("crm.item.details.configuration.get", configuration_payload)
    )
    final_plan = build_plan(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        type_id=type_id,
        category_id=settings.site_service_requests_bitrix_working_category_id,
        fields=refreshed_fields,
        stages=refreshed_stages,
        existing_form=final_form,
        process_title=str(process_type["title"]),
        category_title=str(category["name"]),
    )
    if (
        final_plan.missing_fields
        or final_plan.type_mismatches
        or final_plan.missing_stages
        or final_plan.missing_enum_mappings
        or final_plan.ux_mismatches
    ):
        raise RuntimeError("site_service_request_ux_readback_failed")
    return final_plan


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
        "uxMismatches": list(plan.ux_mismatches),
        "labelAdvisories": list(plan.label_advisories),
        "desiredProcessTitle": PROCESS_TITLE,
        "desiredWorkingCategoryTitle": WORKING_CATEGORY_TITLE,
        "desiredWorkingStageTitles": WORKING_STAGE_TITLES,
        "desiredRequestTypeLabels": REQUEST_TYPE_ENUM_LABELS,
        "technicalFormFields": sorted(TECHNICAL_FORM_FIELD_NAMES),
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
    api = BitrixRestClient(webhook)
    if args.snapshot_output is not None:
        snapshot = build_process_snapshot(api, settings=settings)
        args.snapshot_output.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot_output.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    plan = ensure(
        api,
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


def _read_fields(api: BitrixJsonApi, *, entity_id: str) -> list[dict[str, Any]]:
    fields = _list_fields(api, entity_id=entity_id)
    enriched_fields: list[dict[str, Any]] = []
    for field in fields:
        field_id = _strict_aliased_positive_int(
            field,
            "id",
            "ID",
            error_code="site_service_request_field_details_readback_unrecognized",
        )
        response = api.call_json(
            "userfieldconfig.get",
            {"moduleId": "crm", "id": field_id},
        )
        if not isinstance(response, dict):
            raise RuntimeError("site_service_request_field_details_readback_unrecognized")
        result = response.get("result")
        detailed_field = result.get("field") if isinstance(result, dict) else None
        if not isinstance(detailed_field, dict):
            raise RuntimeError("site_service_request_field_details_readback_unrecognized")
        detailed_id = _strict_aliased_positive_int(
            detailed_field,
            "id",
            "ID",
            error_code="site_service_request_field_details_readback_unrecognized",
        )
        detailed_entity_id = _strict_aliased_string(
            detailed_field,
            "entityId",
            "ENTITY_ID",
            error_code="site_service_request_field_details_readback_unrecognized",
        )
        detailed_field_name = _strict_aliased_string(
            detailed_field,
            "fieldName",
            "FIELD_NAME",
            error_code="site_service_request_field_details_readback_unrecognized",
        )
        detailed_user_type = _strict_aliased_string(
            detailed_field,
            "userTypeId",
            "USER_TYPE_ID",
            error_code="site_service_request_field_details_readback_unrecognized",
        )
        if (
            detailed_id != field_id
            or detailed_entity_id != entity_id
            or detailed_field_name != field["fieldName"]
            or detailed_user_type != field["userTypeId"]
        ):
            raise RuntimeError("site_service_request_field_details_readback_unrecognized")
        if detailed_user_type == "enumeration" and not isinstance(detailed_field.get("enum"), list):
            raise RuntimeError("site_service_request_field_details_readback_unrecognized")
        enriched_fields.append({**field, **detailed_field})
    return enriched_fields


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


def _require_process_type(api: BitrixJsonApi, *, entity_type_id: int) -> dict[str, Any]:
    params = [("filter[entityTypeId]", str(entity_type_id))]
    process_types: list[dict[str, Any]] = []
    seen_starts: set[int] = {0}
    page_count = 0
    while True:
        page_count += 1
        response = api.call("crm.type.list", params)
        if not isinstance(response, dict):
            raise RuntimeError("site_service_request_type_readback_unrecognized")
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("types"), list):
            raise RuntimeError("site_service_request_type_readback_unrecognized")
        for process_type in result["types"]:
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
            title = process_type.get("title")
            if (
                type_id <= 0
                or returned_entity_type_id != entity_type_id
                or not isinstance(title, str)
                or not title.strip()
                or title.strip() != title
            ):
                raise RuntimeError("site_service_request_type_readback_unrecognized")
            process_types.append(
                {
                    **process_type,
                    "id": type_id,
                    "entityTypeId": returned_entity_type_id,
                    "title": title,
                }
            )
        start = _resolve_pagination_offset(
            top_next=response.get("next"),
            nested_next=result.get("next"),
            error_code="site_service_request_type_pagination_invalid",
        )
        if start is None:
            break
        if page_count >= 100:
            raise RuntimeError("site_service_request_type_pagination_invalid")
        if start in seen_starts:
            raise RuntimeError("site_service_request_type_pagination_loop")
        seen_starts.add(start)
        params = [
            ("filter[entityTypeId]", str(entity_type_id)),
            ("start", str(start)),
        ]
    if len(process_types) != 1:
        raise RuntimeError("site_service_request_type_readback_unrecognized")
    return process_types[0]


def _require_process_type_id(api: BitrixJsonApi, *, entity_type_id: int) -> int:
    return int(_require_process_type(api, entity_type_id=entity_type_id)["id"])


def _list_categories(api: BitrixJsonApi, *, entity_type_id: int) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"entityTypeId": entity_type_id}
    categories: list[dict[str, Any]] = []
    seen_starts: set[int] = {0}
    page_count = 0
    while True:
        page_count += 1
        response = api.call_json("crm.category.list", payload)
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("categories"), list):
            raise RuntimeError("site_service_request_categories_readback_unrecognized")
        for category in result["categories"]:
            if not isinstance(category, dict):
                raise RuntimeError("site_service_request_categories_readback_unrecognized")
            category_id = _strict_aliased_positive_int(
                category,
                "id",
                "ID",
                error_code="site_service_request_categories_readback_unrecognized",
            )
            name = _strict_aliased_string(
                category,
                "name",
                "NAME",
                error_code="site_service_request_categories_readback_unrecognized",
            )
            if name is None or not name or name.strip() != name:
                raise RuntimeError("site_service_request_categories_readback_unrecognized")
            categories.append({**category, "id": category_id, "name": name})
        start = _resolve_pagination_offset(
            top_next=response.get("next"),
            nested_next=result.get("next"),
            error_code="site_service_request_categories_pagination_invalid",
        )
        if start is None:
            return categories
        if page_count >= 100 or start in seen_starts:
            raise RuntimeError("site_service_request_categories_pagination_invalid")
        seen_starts.add(start)
        payload = {"entityTypeId": entity_type_id, "start": start}


def _require_category(
    api: BitrixJsonApi,
    *,
    entity_type_id: int,
    category_id: int,
) -> dict[str, Any]:
    matches = [
        category
        for category in _list_categories(api, entity_type_id=entity_type_id)
        if int(category["id"]) == category_id
    ]
    if len(matches) != 1:
        raise RuntimeError("site_service_request_category_readback_unrecognized")
    return matches[0]


def _require_enum_rows(field: dict[str, Any]) -> list[dict[str, Any]]:
    rows = field.get("enum")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("site_service_request_enum_readback_unrecognized")
    enum_ids = [_strict_enum_row_id(row) for row in rows]
    for index, row in enumerate(rows, start=1):
        xml_id = _strict_optional_enum_string(row, "xmlId", "XML_ID")
        value = _strict_optional_enum_string(row, "value", "VALUE")
        if (
            value is None
            or not value
            or value.strip() != value
            or (xml_id is not None and xml_id.strip() != xml_id)
        ):
            raise RuntimeError("site_service_request_enum_readback_unrecognized")
        _strict_enum_sort(row, fallback=index * 100)
        _strict_enum_default(row)
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


def _strict_enum_sort(row: dict[str, Any], *, fallback: int) -> int:
    values = [row[field_name] for field_name in ("sort", "SORT") if field_name in row]
    if not values:
        return fallback
    parsed_values = [
        _strict_pagination_offset(
            value,
            error_code="site_service_request_enum_readback_unrecognized",
        )
        for value in values
    ]
    first_value = parsed_values[0]
    if any(value != first_value for value in parsed_values[1:]):
        raise RuntimeError("site_service_request_enum_readback_ambiguous")
    return first_value


def _strict_enum_default(row: dict[str, Any]) -> str:
    values = [row[field_name] for field_name in ("def", "DEF") if field_name in row]
    if not values:
        return "N"
    if any(not isinstance(value, str) or value not in {"Y", "N"} for value in values):
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
