#!/usr/bin/env python3
"""Create/update Bitrix Box smart-process for site defect archive/search."""

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

from app.core.config import Settings  # noqa: E402

DEFAULT_MAPPING_PATH = REPO_ROOT / "build/bitrix/site_defect_archive_mapping.json"
DEFAULT_DETAILS_CONFIG_PATH = REPO_ROOT / "build/bitrix/site_defect_details_configuration.json"
DEFAULT_PROCESS_TITLE = "Рекламации сайта / Браки сайта"
DEFAULT_PROCESS_CODE = "site_defect_archive"
WORKING_CATEGORY = "Рабочие рекламации"
ARCHIVE_CATEGORY = "Архив старого чата"
EXPERTISE_ENTITY_TYPE_ID = 1112

WORKING_STAGE_SPECS = [
    {"logical_key": "new", "code": "NEW", "name": "Новая", "sort": 100, "semantics": None},
    {
        "logical_key": "clarify",
        "code": "CLARIFY",
        "name": "Разобраться",
        "sort": 200,
        "semantics": None,
    },
    {
        "logical_key": "waiting_client_or_goods",
        "code": "WAITING",
        "name": "Ожидаем клиента / товар",
        "sort": 300,
        "semantics": None,
    },
    {
        "logical_key": "need_expertise",
        "code": "NEED_EXPERTISE",
        "name": "Нужна экспертиза",
        "sort": 400,
        "semantics": None,
    },
    {
        "logical_key": "linked_expertise",
        "code": "LINKED_EXPERTISE",
        "name": "Связано с экспертизой",
        "sort": 500,
        "semantics": None,
    },
    {
        "logical_key": "refund_or_decision",
        "code": "REFUND_DECISION",
        "name": "Возврат денег / решение",
        "sort": 600,
        "semantics": None,
    },
    {
        "logical_key": "closed",
        "code": "CLOSED",
        "name": "Закрыто",
        "sort": 900,
        "semantics": "S",
    },
]

ARCHIVE_STAGE_SPECS = [
    {"logical_key": "archive", "code": "ARCHIVE", "name": "Архив", "sort": 100, "semantics": None},
    {"logical_key": "closed", "code": "SUCCESS", "name": "Закрыто", "sort": 900, "semantics": "S"},
]

BUILTIN_FIELD_MAPPING = {"title": "TITLE", "assigned_by": "ASSIGNED_BY_ID"}

DETAIL_BUILTIN_ELEMENT_NAMES = {
    "stage": "STAGE_ID",
    "title": "TITLE",
    "assigned_by": "ASSIGNED_BY_ID",
}

CUSTOMER_REQUEST_ENUM = [
    {"xml_id": "clarify", "value": "Разобраться"},
    {"xml_id": "refund_money", "value": "Вернуть деньги"},
    {"xml_id": "replacement", "value": "Замена товара"},
    {"xml_id": "expertise", "value": "Нужна экспертиза"},
    {"xml_id": "logistics_return", "value": "Доставка / возврат"},
    {"xml_id": "other", "value": "Другое"},
]

PROBLEM_TYPE_ENUM = [
    {"xml_id": "model_mismatch", "value": "Перепутали модель"},
    {"xml_id": "return", "value": "Возврат"},
    {"xml_id": "money_refund", "value": "Деньги"},
    {"xml_id": "delivery", "value": "Доставка"},
    {"xml_id": "expertise", "value": "Экспертиза"},
    {"xml_id": "other", "value": "Прочее"},
]

PRIORITY_ENUM = [
    {"xml_id": "normal", "value": "Обычный", "default": True},
    {"xml_id": "urgent", "value": "Срочно"},
    {"xml_id": "conflict_risk", "value": "Риск конфликта"},
]

RETURN_ECONOMICS_ENUM = [
    {"xml_id": "take_back", "value": "Возврат экономически оправдан"},
    {"xml_id": "leave", "value": "Не забирать"},
    {"xml_id": "needs_manager", "value": "Нужна оценка старшего"},
    {"xml_id": "missing_data", "value": "Нужны стоимость и доставка"},
]

RETURN_GOODS_DECISION_ENUM = [
    {"xml_id": "needs_evaluation", "value": "Нужна оценка", "default": True},
    {"xml_id": "return_goods", "value": "Забрать товар"},
    {"xml_id": "leave_with_client", "value": "Оставить у клиента"},
    {"xml_id": "expertise_first", "value": "Сначала экспертиза"},
]

