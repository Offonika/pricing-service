#!/usr/bin/env python3
"""Build a local dry-run blueprint for the `Кредитное решение` smart-process.

The script is intentionally read-only for Bitrix24. It may read current process
metadata for comparison, but it never calls add/update/delete REST methods.
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

DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "build/bitrix/receivable_decision_blueprint.json"
DEFAULT_CURRENT_ENTITY_TYPE_ID = 1132
DEFAULT_CURRENT_PROCESS_TITLE = "Дебиторка покупателей"
DEFAULT_PROCESS_TITLE = "Кредитное решение"
DEFAULT_PROCESS_CODE = "receivable_decision"
DEFAULT_CATEGORY_NAME = "Работа с дебиторкой"
ARSEN_LAST_NAME = "Сагиян"
ARSEN_NAME = "Арсен"

READ_ONLY_BITRIX_METHODS = {
    "crm.type.list",
    "crm.category.list",
    "crm.status.list",
    "crm.item.list",
    "crm.item.fields",
    "department.get",
    "user.search",
}

STAGE_SPECS = [
    {
        "logical_key": "new_debt",
        "code": "NEW",
        "name": "Новый долг",
        "sort": 100,
        "semantics": None,
    },
    {
        "logical_key": "prepare_negotiation",
        "code": "PREPARE",
        "name": "Подготовить переговоры",
        "sort": 200,
        "semantics": None,
    },
    {
        "logical_key": "negotiation",
        "code": "NEGOTIATION",
        "name": "В переговорах",
        "sort": 300,
        "semantics": None,
    },
    {
        "logical_key": "payment_schedule",
        "code": "SCHEDULE",
        "name": "График оплаты",
        "sort": 400,
        "semantics": None,
    },
    {
        "logical_key": "waiting_payment",
        "code": "WAITING",
        "name": "Ждем оплату",
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
        "logical_key": "shipment_stop",
        "code": "SHIPMENT_STOP",
        "name": "Стоп отгрузка",
        "sort": 700,
        "semantics": None,
    },
    {
        "logical_key": "escalated",
        "code": "ESCALATED",
        "name": "На эскалации",
        "sort": 800,
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

PAYMENT_BEHAVIOR_ENUM = [
    {"xml_id": "weekly_batch_payer", "value": "Частые заказы, недельная оплата пачкой"},
    {"xml_id": "regular_term_payer", "value": "Стабильно платит около срока"},
    {"xml_id": "partial_but_alive", "value": "Платит частями, долг не разгоняется"},
    {"xml_id": "growing_debt_second_week", "value": "Долг растет вторую неделю"},
    {"xml_id": "chronic_non_payer", "value": "Злостный неплательщик"},
    {"xml_id": "promise_breaker", "value": "Срывает обещания оплаты"},
    {"xml_id": "silent_no_contact", "value": "Нет связи"},
    {"xml_id": "dispute_quality", "value": "Спор / брак / проверка суммы"},
    {"xml_id": "new_no_history", "value": "Новый, мало истории"},
]

RECOMMENDED_DECISION_ENUM = [
    {"xml_id": "soft_work", "value": "Работать мягко"},
    {"xml_id": "strict_control", "value": "Жесткий контроль"},
    {"xml_id": "shipment_stop", "value": "Стоп отгрузка"},
    {"xml_id": "verify_amount", "value": "Проверить сумму"},
    {"xml_id": "escalate", "value": "Эскалация"},
    {"xml_id": "close", "value": "Закрыть"},
]

ADVISOR_TONE_ENUM = [
    {"xml_id": "partner_soft", "value": "Партнерски и мягко"},
    {"xml_id": "calm_strict", "value": "Спокойно, но жестко"},
    {"xml_id": "fact_check", "value": "Сбор фактов без давления"},
    {"xml_id": "final_warning", "value": "Финальное предупреждение"},
]

SETTLEMENT_STATUS_ENUM = [
    {"xml_id": "none", "value": "Нет запроса"},
    {"xml_id": "requested", "value": "Запрошено решение"},
    {"xml_id": "approved", "value": "Согласовано"},
    {"xml_id": "rejected", "value": "Отклонено"},
    {"xml_id": "finance_required", "value": "Нужно финподтверждение"},
]

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "stable_key",
        "title": "Тех. ключ карточки",
        "type": "string",
        "edit_in_list": False,
    },
    {
        "logical_key": "counterparty_ref",
        "title": "Тех. ref контрагента 1С",
        "type": "string",
        "edit_in_list": False,
    },
    {"logical_key": "counterparty_code", "title": "Код 1С", "type": "string", "edit_in_list": True},
    {"logical_key": "counterparty_name", "title": "Клиент", "type": "string", "edit_in_list": True},
    {"logical_key": "onec_folder", "title": "Папка 1С", "type": "string", "edit_in_list": True},
    {
        "logical_key": "department_ref",
        "title": "Тех. ref подразделения 1С",
        "type": "string",
        "edit_in_list": False,
    },
    {
        "logical_key": "department_name",
        "title": "Подразделение",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "manager_name",
        "title": "Ответственный менеджер 1С",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "current_balance",
        "title": "Текущий долг",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "overdue_amount",
        "title": "Просроченная сумма",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "overdue_days",
        "title": "Дней просрочки",
        "type": "integer",
        "edit_in_list": True,
    },
    {
        "logical_key": "oldest_overdue_date",
        "title": "Самая старая просрочка",
        "type": "datetime",
        "edit_in_list": True,
    },
    {"logical_key": "due_date", "title": "Срок оплаты", "type": "datetime", "edit_in_list": True},
    {
        "logical_key": "chain_documents",
        "title": "Документы задолженности",
        "type": "text",
        "edit_in_list": True,
    },
    {"logical_key": "sales_30", "title": "Продажи 30 дней", "type": "double", "edit_in_list": True},
    {"logical_key": "sales_60", "title": "Продажи 60 дней", "type": "double", "edit_in_list": True},
    {"logical_key": "sales_90", "title": "Продажи 90 дней", "type": "double", "edit_in_list": True},
    {
        "logical_key": "avg_sales_30",
        "title": "Средняя продажа/день 30",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "avg_sales_60",
        "title": "Средняя продажа/день 60",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "avg_sales_90",
        "title": "Средняя продажа/день 90",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "gross_profit_30",
        "title": "Валовая прибыль 30 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "gross_profit_60",
        "title": "Валовая прибыль 60 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "gross_profit_90",
        "title": "Валовая прибыль 90 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "gross_margin_pct_90",
        "title": "Маржа 90 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "profitability_pct_90",
        "title": "Рентабельность 90 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "defect_return_amount_90",
        "title": "Возвраты брак/качество 90 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "trend_coefficient",
        "title": "Коэффициент тенденции",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "payment_behavior_group",
        "title": "Тип оплаты клиента",
        "type": "enumeration",
        "enum": PAYMENT_BEHAVIOR_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "median_payment_lag_days",
        "title": "Медианный лаг оплаты, дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "payment_frequency_days",
        "title": "Частота оплат, дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "last_payment_at",
        "title": "Последняя оплата",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "last_payment_amount",
        "title": "Сумма последней оплаты",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "debt_to_sales_90_ratio",
        "title": "Долг / продажи 90 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "broken_promises_count",
        "title": "Сорвано обещаний",
        "type": "integer",
        "edit_in_list": True,
    },
    {
        "logical_key": "recommended_decision",
        "title": "Решение",
        "type": "enumeration",
        "enum": RECOMMENDED_DECISION_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "advisor_summary",
        "title": "Советник: картина",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "advisor_goal",
        "title": "Советник: цель разговора",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "advisor_tone",
        "title": "Советник: тон",
        "type": "enumeration",
        "enum": ADVISOR_TONE_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "advisor_script",
        "title": "Советник: скрипт",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "advisor_forbidden_promises",
        "title": "Советник: что нельзя обещать",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "recommended_first_payment_pct",
        "title": "Рекомендуемый первый платеж, %",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "recommended_first_payment_amount",
        "title": "Рекомендуемый первый платеж",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "recommended_payment_window_days",
        "title": "Рекомендуемый срок графика, дней",
        "type": "integer",
        "edit_in_list": True,
    },
    {
        "logical_key": "escalation_condition",
        "title": "Условие эскалации",
        "type": "text",
        "edit_in_list": True,
    },
    {"logical_key": "phone", "title": "Телефон клиента", "type": "string", "edit_in_list": True},
    {
        "logical_key": "phone_status",
        "title": "Статус телефона",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "last_contact_at",
        "title": "Последний контакт",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "contact_result",
        "title": "Результат контакта",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "promised_payment_date",
        "title": "Обещанная дата оплаты",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "next_action_date",
        "title": "Дата следующего действия",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "last_contact_comment",
        "title": "Комментарий по контакту",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "call_attempts_30",
        "title": "Попытки связи 30 дней",
        "type": "integer",
        "edit_in_list": True,
    },
    {
        "logical_key": "successful_contacts_30",
        "title": "Успешные контакты 30 дней",
        "type": "integer",
        "edit_in_list": True,
    },
    {
        "logical_key": "last_call_at",
        "title": "Последний звонок",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "last_sms_at",
        "title": "Последняя SMS",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "settlement_request",
        "title": "Запрос конфликтного решения",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "settlement_status",
        "title": "Статус конфликтного решения",
        "type": "enumeration",
        "enum": SETTLEMENT_STATUS_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "source",
        "title": "Источник синхронизации",
        "type": "string",
        "edit_in_list": False,
    },
]

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
            "recommended_decision",
            "trend_coefficient",
            "next_action_date",
        ],
    },
    {
        "name": "client",
        "title": "Клиент",
        "elements": [
            "counterparty_code",
            "counterparty_name",
            "onec_folder",
            "department_name",
            "manager_name",
            "phone",
            "phone_status",
        ],
    },
    {
        "name": "portrait",
        "title": "Портрет",
        "elements": [
            "sales_30",
            "sales_60",
            "sales_90",
            "gross_profit_90",
            "gross_margin_pct_90",
            "profitability_pct_90",
            "defect_return_amount_90",
            "payment_behavior_group",
            "debt_to_sales_90_ratio",
        ],
    },
    {
        "name": "advisor",
        "title": "Советник переговорщика",
        "elements": [
            "advisor_summary",
            "advisor_goal",
            "advisor_tone",
            "advisor_script",
            "recommended_first_payment_pct",
            "recommended_payment_window_days",
            "advisor_forbidden_promises",
            "escalation_condition",
        ],
    },
    {
        "name": "work",
        "title": "Работа",
        "elements": [
            "last_contact_at",
            "contact_result",
            "promised_payment_date",
            "last_contact_comment",
            "call_attempts_30",
            "successful_contacts_30",
            "settlement_request",
            "settlement_status",
        ],
    },
    {
        "name": "documents",
        "title": "Документы",
        "elements": ["due_date", "oldest_overdue_date", "chain_documents"],
    },
]

WORKLIST_RULES = [
    {
        "key": "next_action_due",
        "title": "Сегодня следующий шаг",
        "condition": "next_action_date <= today",
    },
    {
        "key": "promise_overdue",
        "title": "Обещал оплатить и не оплатил",
        "condition": "promised_payment_date < today and current_balance > 0",
    },
    {
        "key": "growing_debt_second_week",
        "title": "Долг растет вторую неделю",
        "condition": "payment_behavior_group == growing_debt_second_week",
    },
    {
        "key": "chronic_non_payer",
        "title": "Злостный неплательщик",
        "condition": "payment_behavior_group == chronic_non_payer",
    },
    {
        "key": "no_contact",
        "title": "Нет связи",
        "condition": "phone_status != present or payment_behavior_group == silent_no_contact",
    },
    {
        "key": "high_profit_at_risk",
        "title": "Прибыльный клиент с риском",
        "condition": "gross_profit_90 > 0 and overdue_days > 7 and trend_coefficient >= 1",
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


def _field_xml_id(logical_key: str) -> str:
    suffix = re.sub(r"[^A-Z0-9_]+", "_", logical_key.upper()).strip("_")
    return f"UF_CRM_RECEIVABLE_DECISION_{suffix}"


def _field_blueprint(spec: dict[str, Any]) -> dict[str, Any]:
    row = dict(spec)
    row["xml_id"] = _field_xml_id(str(spec["logical_key"]))
    row.setdefault("searchable", False)
    row.setdefault("edit_in_list", True)
    row.setdefault("required", False)
    return row


def build_blueprint(*, live_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    fields = [_field_blueprint(spec) for spec in CUSTOM_FIELD_SPECS]
    visible_fields = {
        logical_key
        for section in DETAIL_SECTION_SPECS
        for logical_key in section.get("elements", [])
    }
    hidden_technical_fields = [
        field["logical_key"]
        for field in fields
        if not field.get("edit_in_list") or field["logical_key"] not in visible_fields
    ]
    return {
        "mode": "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "bitrix_writes": False,
            "allowed_bitrix_methods": sorted(READ_ONLY_BITRIX_METHODS),
            "requires_owner_presence_for_apply": True,
        },
        "process": {
            "title": DEFAULT_PROCESS_TITLE,
            "code": DEFAULT_PROCESS_CODE,
            "category_name": DEFAULT_CATEGORY_NAME,
            "current_process_kept_as_history": DEFAULT_CURRENT_PROCESS_TITLE,
        },
        "pilot": {
            "owner_name": "Арсен Сагиян",
            "owner_role": "Руководитель сети торговых точек",
            "department_resolution": "read-only before pilot",
            "onec_folder_filter": "Покупатели",
        },
        "stages": STAGE_SPECS,
        "fields": fields,
        "detail_sections": DETAIL_SECTION_SPECS,
        "hidden_technical_fields": hidden_technical_fields,
        "worklist_rules": WORKLIST_RULES,
        "payment_behavior_groups": PAYMENT_BEHAVIOR_ENUM,
        "advisor_rules": {
            "first_payment_pct_range": [20, 35],
            "payment_window_days_range": [7, 10],
            "base_inputs": [
                "current_balance",
                "overdue_days",
                "sales_30",
                "sales_60",
                "sales_90",
                "gross_profit_90",
                "gross_margin_pct_90",
                "profitability_pct_90",
                "trend_coefficient",
                "payment_behavior_group",
                "broken_promises_count",
            ],
            "llm_policy": "rules first; LLM may only phrase an already selected recommendation",
        },
        "access_rules": {
            "sales_managers": "own buyers folder customers only",
            "senior_sales": "expanded view by rank/department",
            "store_senior": "can create settlement_request within approved limit",
            "other_onec_folders": "owner approval required",
        },
        "live_snapshot": live_snapshot or {"status": "not_requested"},
    }


def _safe_bitrix_call(webhook_base: str, method: str, params: dict[str, Any]) -> Any:
    if method not in READ_ONLY_BITRIX_METHODS:
        raise RuntimeError(f"Refusing non-read-only Bitrix method: {method}")
    return bitrix_setup.bitrix_call(webhook_base, method, params).get("result")


def build_live_snapshot(webhook_base: str, *, current_entity_type_id: int) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "status": "ready",
        "current_entity_type_id": current_entity_type_id,
        "write_methods_used": [],
    }
    try:
        types_result = _safe_bitrix_call(webhook_base, "crm.type.list", {})
        types = types_result.get("types") if isinstance(types_result, dict) else []
        snapshot["current_process"] = next(
            (
                {
                    "id": item.get("id"),
                    "entityTypeId": item.get("entityTypeId"),
                    "title": item.get("title"),
                    "code": item.get("code"),
                }
                for item in types or []
                if int(item.get("entityTypeId") or 0) == current_entity_type_id
            ),
            None,
        )
        snapshot["target_title_collision"] = [
            {
                "id": item.get("id"),
                "entityTypeId": item.get("entityTypeId"),
                "title": item.get("title"),
                "code": item.get("code"),
            }
            for item in types or []
            if str(item.get("title") or "").strip() == DEFAULT_PROCESS_TITLE
        ]
    except Exception as exc:  # pragma: no cover - exercised through CLI/reporting
        snapshot["type_error"] = str(exc)

    try:
        categories_result = _safe_bitrix_call(
            webhook_base,
            "crm.category.list",
            {"entityTypeId": current_entity_type_id},
        )
        categories = (
            categories_result.get("categories") if isinstance(categories_result, dict) else []
        )
        snapshot["current_categories"] = categories or []
        stages_by_category: dict[str, list[dict[str, Any]]] = {}
        for category in categories or []:
            category_id = int(category.get("id"))
            stages = _safe_bitrix_call(
                webhook_base,
                "crm.status.list",
                {
                    "filter[ENTITY_ID]": f"DYNAMIC_{current_entity_type_id}_STAGE_{category_id}",
                    "order[SORT]": "ASC",
                },
            )
            stages_by_category[str(category_id)] = [
                {
                    "STATUS_ID": item.get("STATUS_ID"),
                    "NAME": item.get("NAME"),
                    "SORT": item.get("SORT"),
                    "SEMANTICS": item.get("SEMANTICS"),
                }
                for item in stages or []
            ]
        snapshot["current_stages"] = stages_by_category
    except Exception as exc:  # pragma: no cover
        snapshot["category_stage_error"] = str(exc)

    try:
        fields_result = _safe_bitrix_call(
            webhook_base,
            "crm.item.fields",
            {"entityTypeId": current_entity_type_id},
        )
        fields = fields_result.get("fields") if isinstance(fields_result, dict) else {}
        snapshot["current_field_count"] = len(fields or {})
        snapshot["current_user_fields"] = [
            {
                "name": name,
                "title": meta.get("title") or meta.get("formLabel") or name,
                "type": meta.get("type"),
            }
            for name, meta in sorted((fields or {}).items())
            if name.startswith("ufCrm")
        ]
    except Exception as exc:  # pragma: no cover
        snapshot["field_error"] = str(exc)

    try:
        items_result = _safe_bitrix_call(
            webhook_base,
            "crm.item.list",
            {
                "entityTypeId": current_entity_type_id,
                "select[0]": "id",
                "select[1]": "title",
                "select[2]": "stageId",
                "select[3]": "assignedById",
                "order[id]": "ASC",
                "start": 0,
            },
        )
        items = items_result.get("items") if isinstance(items_result, dict) else []
        snapshot["current_item_count"] = len(items or [])
        snapshot["current_item_sample"] = items or []
    except Exception as exc:  # pragma: no cover
        snapshot["item_error"] = str(exc)

    try:
        user_result = _safe_bitrix_call(
            webhook_base,
            "user.search",
            {"FILTER[NAME]": ARSEN_NAME, "FILTER[LAST_NAME]": ARSEN_LAST_NAME},
        )
        users = user_result if isinstance(user_result, list) else []
        snapshot["arsen_candidates"] = [
            {
                "ID": user.get("ID"),
                "NAME": user.get("NAME"),
                "LAST_NAME": user.get("LAST_NAME"),
                "SECOND_NAME": user.get("SECOND_NAME"),
                "WORK_POSITION": user.get("WORK_POSITION"),
                "UF_DEPARTMENT": user.get("UF_DEPARTMENT"),
                "ACTIVE": user.get("ACTIVE"),
            }
            for user in users
        ]
        department_ids = sorted(
            {
                int(department_id)
                for user in users
                for department_id in (user.get("UF_DEPARTMENT") or [])
                if str(department_id).isdigit()
            }
        )
        departments: list[dict[str, Any]] = []
        for department_id in department_ids:
            department_result = _safe_bitrix_call(
                webhook_base,
                "department.get",
                {"ID": department_id},
            )
            if isinstance(department_result, list):
                departments.extend(department_result)
        snapshot["arsen_departments"] = [
            {
                "ID": item.get("ID"),
                "NAME": item.get("NAME"),
                "PARENT": item.get("PARENT"),
                "UF_HEAD": item.get("UF_HEAD"),
            }
            for item in departments
        ]
    except Exception as exc:  # pragma: no cover
        snapshot["arsen_error"] = str(exc)
    return snapshot


def resolve_webhook(args: argparse.Namespace) -> str:
    if args.webhook_url:
        return args.webhook_url.strip()
    env = load_env(args.env_file)
    return (
        env.get("RECEIVABLE_BITRIX_WEBHOOK_URL") or env.get("BITRIX_BOX_WEBHOOK_BASE") or ""
    ).strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--current-entity-type-id", type=int, default=DEFAULT_CURRENT_ENTITY_TYPE_ID
    )
    parser.add_argument(
        "--bitrix-readonly",
        action="store_true",
        help="Read current Bitrix metadata for comparison. Never writes to Bitrix.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    live_snapshot: dict[str, Any] | None = None
    if args.bitrix_readonly:
        webhook = resolve_webhook(args)
        if webhook:
            live_snapshot = build_live_snapshot(
                webhook,
                current_entity_type_id=args.current_entity_type_id,
            )
        else:
            live_snapshot = {"status": "skipped", "reason": "webhook_not_configured"}

    blueprint = build_blueprint(live_snapshot=live_snapshot)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(
        json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(blueprint, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
