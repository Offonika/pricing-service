#!/usr/bin/env python3
"""Create/update Bitrix24 smart-process for buyer receivables workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.ensure_expertise_bitrix_process as bitrix_setup  # noqa: E402

DEFAULT_MAPPING_PATH = REPO_ROOT / "build/bitrix/receivable_mapping.json"
DEFAULT_DETAILS_CONFIG_PATH = REPO_ROOT / "build/bitrix/receivable_details_configuration.json"
DEFAULT_PROCESS_TITLE = "Дебиторка покупателей"
DEFAULT_PROCESS_CODE = "receivable_buyers"
DEFAULT_CATEGORY_NAME = "Работа с дебиторкой"

STAGE_SPECS = [
    {
        "logical_key": "new_debt",
        "code": "NEW",
        "name": "Новый долг",
        "sort": 100,
        "semantics": None,
    },
    {
        "logical_key": "waiting_payment",
        "code": "WAITING",
        "name": "Ожидаем оплату",
        "sort": 200,
        "semantics": None,
    },
    {
        "logical_key": "sms_sent",
        "code": "SMS",
        "name": "SMS отправлено",
        "sort": 300,
        "semantics": None,
    },
    {
        "logical_key": "no_phone",
        "code": "NO_PHONE",
        "name": "Нет телефона",
        "sort": 350,
        "semantics": None,
    },
    {
        "logical_key": "calling",
        "code": "CALLING",
        "name": "Менеджер прозванивает",
        "sort": 400,
        "semantics": None,
    },
    {
        "logical_key": "promised_payment",
        "code": "PROMISED",
        "name": "Клиент обещал оплатить",
        "sort": 500,
        "semantics": None,
    },
    {
        "logical_key": "dispute_check",
        "code": "DISPUTE",
        "name": "Спор / проверка суммы",
        "sort": 600,
        "semantics": None,
    },
    {
        "logical_key": "escalated",
        "code": "ESCALATED",
        "name": "На эскалации",
        "sort": 700,
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

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "stable_key",
        "title": "Тех. ключ карточки",
        "type": "string",
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "counterparty_ref",
        "title": "Тех. ref контрагента 1С",
        "type": "string",
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "counterparty_name",
        "title": "Клиент",
        "type": "string",
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "current_balance",
        "title": "Сумма просрочки",
        "type": "double",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "origin_document_number",
        "title": "Номер реализации / заказа",
        "type": "string",
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "origin_document_date",
        "title": "Дата реализации",
        "type": "datetime",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "due_date",
        "title": "Срок оплаты",
        "type": "datetime",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "overdue_days",
        "title": "Дней просрочки",
        "type": "integer",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "age_days",
        "title": "Дней от реализации",
        "type": "integer",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "manager_name",
        "title": "Ответственный менеджер 1С",
        "type": "string",
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "department_ref",
        "title": "Тех. ref подразделения 1С",
        "type": "string",
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "department_name",
        "title": "Подразделение",
        "type": "string",
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "phone",
        "title": "Телефон клиента",
        "type": "string",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "phone_status",
        "title": "Статус телефона",
        "type": "enumeration",
        "enum": [
            {"xml_id": "present", "value": "Есть телефон"},
            {"xml_id": "missing", "value": "Нет телефона"},
            {"xml_id": "needs_check", "value": "Телефон требует проверки"},
        ],
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "status",
        "title": "Тех. статус backend",
        "type": "string",
        "searchable": False,
        "edit_in_list": False,
    },
    {
        "logical_key": "sms_status",
        "title": "Статус SMS",
        "type": "enumeration",
        "enum": [
            {"xml_id": "planned", "value": "Запланирована"},
            {"xml_id": "dry_run", "value": "Тестовый режим"},
            {"xml_id": "sent", "value": "Отправлена"},
            {"xml_id": "failed", "value": "Ошибка отправки"},
            {"xml_id": "skipped_no_phone", "value": "Не отправлена: нет телефона"},
        ],
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "last_sms_at",
        "title": "Дата последней SMS",
        "type": "datetime",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "needs_call_today",
        "title": "Сегодня на обзвон",
        "type": "boolean",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "promised_payment_date",
        "title": "Обещанная дата оплаты",
        "type": "datetime",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "next_action_date",
        "title": "Дата следующего действия",
        "type": "datetime",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "contact_result",
        "title": "Результат контакта",
        "type": "enumeration",
        "enum": [
            {"xml_id": "not_reached", "value": "Не дозвонился"},
            {"xml_id": "reached", "value": "Дозвонился"},
            {"xml_id": "promised_payment", "value": "Клиент обещал оплатить"},
            {"xml_id": "dispute_check", "value": "Спор / проверка суммы"},
            {"xml_id": "wrong_phone", "value": "Неверный телефон"},
            {"xml_id": "refused", "value": "Отказ от оплаты"},
        ],
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "last_contact_comment",
        "title": "Комментарий по контакту",
        "type": "text",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "escalation_level",
        "title": "Уровень эскалации",
        "type": "enumeration",
        "enum": [
            {"xml_id": "retail_network_head", "value": "Руководитель розничной сети"},
            {"xml_id": "finance", "value": "Финансовый контроль"},
            {"xml_id": "director", "value": "Директор"},
            {"xml_id": "legal", "value": "Юридический контур"},
        ],
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "chain_documents",
        "title": "Документы задолженности",
        "type": "text",
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "source",
        "title": "Источник синхронизации",
        "type": "string",
        "searchable": False,
        "edit_in_list": False,
    },
]

BUILTIN_FIELD_MAPPING = {
    "title": "TITLE",
    "assigned_by": "ASSIGNED_BY_ID",
}

DETAIL_SECTION_SPECS = [
    {
        "name": "control",
        "title": "Контроль",
        "elements": [
            "stage",
            "title",
            "assigned_by",
            "current_balance",
            "overdue_days",
            "due_date",
            "needs_call_today",
        ],
    },
    {
        "name": "client",
        "title": "Клиент",
        "elements": [
            "counterparty_name",
            "department_name",
            "phone",
            "phone_status",
            "manager_name",
        ],
    },
    {
        "name": "documents",
        "title": "Документы",
        "elements": [
            "origin_document_number",
            "origin_document_date",
            "age_days",
            "chain_documents",
        ],
    },
    {
        "name": "work",
        "title": "Работа",
        "elements": [
            "sms_status",
            "last_sms_at",
            "promised_payment_date",
            "next_action_date",
            "contact_result",
            "last_contact_comment",
            "escalation_level",
        ],
    },
]


def _field_xml_id_for_spec(spec: dict[str, Any]) -> str:
    return f"UF_CRM_RECEIVABLE_{bitrix_setup._slug_suffix(spec['logical_key'])}"


def _configure_generic_setup() -> None:
    bitrix_setup.STAGE_SPECS = STAGE_SPECS
    bitrix_setup.CUSTOM_FIELD_SPECS = CUSTOM_FIELD_SPECS
    bitrix_setup.DETAIL_SECTION_SPECS = DETAIL_SECTION_SPECS
    bitrix_setup._field_xml_id_for_spec = _field_xml_id_for_spec


def ensure_receivable_category(
    webhook_base: str,
    *,
    entity_type_id: int,
    name: str,
) -> dict[str, Any]:
    response = bitrix_setup.bitrix_call(
        webhook_base,
        "crm.category.list",
        {"entityTypeId": entity_type_id},
    )
    categories = (response.get("result") or {}).get("categories") or []
    default_category = next(
        (category for category in categories if str(category.get("isDefault") or "") == "Y"),
        None,
    )
    if default_category is None:
        return bitrix_setup.ensure_category(webhook_base, entity_type_id=entity_type_id, name=name)

    default_id = int(default_category["id"])
    fields: dict[str, Any] = {}
    if str(default_category.get("name") or "").strip() != name:
        fields["name"] = name
    if int(default_category.get("sort") or 0) != 100:
        fields["sort"] = 100
    if fields:
        bitrix_setup.bitrix_call(
            webhook_base,
            "crm.category.update",
            {"entityTypeId": entity_type_id, "id": default_id, "fields": fields},
        )
        refreshed = bitrix_setup.bitrix_call(
            webhook_base,
            "crm.category.list",
            {"entityTypeId": entity_type_id},
        )
        for category in (refreshed.get("result") or {}).get("categories") or []:
            if int(category.get("id")) == default_id:
                return category
    return default_category


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webhook-url", required=True)
    parser.add_argument("--title", default=DEFAULT_PROCESS_TITLE)
    parser.add_argument("--code", default=DEFAULT_PROCESS_CODE)
    parser.add_argument("--category-name", default=DEFAULT_CATEGORY_NAME)
    parser.add_argument("--mapping-path", default=str(DEFAULT_MAPPING_PATH))
    parser.add_argument("--details-config-path", default=str(DEFAULT_DETAILS_CONFIG_PATH))
    args = parser.parse_args()

    _configure_generic_setup()
    webhook_base = args.webhook_url.rstrip("/")
    process_type = bitrix_setup.ensure_type(webhook_base, title=args.title, code=args.code)
    entity_type_id = int(process_type["entityTypeId"])
    category = ensure_receivable_category(
        webhook_base,
        entity_type_id=entity_type_id,
        name=args.category_name,
    )
    category_id = int(category["id"])
    stages = bitrix_setup.ensure_stages(
        webhook_base,
        entity_type_id=entity_type_id,
        category_id=category_id,
    )
    custom_fields = bitrix_setup.ensure_custom_fields(
        webhook_base,
        process_type=process_type,
    )
    field_map = dict(BUILTIN_FIELD_MAPPING)
    for row in custom_fields:
        field_map[row["logical_key"]] = row["field_name"]
    enum_map = {row["logical_key"]: row["enum_map"] for row in custom_fields if row.get("enum_map")}
    stage_map = {
        key: str(value.get("STATUS_ID") or value.get("statusId") or "")
        for key, value in stages.items()
    }
    mapping = {
        "process": {
            "title": args.title,
            "entity_type_id": entity_type_id,
            "category_id": category_id,
            "stage_entity_id": bitrix_setup._status_entity_id(entity_type_id, category_id),
        },
        "stage_map": stage_map,
        "field_map": field_map,
        "enum_map": enum_map,
        "env": {
            "RECEIVABLE_BITRIX_ENTITY_TYPE_ID": entity_type_id,
            "RECEIVABLE_BITRIX_CATEGORY_ID": category_id,
            "RECEIVABLE_BITRIX_STAGE_MAP": json.dumps(stage_map, ensure_ascii=False),
            "RECEIVABLE_BITRIX_FIELD_MAP": json.dumps(field_map, ensure_ascii=False),
            "RECEIVABLE_BITRIX_ENUM_MAP": json.dumps(enum_map, ensure_ascii=False),
        },
    }
    details_mapping = {
        "process": mapping["process"],
        "fields": field_map,
    }
    details_configuration, details_config_path = bitrix_setup.ensure_common_details_configuration(
        webhook_base,
        mapping=details_mapping,
        path=Path(args.details_config_path),
    )
    mapping["details_configuration_path"] = str(details_config_path)
    mapping["details_sections"] = [
        str(item.get("title") or item.get("name") or "") for item in details_configuration
    ]
    mapping_path = Path(args.mapping_path)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(mapping, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