RETURN_LEAVE_REASON_ENUM = [
    {"xml_id": "return_cost_too_high", "value": "Доставка дороже товара"},
    {"xml_id": "low_item_value", "value": "Низкая полезная стоимость"},
    {"xml_id": "client_keep_after_refund", "value": "Клиент оставляет товар после решения"},
    {"xml_id": "photo_video_enough", "value": "Фото/видео достаточно"},
    {"xml_id": "other", "value": "Другое"},
]

RETURN_CARRIER_ENUM = [
    {"xml_id": "cdek", "value": "СДЭК"},
    {"xml_id": "post", "value": "Почта"},
    {"xml_id": "courier", "value": "Курьер"},
    {"xml_id": "other", "value": "Другое"},
]

RETURN_STATUS_ENUM = [
    {"xml_id": "needs_create", "value": "Нужно создать"},
    {"xml_id": "created", "value": "Создан"},
    {"xml_id": "sent_to_client", "value": "Передан клиенту"},
    {"xml_id": "in_transit", "value": "В пути"},
    {"xml_id": "received", "value": "Получен"},
    {"xml_id": "cancelled", "value": "Отменен"},
]

DETAIL_SECTION_SPECS = {
    "working": [
        {
            "name": "main",
            "title": "Рекламация",
            "elements": [
                "stage",
                "title",
                "assigned_by",
                "priority_choice",
                "reaction_deadline",
            ],
        },
        {
            "name": "client",
            "title": "Клиент",
            "elements": [
                "crm_contact",
                "crm_company",
                "customer_contact",
                "crm_deal",
                "order_refs",
            ],
        },
        {
            "name": "case",
            "title": "Суть обращения",
            "elements": [
                "product_model",
                "problem_description",
                "customer_request_choice",
                "problem_type_choice",
                "client_files",
            ],
        },
        {
            "name": "return_economics",
            "title": "Экономика возврата",
            "elements": [
                "item_value",
                "estimated_return_cost",
                "return_economics_result",
                "return_goods_decision",
                "return_leave_reason",
                "return_decision_approved_by",
            ],
        },
        {
            "name": "work",
            "title": "Разбор и итог",
            "elements": [
                "next_action",
                "linked_expertise_crm",
                "return_carrier",
                "return_tracking_number",
                "return_tracking_created_at",
                "return_status",
                "decision_result",
                "working_files_url",
            ],
        },
        {
            "name": "hints",
            "title": "Автоподсказки",
            "elements": [
                "numbers",
                "analysis_hints",
            ],
        },
    ],
    "archive": [
        {
            "name": "archive_main",
            "title": "Архив",
            "elements": [
                "stage",
                "title",
                "post_date",
                "author",
                "summary",
                "numbers",
                "problem_type",
                "folder_url",
            ],
        },
        {
            "name": "archive_counts",
            "title": "Комментарии и файлы",
            "elements": [
                "comment_count",
                "file_count",
                "linked_expertise",
            ],
        },
        {
            "name": "archive_technical",
            "title": "Технические поля архива",
            "elements": [
                "source",
                "old_dialog_id",
                "old_post_message_id",
                "old_comment_chat_id",
                "backend_case_id",
                "idempotency_key",
                "search_text",
            ],
        },
    ],
}

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "source",
        "title": "Источник",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "customer_contact",
        "title": "Контакт вручную / телефон",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "crm_contact",
        "title": "Контакт CRM",
        "type": "crm_contact",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "crm_company",
        "title": "Компания CRM",
        "type": "crm_company",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "crm_deal",
        "title": "Связанная сделка / заказ CRM",
        "type": "crm_deal",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "order_refs",
        "title": "Номер заказа / РБГУ / перемещение",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "product_model",
        "title": "Товар / модель",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "problem_description",
        "title": "Что случилось",
        "type": "text",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "customer_request",
        "title": "Тех. требование клиента",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "customer_request_choice",
        "title": "Что требует клиент",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": CUSTOMER_REQUEST_ENUM,
    },
    {
        "logical_key": "priority",
        "title": "Тех. приоритет",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "priority_choice",
        "title": "Приоритет",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": PRIORITY_ENUM,
    },
    {
        "logical_key": "next_action",
        "title": "Следующее действие",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "reaction_deadline",
        "title": "Срок реакции",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "decision_result",
        "title": "Решение / итог",
        "type": "text",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "client_files",
        "title": "Файлы клиента / фото / видео",
        "type": "file",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
        "multiple": True,
    },
    {
        "logical_key": "working_files_url",
        "title": "Ссылка на файлы",
        "type": "url",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "item_value",
        "title": "Стоимость товара / полезная стоимость",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "estimated_return_cost",
        "title": "Оценка обратной доставки",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "return_economics_result",
        "title": "Экономика возврата",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": RETURN_ECONOMICS_ENUM,
    },
    {
        "logical_key": "return_goods_decision",
        "title": "Товар возвращать?",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": RETURN_GOODS_DECISION_ENUM,
    },
    {
        "logical_key": "return_leave_reason",
        "title": "Причина не забирать",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": RETURN_LEAVE_REASON_ENUM,
    },
    {
        "logical_key": "return_decision_approved_by",
        "title": "Согласовал решение",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "return_carrier",
        "title": "Перевозчик возврата",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": RETURN_CARRIER_ENUM,
    },
    {
        "logical_key": "return_tracking_number",
        "title": "Трек-номер возврата",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "return_tracking_created_at",
        "title": "Дата создания трека",
        "type": "datetime",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "return_status",
        "title": "Статус возврата",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": RETURN_STATUS_ENUM,
    },
    {
        "logical_key": "analysis_hints",
        "title": "Подсказки анализа",
        "type": "text",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "old_dialog_id",
        "title": "Старый dialog_id",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "old_post_message_id",
        "title": "ID публикации старого чата",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "old_comment_chat_id",
        "title": "ID ветки комментариев старого чата",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "post_date",
        "title": "Дата публикации",
        "type": "datetime",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "author",
        "title": "Автор",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "summary",
        "title": "Краткое резюме",
        "type": "text",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "search_text",
        "title": "Полный текст для поиска",
        "type": "text",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "numbers",
        "title": "Найденные номера",
        "type": "text",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "problem_type",
        "title": "Тех. тип проблемы",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "problem_type_choice",
        "title": "Тип проблемы",
        "type": "enumeration",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
        "enum": PROBLEM_TYPE_ENUM,
    },
    {
        "logical_key": "archive_status",
        "title": "Статус разбора",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "folder_url",
        "title": "Ссылка на папку Disk",
        "type": "url",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "comment_count",
        "title": "Количество комментариев",
        "type": "integer",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "file_count",
        "title": "Количество файлов",
        "type": "integer",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "linked_expertise",
        "title": "Тех. связанная экспертиза",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "linked_expertise_crm",
        "title": "Связанная экспертиза",
        "type": "crm_dynamic",
        "entity_type_id": EXPERTISE_ENTITY_TYPE_ID,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "backend_case_id",
        "title": "Тех. ID кейса backend",
        "type": "integer",
        "required": False,
        "searchable": False,
        "edit_in_list": False,
    },
    {
        "logical_key": "idempotency_key",
        "title": "Тех. ключ идемпотентности",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
]

COMPATIBLE_BITRIX_TYPES = {
    "string": {"string", "crm"},
    "text": {"text", "string"},
    "integer": {"integer", "double"},
    "double": {"double", "integer"},
    "datetime": {"datetime", "date"},
    "url": {"string", "url"},
    "file": {"file"},
    "enumeration": {"enumeration"},
    "crm_contact": {"crm"},
    "crm_company": {"crm"},
    "crm_deal": {"crm"},
    "crm_dynamic": {"crm"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/update Bitrix Box smart-process for site defect archive."
    )
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--details-config-path", type=Path, default=DEFAULT_DETAILS_CONFIG_PATH)
    parser.add_argument("--skip-details-config", action="store_true")
    parser.add_argument("--title", default=DEFAULT_PROCESS_TITLE)
    parser.add_argument("--code", default=DEFAULT_PROCESS_CODE)
    return parser.parse_args()


def load_settings() -> Settings:
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
    pairs: list[tuple[str, str]] = []
    if params:
        items = params.items() if isinstance(params, dict) else params
        for key, value in items:
            pairs.extend(_flatten_bitrix_param(key, value))
    request = urllib.request.Request(
        webhook_base.rstrip("/") + f"/{method}.json",
        data=urllib.parse.urlencode(pairs, doseq=True).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Bitrix API {method}: HTTP {exc.code} {body}") from exc
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
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Bitrix API {method}: HTTP {exc.code} {body}") from exc
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


def ensure_category(
    webhook_base: str,
    *,
    entity_type_id: int,
    name: str,
    sort: int,
) -> dict[str, Any]:
    response = bitrix_call(webhook_base, "crm.category.list", {"entityTypeId": entity_type_id})
    categories = (response.get("result") or {}).get("categories") or []
    for category in categories:
        if str(category.get("name") or "").strip() == name:
            return category
    created = bitrix_call(
        webhook_base,
        "crm.category.add",
        {"entityTypeId": entity_type_id, "fields": {"name": name, "sort": sort}},
    )
    category = (created.get("result") or {}).get("category") or {}
    if not category:
        raise RuntimeError(f"Bitrix API crm.category.add returned empty category for name={name!r}")
    return category


def _status_entity_id(entity_type_id: int, category_id: int) -> str:
    return f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"


def list_stages(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
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
    specs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    entity_id = _status_entity_id(entity_type_id, category_id)
    stages = list_stages(webhook_base, entity_type_id=entity_type_id, category_id=category_id)
    by_status = {str(item.get("STATUS_ID")): item for item in stages}
    success_stage = next((item for item in stages if item.get("SEMANTICS") == "S"), None)
    failure_stages = sorted(
        [item for item in stages if item.get("SEMANTICS") == "F"],
        key=lambda item: int(item.get("SORT") or 0),
    )
    if success_stage is not None:
        bitrix_call(
            webhook_base,
            "crm.status.update",
            {"id": success_stage["ID"], "fields": {"SORT": 990}},
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
    result: dict[str, dict[str, Any]] = {}
    used_status_ids: set[str] = set()
    expected_status_ids = {f"DT{entity_type_id}_{category_id}:{spec['code']}" for spec in specs}

    def next_unused_process_stage() -> dict[str, Any] | None:
        for stage in process_stages:
            status_id = str(stage.get("STATUS_ID") or "")
            if status_id not in used_status_ids and status_id not in expected_status_ids:
                return stage
        return None

    for spec in specs:
        status_id = f"DT{entity_type_id}_{category_id}:{spec['code']}"
        fields = {"NAME": spec["name"], "SORT": spec["sort"]}
        if spec.get("semantics"):
            fields["SEMANTICS"] = spec["semantics"]
        current = by_status.get(status_id)
        if current is None:
            if spec.get("semantics") == "S":
                current = (
                    success_stage
                    if success_stage is not None
                    and str(success_stage.get("STATUS_ID") or "") not in used_status_ids
                    else None
                )
            elif spec.get("semantics") is None:
                current = next_unused_process_stage()
        if current is not None:
            used_status_ids.add(str(current.get("STATUS_ID") or ""))
            bitrix_call(webhook_base, "crm.status.update", {"id": current["ID"], "fields": fields})
            refreshed = list_stages(
                webhook_base,
                entity_type_id=entity_type_id,
                category_id=category_id,
            )
            stage = next(
                (
                    item
                    for item in refreshed
                    if str(item.get("NAME") or "").strip() == spec["name"]
                    and str(item.get("SEMANTICS") or "") == str(spec.get("semantics") or "")
                ),
                None,
            )
        else:
            bitrix_call(
                webhook_base,
                "crm.status.add",
                {
                    "fields": {
                        "ENTITY_ID": entity_id,
                        "STATUS_ID": status_id,
                        **fields,
                    }
                },
            )
            used_status_ids.add(status_id)
            refreshed = list_stages(
                webhook_base,
                entity_type_id=entity_type_id,
                category_id=category_id,
            )
            stage = next((item for item in refreshed if item.get("STATUS_ID") == status_id), None)
        if stage is None:
            raise RuntimeError(f"Не удалось найти stage {spec['name']} после ensure")
        result[spec["logical_key"]] = stage
        by_status[str(stage.get("STATUS_ID") or status_id)] = stage
    return result


def smart_process_userfield_entity_id(type_id: int) -> str:
    return f"CRM_{type_id}"


def list_userfields(webhook_base: str, *, entity_id: str) -> list[dict[str, Any]]:
    response = bitrix_call(
        webhook_base,
        "userfieldconfig.list",
        {"moduleId": "crm", "filter": {"entityId": entity_id}},
    )
    return (response.get("result") or {}).get("fields") or []


def _slug_suffix(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]+", "_", value.upper())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:32] or "FIELD"


def _field_name_for_spec(entity_id: str, spec: dict[str, Any]) -> str:
    compact_suffix = _slug_suffix(spec["logical_key"]).replace("_", "")
    return f"UF_{entity_id}_{compact_suffix}"


def _field_xml_id_for_spec(spec: dict[str, Any]) -> str:
    return f"UF_CRM_SITE_DEFECT_{_slug_suffix(spec['logical_key'])}"


def _field_config_for_spec(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    field_type = spec["type"]
    if field_type == "string":
        return "string", {"DEFAULT_VALUE": "", "SIZE": 20, "ROWS": 1}
    if field_type == "text":
        return "string", {"DEFAULT_VALUE": "", "SIZE": 50, "ROWS": 5}
    if field_type == "integer":
        return "integer", {"SIZE": 20, "MIN_VALUE": 0, "MAX_VALUE": 0, "DEFAULT_VALUE": None}
    if field_type == "double":
        return (
            "double",
            {"SIZE": 20, "PRECISION": 2, "MIN_VALUE": 0, "MAX_VALUE": 0, "DEFAULT_VALUE": None},
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
    if field_type == "file":
        return "file", {"SIZE": 20, "LIST_WIDTH": 0, "LIST_HEIGHT": 0, "MAX_ALLOWED_SIZE": 0}
    if field_type == "enumeration":
        return "enumeration", {"DISPLAY": "LIST", "LIST_HEIGHT": 1, "SHOW_NO_VALUE": "Y"}
    if field_type == "crm_contact":
        return "crm", {"LEAD": "N", "CONTACT": "Y", "COMPANY": "N", "DEAL": "N"}
    if field_type == "crm_company":
        return "crm", {"LEAD": "N", "CONTACT": "N", "COMPANY": "Y", "DEAL": "N"}
    if field_type == "crm_deal":
        return "crm", {"LEAD": "N", "CONTACT": "N", "COMPANY": "N", "DEAL": "Y"}
    if field_type == "crm_dynamic":
        entity_type_id = int(spec["entity_type_id"])
        return (
            "crm",
            {
                "LEAD": "N",
                "CONTACT": "N",
                "COMPANY": "N",
                "DEAL": "N",
                f"DYNAMIC_{entity_type_id}": "Y",
            },
        )
    raise RuntimeError(f"Unsupported site defect archive field type: {field_type}")


def _enum_options_for_spec(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, option in enumerate(spec.get("enum") or [], start=1):
        value = str(option["value"])
        xml_id = str(option.get("xml_id") or option.get("xmlId") or _slug_suffix(value))
        rows.append(
            {
                "value": value,
                "xmlId": xml_id,
                "def": "Y" if option.get("default") else "N",
                "sort": 100 + index * 100,
            }
        )
    return rows


def _enum_values(field: dict[str, Any]) -> list[str]:
    return [
        str(item.get("value") or item.get("VALUE") or "").strip()
        for item in field.get("enum") or []
        if str(item.get("value") or item.get("VALUE") or "").strip()
    ]


def _enum_options_for_update(spec: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    current_by_value = {
        str(item.get("value") or item.get("VALUE") or "").strip(): item
        for item in current.get("enum") or []
    }
    rows: list[dict[str, Any]] = []
    for option in _enum_options_for_spec(spec):
        existing = current_by_value.get(option["value"])
        if existing:
            enum_id = existing.get("id") or existing.get("ID")
            if enum_id:
                option["id"] = str(enum_id)
            option["xmlId"] = str(
                existing.get("xmlId") or existing.get("XML_ID") or option["xmlId"]
            )
        rows.append(option)
    return rows


def _enum_map_for_spec(spec: dict[str, Any], field: dict[str, Any]) -> dict[str, str]:
    current_by_value = {
        str(item.get("value") or item.get("VALUE") or "").strip(): item
        for item in field.get("enum") or []
    }
    result: dict[str, str] = {}
    for option in _enum_options_for_spec(spec):
        current = current_by_value.get(option["value"])
        enum_id = str((current or {}).get("id") or (current or {}).get("ID") or "").strip()
        xml_id = str(option.get("xmlId") or "").strip()
        if xml_id and enum_id:
            result[xml_id] = enum_id
    return result


def _spec_searchable(spec: dict[str, Any]) -> bool:
    return bool(spec.get("searchable", False))


def _spec_edit_in_list(spec: dict[str, Any]) -> bool:
    return bool(spec.get("edit_in_list", True))


def _spec_show_filter(spec: dict[str, Any]) -> str:
    return "E" if _spec_searchable(spec) else "N"


def ensure_custom_fields(
    webhook_base: str, *, process_type: dict[str, Any]
) -> list[dict[str, Any]]:
    type_id = int(process_type["id"])
    entity_id = smart_process_userfield_entity_id(type_id)
    existing_fields = list_userfields(webhook_base, entity_id=entity_id)
    existing_by_name = {str(item.get("fieldName") or ""): item for item in existing_fields}
    existing_by_xml_id = {
        str(item.get("xmlId") or ""): item for item in existing_fields if item.get("xmlId")
    }
    rows: list[dict[str, Any]] = []
    for index, spec in enumerate(CUSTOM_FIELD_SPECS, start=1):
        field_name = _field_name_for_spec(entity_id, spec)
        xml_id = _field_xml_id_for_spec(spec)
        title = spec["title"]
        user_type_id, settings = _field_config_for_spec(spec)
        current = existing_by_xml_id.get(xml_id) or existing_by_name.get(field_name)
        action = "updated"
        if current is None:
            field = {
                "entityId": entity_id,
                "fieldName": field_name,
                "userTypeId": user_type_id,
                "xmlId": xml_id,
                "multiple": "Y" if spec.get("multiple") else "N",
                "mandatory": "Y" if spec.get("required") else "N",
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
            action = "created"
        update_field = {
            "languageId": "ru",
            "xmlId": xml_id,
            "showFilter": _spec_show_filter(spec),
            "isSearchable": "Y" if _spec_searchable(spec) else "N",
            "editInList": "Y" if _spec_edit_in_list(spec) else "N",
            "sort": 100 + index * 10,
            "editFormLabel": {"ru": title},
            "listColumnLabel": {"ru": title},
            "listFilterLabel": {"ru": title},
        }
        bitrix_call(
            webhook_base,
            "userfieldconfig.update",
            {
                "moduleId": "crm",
                "id": current["id"],
                "field": update_field,
            },
        )
        refreshed_response = bitrix_call(
            webhook_base,
            "userfieldconfig.get",
            {"moduleId": "crm", "id": current["id"]},
        )
        current = (refreshed_response.get("result") or {}).get("field") or current
        if user_type_id == "enumeration":
            expected_values = [option["value"] for option in _enum_options_for_spec(spec)]
            if _enum_values(current) != expected_values:
                bitrix_call(
                    webhook_base,
                    "userfieldconfig.update",
                    {
                        "moduleId": "crm",
                        "id": current["id"],
                        "field": {
                            "userTypeId": "enumeration",
                            "enum": _enum_options_for_update(spec, current),
                        },
                    },
                )
                refreshed_response = bitrix_call(
                    webhook_base,
                    "userfieldconfig.get",
                    {"moduleId": "crm", "id": current["id"]},
                )
                current = (refreshed_response.get("result") or {}).get("field") or current
        rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": title,
                "multiple": bool(spec.get("multiple")),
                "field_name": current.get("fieldName") or field_name,
                "field_id": current.get("id"),
                "xml_id": xml_id,
                "enum_map": (
                    _enum_map_for_spec(spec, current) if user_type_id == "enumeration" else {}
                ),
                "action": action,
            }
        )
        refreshed = {
            "id": current.get("id"),
            "fieldName": current.get("fieldName") or field_name,
            "userTypeId": current.get("userTypeId") or user_type_id,
            "xmlId": xml_id,
        }
        existing_by_name[str(refreshed["fieldName"])] = refreshed
        existing_by_xml_id[xml_id] = refreshed
    return rows


def field_type_matches_expected(expected_type: str, actual_type: str | None) -> bool:
    if not actual_type:
        return False
    return actual_type in COMPATIBLE_BITRIX_TYPES.get(expected_type, {expected_type})


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
        actual_type = str((field_info or {}).get("userTypeId") or "").strip() or None
        type_matches = (
            field_type_matches_expected(spec["type"], actual_type) if field_code else None
        )
        enum_field_info = field_info
        if spec["type"] == "enumeration" and field_info and field_info.get("id"):
            refreshed_response = bitrix_call(
                webhook_base,
                "userfieldconfig.get",
                {"moduleId": "crm", "id": field_info["id"]},
            )
            enum_field_info = (refreshed_response.get("result") or {}).get("field") or field_info
        if field_code:
            resolved[spec["logical_key"]] = field_code
        spec_rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": spec["title"],
                "type": spec["type"],
                "required": spec["required"],
                "multiple": bool(spec.get("multiple")),
                "bitrix_code": field_code,
                "found": bool(field_code),
                "actual_type": actual_type,
                "type_matches": type_matches,
                "enum_map": (
                    _enum_map_for_spec(spec, enum_field_info or {})
                    if spec["type"] == "enumeration" and enum_field_info
                    else {}
                ),
            }
        )
    return resolved, spec_rows


def detail_element_name_for_logical_key(
    logical_key: str,
    *,
    field_mapping: dict[str, str],
) -> str | None:
    builtin = DETAIL_BUILTIN_ELEMENT_NAMES.get(logical_key)
    if builtin:
        return builtin
    return field_mapping.get(logical_key)


def build_details_configuration(
    *,
    category_key: str,
    field_mapping: dict[str, str],
) -> list[dict[str, Any]]:
    sections = DETAIL_SECTION_SPECS[category_key]
    configuration: list[dict[str, Any]] = []
    for section in sections:
        elements: list[dict[str, Any]] = []
        for logical_key in section["elements"]:
            detail_name = detail_element_name_for_logical_key(
                logical_key,
                field_mapping=field_mapping,
            )
            if detail_name:
                elements.append({"name": detail_name, "optionFlags": 1})
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
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
    category_key: str,
    field_mapping: dict[str, str],
    path: Path,
) -> tuple[list[dict[str, Any]], Path]:
    configuration = build_details_configuration(
        category_key=category_key,
        field_mapping=field_mapping,
    )
    if not configuration:
        raise RuntimeError(f"Не удалось собрать форму карточки для category={category_key}")
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
    applied = verification.get("result") or configuration
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(applied, ensure_ascii=False, indent=2), encoding="utf-8")
    return applied, path


def build_mapping_payload(
    *,
    process_type: dict[str, Any],
    working_category: dict[str, Any],
    archive_category: dict[str, Any],
    working_stages: dict[str, dict[str, Any]],
    archive_stages: dict[str, dict[str, Any]],
    field_mapping: dict[str, str],
    field_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    entity_type_id = int(process_type["entityTypeId"])
    working_category_id = int(working_category["id"])
    archive_category_id = int(archive_category["id"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "process": {
            "title": process_type.get("title"),
            "code": process_type.get("code"),
            "type_id": int(process_type["id"]),
            "entity_type_id": entity_type_id,
        },
        "categories": {
            "working": {"id": working_category_id, "name": working_category.get("name")},
            "archive": {"id": archive_category_id, "name": archive_category.get("name")},
        },
        "stage_map": {
            "working": {
                key: str(value.get("STATUS_ID") or "")
                for key, value in working_stages.items()
                if value.get("STATUS_ID")
            },
            "archive": {
                key: str(value.get("STATUS_ID") or "")
                for key, value in archive_stages.items()
                if value.get("STATUS_ID")
            },
        },
        "field_map": field_mapping,
        "enum_map": {
            item["logical_key"]: item["enum_map"] for item in field_specs if item.get("enum_map")
        },
        "working_form": {
            "primary_fields": [
                "TITLE",
                "crm_contact",
                "crm_company",
                "customer_contact",
                "crm_deal",
                "order_refs",
                "product_model",
                "problem_description",
                "customer_request_choice",
                "problem_type_choice",
                "priority_choice",
                "item_value",
                "estimated_return_cost",
                "return_economics_result",
                "return_goods_decision",
                "return_leave_reason",
                "return_decision_approved_by",
                "assigned_by",
                "next_action",
                "reaction_deadline",
                "linked_expertise_crm",
                "return_carrier",
                "return_tracking_number",
                "return_tracking_created_at",
                "return_status",
                "decision_result",
                "client_files",
                "working_files_url",
            ],
            "archive_fields_to_move_down": [
                "old_dialog_id",
                "old_post_message_id",
                "old_comment_chat_id",
                "idempotency_key",
                "backend_case_id",
                "search_text",
                "customer_request",
                "problem_type",
                "priority",
                "linked_expertise",
            ],
        },
        "field_specs": field_specs,
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
    }


def save_mapping(mapping: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    settings = load_settings()
    webhook_base = (
        settings.site_defect_archive_bitrix_webhook_url or settings.expertise_bitrix_webhook_url
    )
    if not webhook_base:
        raise RuntimeError(
            "Не задан SITE_DEFECT_ARCHIVE_BITRIX_WEBHOOK_URL или EXPERTISE_BITRIX_WEBHOOK_URL"
        )
    current_user = bitrix_call(webhook_base, "user.current").get("result") or {}
    process_type = ensure_type(webhook_base, title=args.title, code=args.code)
    entity_type_id = int(process_type["entityTypeId"])
    working_category = ensure_category(
        webhook_base,
        entity_type_id=entity_type_id,
        name=WORKING_CATEGORY,
        sort=100,
    )
    archive_category = ensure_category(
        webhook_base,
        entity_type_id=entity_type_id,
        name=ARCHIVE_CATEGORY,
        sort=900,
    )
    working_stages = ensure_stages(
        webhook_base,
        entity_type_id=entity_type_id,
        category_id=int(working_category["id"]),
        specs=WORKING_STAGE_SPECS,
    )
    archive_stages = ensure_stages(
        webhook_base,
        entity_type_id=entity_type_id,
        category_id=int(archive_category["id"]),
        specs=ARCHIVE_STAGE_SPECS,
    )
    fields = ensure_custom_fields(webhook_base, process_type=process_type)
    field_mapping, field_specs = discover_field_mapping(webhook_base, process_type)
    mapping = build_mapping_payload(
        process_type=process_type,
        working_category=working_category,
        archive_category=archive_category,
        working_stages=working_stages,
        archive_stages=archive_stages,
        field_mapping=field_mapping,
        field_specs=field_specs,
    )
    details_paths: dict[str, str] = {}
    if not args.skip_details_config:
        for category_key, category in (
            ("working", working_category),
            ("archive", archive_category),
        ):
            details_path = args.details_config_path.with_name(
                f"{args.details_config_path.stem}_{category_key}"
                f"{args.details_config_path.suffix}"
            )
            _configuration, saved_path = ensure_details_configuration(
                webhook_base,
                entity_type_id=entity_type_id,
                category_id=int(category["id"]),
                category_key=category_key,
                field_mapping=field_mapping,
                path=details_path,
            )
            details_paths[category_key] = str(saved_path)
    mapping["details_configuration_paths"] = details_paths
    save_mapping(mapping, args.mapping_path)
    env_ready = {
        "SITE_DEFECT_ARCHIVE_BITRIX_ENTITY_TYPE_ID": entity_type_id,
        "SITE_DEFECT_ARCHIVE_BITRIX_WORKING_CATEGORY_ID": int(working_category["id"]),
        "SITE_DEFECT_ARCHIVE_BITRIX_ARCHIVE_CATEGORY_ID": int(archive_category["id"]),
        "SITE_DEFECT_ARCHIVE_BITRIX_ARCHIVE_STAGE_ID": mapping["stage_map"]["archive"].get(
            "archive"
        ),
        "SITE_DEFECT_ARCHIVE_BITRIX_WORKING_STAGE_MAP": mapping["stage_map"]["working"],
        "SITE_DEFECT_ARCHIVE_BITRIX_FIELD_MAP": mapping["field_map"],
    }
    summary = {
        "process_title": process_type.get("title"),
        "entity_type_id": entity_type_id,
        "working_category_id": working_category.get("id"),
        "archive_category_id": archive_category.get("id"),
        "archive_stage_id": env_ready["SITE_DEFECT_ARCHIVE_BITRIX_ARCHIVE_STAGE_ID"],
        "current_webhook_user_id": current_user.get("ID"),
        "mapping_path": str(args.mapping_path),
        "fields": fields,
        "details_configuration_paths": details_paths,
        "env_ready": env_ready,
        "missing_fields": mapping.get("missing_fields"),
        "type_mismatches": mapping.get("type_mismatches"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
