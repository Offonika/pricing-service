#!/usr/bin/env python3
"""Build a local dry-run blueprint for the `Типы Цен` smart-process.

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

import yaml  # noqa: E402

import scripts.ensure_expertise_bitrix_process as bitrix_setup  # noqa: E402

DEFAULT_ENV_FILE = REPO_ROOT / ".env"

# Единый источник истины нормативов: config/price_types/ruleset.yaml.
RULESET_PATH = REPO_ROOT / "config/price_types/ruleset.yaml"
RULESET: dict[str, Any] = yaml.safe_load(RULESET_PATH.read_text(encoding="utf-8"))
LEVELS: dict[str, Any] = RULESET["levels"]
LEVEL_THRESHOLDS_NOTE = "; ".join(
    f"{key} {val['retention_norm_3m']}/{val['hold_last_month']}" for key, val in LEVELS.items()
)
RULESET_TAG = f"ruleset {RULESET['ruleset_version']}"

# Поля, без которых карточка смарт-процесса не имеет смысла.
REQUIRED_FIELD_KEYS = {
    "stable_key",
    "counterparty_ref",
    "counterparty_code",
    "counterparty_name",
    "current_price_type",
    "snapshot_date",
    "three_month_sales_total",
    "last_full_month_sales",
    "final_decision",
    "automation_level",
    "approval_status",
    "onec_export_status",
}
DEFAULT_OUTPUT_PATH = REPO_ROOT / "build/bitrix/customer_price_type_blueprint.json"
DEFAULT_CURRENT_ENTITY_TYPE_ID = 0
DEFAULT_PROCESS_TITLE = "Типы Цен"
DEFAULT_PROCESS_CODE = "customer_price_type"
DEFAULT_CATEGORY_NAME = "Управление типами цен"
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
        "logical_key": "new_snapshot",
        "code": "NEW_SNAPSHOT",
        "name": "Новый срез",
        "sort": 100,
        "semantics": None,
    },
    {
        "logical_key": "preclose_signal",
        "code": "PRECLOSE_SIGNAL",
        "name": "Предсигнал удержания",
        "sort": 200,
        "semantics": None,
    },
    {
        "logical_key": "retention_work",
        "code": "RETENTION_WORK",
        "name": "На удержании",
        "sort": 300,
        "semantics": None,
    },
    {
        "logical_key": "isolate_1m",
        "code": "ISOLATE_1M",
        "name": "Изолятор 1 месяц",
        "sort": 400,
        "semantics": None,
    },
    {
        "logical_key": "recovery_control",
        "code": "RECOVERY_CONTROL",
        "name": "Реанимация / восстановление",
        "sort": 500,
        "semantics": None,
    },
    {
        "logical_key": "quality_check",
        "code": "QUALITY_CHECK",
        "name": "Проверка качества",
        "sort": 600,
        "semantics": None,
    },
    {
        "logical_key": "credit_check",
        "code": "CREDIT_ECONOMICS_CHECK",
        "name": "Проверка кредита и экономики",
        "sort": 700,
        "semantics": None,
    },
    {
        "logical_key": "data_check",
        "code": "DATA_CHECK",
        "name": "Сверка данных",
        "sort": 800,
        "semantics": None,
    },
    {
        "logical_key": "manual_upgrade_approval",
        "code": "UPGRADE_APPROVAL",
        "name": "Подтверждение улучшения",
        "sort": 900,
        "semantics": None,
    },
    {
        "logical_key": "manual_downgrade_approval",
        "code": "DOWNGRADE_APPROVAL",
        "name": "Подтверждение понижения",
        "sort": 1000,
        "semantics": None,
    },
    {
        "logical_key": "ready_for_1c",
        "code": "READY_FOR_1C",
        "name": "Готово к выгрузке в 1С",
        "sort": 1100,
        "semantics": None,
    },
    {
        "logical_key": "closed_keep_current",
        "code": "CLOSED_KEEP",
        "name": "Закрыто без смены",
        "sort": 1200,
        "semantics": "S",
    },
    {
        "logical_key": "closed_changed",
        "code": "CLOSED_CHANGED",
        "name": "Закрыто со сменой",
        "sort": 1300,
        "semantics": "S",
    },
]

PRICE_TYPE_ENUM = [
    {"xml_id": "retail", "value": "Розница"},
    {"xml_id": "bronze", "value": "2.Бронзовый"},
    {"xml_id": "silver", "value": "3.Серебряный"},
    {"xml_id": "gold", "value": "4.Золотой"},
    {"xml_id": "platinum", "value": "5.Платиновый"},
    {"xml_id": "key_account", "value": "Key Account / ручная экономика"},
]

CHANGE_DIRECTION_ENUM = [
    {"xml_id": "no_change", "value": "Без смены"},
    {"xml_id": "upgrade", "value": "Улучшение"},
    {"xml_id": "downgrade", "value": "Понижение"},
    {"xml_id": "manual_economics", "value": "Ручная экономика"},
]

AUTOMATION_LEVEL_ENUM = [
    {"xml_id": "info_only", "value": "Только информация"},
    {"xml_id": "pre_signal", "value": "Только предсигнал"},
    {"xml_id": "manager_action_required", "value": "Нужно действие менеджера"},
    {"xml_id": "manual_review", "value": "Нужна ручная проверка"},
    {"xml_id": "manual_approve", "value": "Нужно ручное подтверждение"},
    {"xml_id": "ready_for_1c", "value": "Готово к выгрузке в 1С"},
    {"xml_id": "exported_to_1c", "value": "Уже выгружено в 1С"},
]

APPROVAL_STATUS_ENUM = [
    {"xml_id": "not_required", "value": "Не требуется"},
    {"xml_id": "pending", "value": "Ждет подтверждения"},
    {"xml_id": "approved", "value": "Подтверждено"},
    {"xml_id": "rejected", "value": "Отклонено"},
]

PAYMENT_BEHAVIOR_ENUM = [
    {"xml_id": "no_current_debt", "value": "Текущего долга нет"},
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

CREDIT_DISCIPLINE_ENUM = [
    {"xml_id": "A", "value": "Отличная дисциплина"},
    {"xml_id": "B", "value": "Нормальная дисциплина"},
    {"xml_id": "C", "value": "Осторожно"},
    {"xml_id": "D", "value": "Риск"},
    {"xml_id": "E", "value": "Стоп / предоплата"},
]

RECOMMENDED_DECISION_ENUM = [
    {"xml_id": "soft_work", "value": "Работать мягко"},
    {"xml_id": "strict_control", "value": "Жесткий контроль"},
    {"xml_id": "shipment_stop", "value": "Стоп отгрузка"},
    {"xml_id": "verify_amount", "value": "Проверить сумму"},
]

ECONOMICS_STATUS_ENUM = [
    {"xml_id": "ok", "value": "Экономика нормальная"},
    {"xml_id": "stop", "value": "Экономика стоп"},
    {"xml_id": "weak", "value": "Экономика слабая"},
    {"xml_id": "strong", "value": "Экономика сильная"},
]

HISTORY_BUCKET_ENUM = [
    {"xml_id": "normal_flow", "value": "Обычный поток"},
    {"xml_id": "comeback", "value": "Вернулся после паузы"},
    {"xml_id": "historical_b2b", "value": "Давний B2B"},
    {"xml_id": "rehab_economics", "value": "Реанимация по экономике"},
    {"xml_id": "duplicate_check", "value": "Подозрение на дубль"},
]

RETURN_BEHAVIOR_ENUM = [
    {"xml_id": "no_returns", "value": "Возвратов нет"},
    {"xml_id": "low_history", "value": "Мало истории"},
    {"xml_id": "healthy_returns", "value": "Норма"},
    {"xml_id": "watch_returns", "value": "Наблюдение"},
    {"xml_id": "elevated_returns", "value": "Повышенный риск"},
    {"xml_id": "critical_returns", "value": "Критический риск"},
    {"xml_id": "dispute_quality", "value": "Спор / качество / проверка"},
]

RETURN_QUALITY_GRADE_ENUM = [
    {"xml_id": "A", "value": "Возвраты в норме"},
    {"xml_id": "B", "value": "Наблюдение"},
    {"xml_id": "C", "value": "Осторожно"},
    {"xml_id": "D", "value": "Риск"},
    {"xml_id": "E", "value": "Критично"},
]

MANAGER_ACTION_LOG_STATUS_ENUM = [
    {"xml_id": "none", "value": "Нет фиксации"},
    {"xml_id": "partial", "value": "Частичная фиксация"},
    {"xml_id": "full", "value": "Полная фиксация"},
]

SOURCE_STATUS_ENUM = [
    {"xml_id": "ready", "value": "Данные готовы"},
    {"xml_id": "partial", "value": "Данные частично неполные"},
    {"xml_id": "conflict", "value": "Есть противоречие в данных"},
]

FINAL_DECISION_ENUM = [
    {"xml_id": "keep_current", "value": "Оставить текущий тип цен"},
    {"xml_id": "retention_work", "value": "Удержание"},
    {"xml_id": "isolate_1m", "value": "Изолятор 1 месяц"},
    {"xml_id": "recovery_control", "value": "Реанимация / восстановление"},
    {"xml_id": "quality_block", "value": "Блок по качеству"},
    {"xml_id": "credit_block", "value": "Блок по платежам / кредиту"},
    {"xml_id": "upgrade_proposed", "value": "Предложить улучшение"},
    {"xml_id": "downgrade_proposed", "value": "Предложить понижение"},
    {"xml_id": "changed_in_1c", "value": "Изменено в 1С"},
]

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "stable_key",
        "title": "Тех. ключ карточки",
        "type": "string",
        "edit_in_list": False,
        "formula": RULESET["identity"]["stable_key_formula"],
    },
    {
        "logical_key": "snapshot_date",
        "title": "Дата среза расчета",
        "type": "date",
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
        "logical_key": "department_name",
        "title": "Подразделение",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "manager_name",
        "title": "Ответственный менеджер",
        "type": "string",
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
        "logical_key": "current_price_type",
        "title": "Текущий тип цен",
        "type": "enumeration",
        "enum": PRICE_TYPE_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "target_price_type_candidate",
        "title": "Кандидат по лестнице",
        "type": "enumeration",
        "enum": PRICE_TYPE_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "approved_target_price_type",
        "title": "Утвержденный целевой тип цен",
        "type": "enumeration",
        "enum": PRICE_TYPE_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "price_type_change_direction",
        "title": "Направление смены",
        "type": "enumeration",
        "enum": CHANGE_DIRECTION_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "ladder_rule_name",
        "title": "Правило лестницы",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "three_month_sales_total",
        "title": "Итог продаж 3 месяца",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "last_full_month_sales",
        "title": "Продажи последнего полного месяца",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "threshold_lower",
        "title": "Нижний порог ступени",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "threshold_upper",
        "title": "Верхний порог ступени",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "preclose_risk_flag",
        "title": "Был предсигнал",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "revenue_90",
        "title": "Выручка 90 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "cost_of_sales_90",
        "title": "Себестоимость 90 дней",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "gross_profit_90",
        "title": "Валовая 90 дней",
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
        "logical_key": "economics_status",
        "title": "Статус экономики",
        "type": "enumeration",
        "enum": ECONOMICS_STATUS_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "economics_bucket",
        "title": "Корзина экономики",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "economics_note",
        "title": "Комментарий по экономике",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "payment_behavior_group",
        "title": "Поведение оплаты",
        "type": "enumeration",
        "enum": PAYMENT_BEHAVIOR_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "payment_behavior_label",
        "title": "Расшифровка поведения оплаты",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "credit_discipline_grade",
        "title": "Grade платежной дисциплины",
        "type": "enumeration",
        "enum": CREDIT_DISCIPLINE_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "credit_discipline_coefficient",
        "title": "Коэффициент доверия",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "recommended_credit_limit",
        "title": "Рекомендованный кредитный лимит",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "over_limit_amount",
        "title": "Выше лимита на сумму",
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
        "logical_key": "recommended_first_payment_pct",
        "title": "Рекомендуемый первый платеж, %",
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
        "logical_key": "recommended_decision",
        "title": "Системная рекомендация",
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
        "logical_key": "return_amount_rate_90_pct",
        "title": "% возврата по сумме",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "return_qty_rate_90_pct",
        "title": "% возврата по шт",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "defect_return_amount_rate_90_pct",
        "title": "% брака / качества по сумме",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "defect_return_qty_90",
        "title": "Возврат брак, шт",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "new_return_qty_90",
        "title": "Возврат новый, шт",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "return_behavior_group",
        "title": "Статус возвратов",
        "type": "enumeration",
        "enum": RETURN_BEHAVIOR_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "return_quality_grade",
        "title": "Grade возвратов",
        "type": "enumeration",
        "enum": RETURN_QUALITY_GRADE_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "return_quality_score",
        "title": "Риск по возвратам",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "return_source_status",
        "title": "Источник возвратов",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "history_bucket",
        "title": "Историческая корзина",
        "type": "enumeration",
        "enum": HISTORY_BUCKET_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "last_positive_price_type",
        "title": "Последняя положительная ступень",
        "type": "enumeration",
        "enum": PRICE_TYPE_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "comeback_full_month_no",
        "title": "Полный месяц после возврата",
        "type": "integer",
        "edit_in_list": True,
    },
    {
        "logical_key": "historical_b2b_flag",
        "title": "Давний B2B",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "duplicate_flag",
        "title": "Подозрение на дубль",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "data_conflict_flag",
        "title": "Противоречие в данных",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "manual_exception_reason",
        "title": "Причина ручного исключения",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "crm_recovery_required",
        "title": "Нужна CRM-реанимация",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "isolate_started_at",
        "title": "Дата входа в изолятор",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "isolate_deadline_at",
        "title": "Дата выхода из изолятора",
        "type": "datetime",
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
        "logical_key": "promised_purchase_date",
        "title": "Обещанная дата закупки",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "promised_purchase_amount",
        "title": "Обещанная сумма закупки",
        "type": "double",
        "edit_in_list": True,
    },
    {
        "logical_key": "next_action_date",
        "title": "Дата следующего действия",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "manager_action_log_status",
        "title": "Статус фиксации действий",
        "type": "enumeration",
        "enum": MANAGER_ACTION_LOG_STATUS_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "manager_action_comment",
        "title": "Комментарий по работе",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "source_status",
        "title": "Статус источника",
        "type": "enumeration",
        "enum": SOURCE_STATUS_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "source_notes",
        "title": "Примечания по источнику",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "sales_source_status",
        "title": "Источник продаж",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "economics_source_status",
        "title": "Источник экономики",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "credit_source_status",
        "title": "Источник кредита",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "quality_source_status",
        "title": "Источник качества",
        "type": "string",
        "edit_in_list": True,
    },
    {
        "logical_key": "final_decision",
        "title": "Итоговое решение",
        "type": "enumeration",
        "enum": FINAL_DECISION_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "automation_level",
        "title": "Уровень автоматизации",
        "type": "enumeration",
        "enum": AUTOMATION_LEVEL_ENUM,
        "edit_in_list": True,
    },
    {
        "logical_key": "approval_status",
        "title": "Статус подтверждения",
        "type": "enumeration",
        "enum": APPROVAL_STATUS_ENUM,
        "edit_in_list": True,
    },
    {"logical_key": "approved_by", "title": "Кто утвердил", "type": "string", "edit_in_list": True},
    {
        "logical_key": "approved_at",
        "title": "Когда утвердили",
        "type": "datetime",
        "edit_in_list": True,
    },
    {
        "logical_key": "decision_reason_short",
        "title": "Короткая причина решения",
        "type": "text",
        "edit_in_list": True,
    },
    {
        "logical_key": "onec_export_status",
        "title": "Статус выгрузки в 1С",
        "type": "string",
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
            "snapshot_date",
            "assigned_by",
            "final_decision",
            "automation_level",
            "approval_status",
            "next_action_date",
            "onec_export_status",
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
        "name": "price_type",
        "title": "Тип цен",
        "elements": [
            "current_price_type",
            "target_price_type_candidate",
            "price_type_change_direction",
            "ladder_rule_name",
            "three_month_sales_total",
            "last_full_month_sales",
            "threshold_lower",
            "threshold_upper",
        ],
    },
    {
        "name": "economics",
        "title": "Экономика",
        "elements": [
            "revenue_90",
            "cost_of_sales_90",
            "gross_profit_90",
            "gross_margin_pct_90",
            "profitability_pct_90",
            "economics_status",
            "economics_bucket",
        ],
    },
    {
        "name": "credit_portrait",
        "title": "Платежи и кредит",
        "elements": [
            "payment_behavior_group",
            "credit_discipline_grade",
            "recommended_credit_limit",
            "over_limit_amount",
            "recommended_first_payment_amount",
            "recommended_payment_window_days",
            "recommended_decision",
            "advisor_summary",
        ],
    },
    {
        "name": "quality",
        "title": "Возвраты и качество",
        "elements": [
            "return_amount_rate_90_pct",
            "return_qty_rate_90_pct",
            "defect_return_amount_rate_90_pct",
            "defect_return_qty_90",
            "new_return_qty_90",
            "return_behavior_group",
            "return_quality_grade",
            "return_quality_score",
        ],
    },
    {
        "name": "history",
        "title": "История и исключения",
        "elements": [
            "history_bucket",
            "last_positive_price_type",
            "comeback_full_month_no",
            "historical_b2b_flag",
            "duplicate_flag",
            "manual_exception_reason",
        ],
    },
    {
        "name": "work",
        "title": "Работа",
        "elements": [
            "crm_recovery_required",
            "isolate_started_at",
            "isolate_deadline_at",
            "last_contact_at",
            "contact_result",
            "promised_purchase_date",
            "promised_purchase_amount",
            "next_action_date",
            "manager_action_log_status",
            "manager_action_comment",
        ],
    },
    {
        "name": "source",
        "title": "Доверие к данным",
        "elements": [
            "source_status",
            "source_notes",
            "sales_source_status",
            "economics_source_status",
            "credit_source_status",
            "quality_source_status",
        ],
    },
]

WORKLIST_RULES = [
    {
        "key": "preclose_risk",
        "title": "Предсигнал до конца месяца",
        "condition": "preclose_risk_flag == yes",
    },
    {
        "key": "retention_now",
        "title": "Срочное удержание",
        "condition": "stage == retention_work",
    },
    {
        "key": "isolate_now",
        "title": "Клиенты в изоляторе",
        "condition": "stage == isolate_1m",
    },
    {
        "key": "sleeping_recovery",
        "title": "Спящие в реанимации",
        "condition": "stage == isolate_1m and ladder_rule_name == спящие_3м",
    },
    {
        "key": "need_first_contact",
        "title": "Нет первого касания",
        "condition": "stage in (retention_work,isolate_1m,recovery_control) and last_contact_at is empty",
    },
    {
        "key": "promise_overdue",
        "title": "Обещал закупку и не сделал",
        "condition": "promised_purchase_date < today and stage in (retention_work,isolate_1m,recovery_control)",
    },
    {
        "key": "economics_recovery_priority",
        "title": "Приоритетная реанимация",
        "condition": (
            "economics_bucket in (хорошая экономика,приоритетная реанимация)"
            " and stage in (retention_work,isolate_1m,recovery_control)"
        ),
    },
    {
        "key": "quality_blocks_upgrade",
        "title": "Качество блокирует улучшение",
        "condition": "stage == quality_check",
    },
    {
        "key": "credit_blocks_upgrade",
        "title": "Платежный риск блокирует улучшение",
        "condition": "stage == credit_check",
    },
    {
        "key": "data_conflict",
        "title": "Сначала чистим данные",
        "condition": "stage == data_check",
    },
    {
        "key": "manual_upgrade_waiting",
        "title": "Ждет подтверждения улучшения",
        "condition": "stage == manual_upgrade_approval",
    },
    {
        "key": "manual_downgrade_waiting",
        "title": "Ждет подтверждения понижения",
        "condition": "stage == manual_downgrade_approval",
    },
    {
        "key": "ready_for_1c_export",
        "title": "Готово к выгрузке в 1С",
        "condition": "stage == ready_for_1c",
    },
]

# Минимальный набор колонок list view из blueprint draft 2026-07-16.
# `stage` — штатная колонка стадии smart process, не UF-поле.
LIST_VIEW_FIELDS = [
    "counterparty_code",
    "counterparty_name",
    "current_price_type",
    "target_price_type_candidate",
    "final_decision",
    "automation_level",
    "three_month_sales_total",
    "last_full_month_sales",
    "gross_profit_90",
    "profitability_pct_90",
    "payment_behavior_group",
    "credit_discipline_grade",
    "return_quality_grade",
    "history_bucket",
    "stage",
    "next_action_date",
    "approval_status",
]

TRANSITION_RULES = [
    {
        "from": "new_snapshot",
        "to": "data_check",
        "when": "source_status != ready or duplicate_flag == yes or data_conflict_flag == yes",
    },
    {
        "from": "new_snapshot",
        "to": "preclose_signal",
        "when": "preclose_risk_flag == yes",
    },
    {
        "from": "new_snapshot",
        "to": "retention_work",
        "when": (
            "three_month_sales_total < retention_norm(level) and "
            "last_full_month_sales >= hold_threshold(level); "
            f"{RULESET_TAG}: {LEVEL_THRESHOLDS_NOTE}"
        ),
    },
    {
        "from": "new_snapshot",
        "to": "isolate_1m",
        "when": (
            "three_month_sales_total < retention_norm(level) and "
            "last_full_month_sales < hold_threshold(level); "
            f"{RULESET_TAG}: {LEVEL_THRESHOLDS_NOTE}"
        ),
    },
    {
        "from": "new_snapshot",
        "to": "isolate_1m",
        "when": "sleeping client: 3 full months with zero sales and purchases before, recovery month with terms kept",
    },
    {
        "from": "new_snapshot",
        "to": "recovery_control",
        "when": "history_bucket in (comeback,historical_b2b,rehab_economics)",
    },
    {
        "from": "new_snapshot",
        "to": "quality_check",
        "when": "return_quality_grade in (D,E) or return_behavior_group == dispute_quality",
    },
    {
        "from": "new_snapshot",
        "to": "credit_check",
        "when": "credit_discipline_grade in (D,E) or over_limit_amount > 0",
    },
    {
        "from": "new_snapshot",
        "to": "manual_upgrade_approval",
        "when": "target_price_type_candidate != current_price_type and price_type_change_direction == upgrade and no stop_factors",
    },
    {
        "from": "new_snapshot",
        "to": "closed_keep_current",
        "when": "no price type action required and no stop_factors",
    },
    {
        "from": "preclose_signal",
        "to": "retention_work",
        "when": "month closed and fact confirms retention scenario",
    },
    {
        "from": "preclose_signal",
        "to": "closed_keep_current",
        "when": "client recovered before month close",
    },
    {
        "from": "retention_work",
        "to": "closed_keep_current",
        "when": "three_month_sales_total >= threshold_lower or client retained without price type change",
    },
    {
        "from": "retention_work",
        "to": "isolate_1m",
        "when": (
            "retention failed and last_full_month_sales < hold_threshold(level); " f"{RULESET_TAG}"
        ),
    },
    {
        "from": "isolate_1m",
        "to": "closed_keep_current",
        "when": "client recovered during isolate period or valid drop reason logged",
    },
    {
        "from": "isolate_1m",
        "to": "manual_downgrade_approval",
        "when": "isolate period ended and no recovery and manager_action_log_status == full",
    },
    {
        "from": "isolate_1m",
        "to": "recovery_control",
        "when": "protective factor found during isolate: history, economics or comeback",
    },
    {
        "from": "recovery_control",
        "to": "closed_keep_current",
        "when": "client protected and keeps current price type",
    },
    {
        "from": "recovery_control",
        "to": "manual_downgrade_approval",
        "when": "protective scenario exhausted and real drop confirmed",
    },
    {
        "from": "quality_check",
        "to": "closed_keep_current",
        "when": "quality block: price type must not change",
    },
    {
        "from": "quality_check",
        "to": "manual_upgrade_approval",
        "when": "quality issue resolved and upgrade candidate valid again",
    },
    {
        "from": "credit_check",
        "to": "closed_keep_current",
        "when": "credit block: price type must not change",
    },
    {
        "from": "credit_check",
        "to": "manual_upgrade_approval",
        "when": "credit stop removed and upgrade allowed again",
    },
    {
        "from": "data_check",
        "to": "new_snapshot",
        "when": "data cleaned and requalification allowed",
    },
    {
        "from": "manual_upgrade_approval",
        "to": "ready_for_1c",
        "when": "approval_status == approved",
    },
    {
        "from": "manual_upgrade_approval",
        "to": "closed_keep_current",
        "when": "approval_status == rejected",
    },
    {
        "from": "manual_downgrade_approval",
        "to": "ready_for_1c",
        "when": "approval_status == approved",
    },
    {
        "from": "manual_downgrade_approval",
        "to": "closed_keep_current",
        "when": "approval_status == rejected",
    },
    {
        "from": "ready_for_1c",
        "to": "closed_changed",
        "when": "onec_export_status confirms export to 1C",
    },
]

STOP_FACTORS = [
    {"key": "partial_source", "blocks": "all", "when": "source_status != ready"},
    {"key": "duplicate_master_data", "blocks": "all", "when": "duplicate_flag == yes"},
    {
        "key": "service_or_fake_card",
        "blocks": "all",
        "when": "card is technical or fake, not a live client",
    },
    {
        "key": "incomplete_month",
        "blocks": "upgrade,downgrade",
        "when": "incomplete month or new client",
    },
    {"key": "comeback_not_mature", "blocks": "downgrade", "when": "comeback_full_month_no < 3"},
    {
        "key": "historical_b2b_protection",
        "blocks": "downgrade",
        "when": "historical_b2b_flag == yes",
    },
    {
        "key": "economics_recovery_required",
        "blocks": "downgrade",
        "when": (
            "economics_status == ok and profitability_pct_90 >= 40"
            " and three_month_sales_total < threshold_lower"
        ),
    },
    {
        "key": "upgrade_freeze",
        "blocks": "upgrade",
        "when": (
            "ГЛОБАЛЬНАЯ ЗАМОРОЗКА (ruleset upgrades.frozen=true, решение "
            "2026-07-17/18): любые повышения уровня не выполняются; кандидаты "
            "отображаются информационно, стадия manual_upgrade_approval "
            "недостижима до отдельной команды о разморозке"
        ),
    },
    {"key": "credit_risk_high", "blocks": "upgrade", "when": "credit_discipline_grade in (D,E)"},
    {"key": "over_credit_limit", "blocks": "upgrade", "when": "over_limit_amount > 0"},
    {
        "key": "bad_payment_behavior",
        "blocks": "upgrade",
        "when": "payment_behavior_group in (chronic_non_payer,promise_breaker,silent_no_contact)",
    },
    {
        "key": "quality_risk_high",
        "blocks": "upgrade",
        "when": "return_quality_grade in (D,E) or return_behavior_group == dispute_quality",
    },
    {
        "key": "manager_action_not_logged",
        "blocks": "downgrade",
        "when": "stage == isolate_1m and manager_action_log_status != full",
    },
    {
        "key": "retail_auto_upgrade_forbidden",
        "blocks": "upgrade",
        "when": "current_price_type == retail",
    },
    {
        "key": "key_account_zone",
        "blocks": "all",
        "when": "target_price_type_candidate == key_account",
    },
]

APPROVAL_GATES = [
    {"key": "retail_to_b2b_upgrade_gate", "approver": "руководитель / B2B"},
    {"key": "upgrade_gate", "approver": "руководитель"},
    {"key": "downgrade_after_isolate_gate", "approver": "руководитель"},
    {"key": "credit_risk_gate", "approver": "финансы / руководитель"},
    {"key": "quality_dispute_gate", "approver": "качество / руководитель"},
    {"key": "key_account_gate", "approver": "ручная экономика"},
    {
        "key": "veteran_level_return_gate",
        "approver": "руководитель, по аргументам менеджера (стаж 18+ мес, карточка положительная, экономика выгодная)",
    },
]

EXPORT_GATE_RULES = [
    "final_decision must be filled",
    "approved_target_price_type must be filled",
    "approval_status must be approved",
    "source_status must be ready",
    "no active stop_factor remains",
    "for downgrade after isolate manager_action_log_status must be full",
    "for upgrade credit and quality blocks must be absent",
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
    return f"UF_CRM_CUSTOMER_PRICE_TYPE_{suffix}"


def _field_blueprint(spec: dict[str, Any]) -> dict[str, Any]:
    row = dict(spec)
    row["xml_id"] = _field_xml_id(str(spec["logical_key"]))
    row.setdefault("searchable", False)
    row.setdefault("edit_in_list", True)
    row.setdefault("required", spec["logical_key"] in REQUIRED_FIELD_KEYS)
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
        "rulebook": {
            "ruleset_version": RULESET["ruleset_version"],
            "effective_date": str(RULESET["effective_date"]),
            "source": "config/price_types/ruleset.yaml",
            "levels": {
                key: {
                    "price_type_prefix": val["price_type_prefix"],
                    "retention_norm_3m": val["retention_norm_3m"],
                    "hold_last_month": val["hold_last_month"],
                    "downgrade_to": val["downgrade_to"],
                }
                for key, val in LEVELS.items()
            },
            "upgrades_frozen": RULESET["upgrades"]["frozen"],
        },
        "safety": {
            "bitrix_writes": False,
            "allowed_bitrix_methods": sorted(READ_ONLY_BITRIX_METHODS),
            "requires_owner_presence_for_apply": True,
        },
        "process": {
            "title": DEFAULT_PROCESS_TITLE,
            "code": DEFAULT_PROCESS_CODE,
            "category_name": DEFAULT_CATEGORY_NAME,
            "current_process_kept_as_history": None,
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
        "list_view_fields": LIST_VIEW_FIELDS,
        "transition_rules": TRANSITION_RULES,
        "stop_factors": STOP_FACTORS,
        "approval_gates": APPROVAL_GATES,
        "export_gate_rules": EXPORT_GATE_RULES,
        "price_type_levels": PRICE_TYPE_ENUM,
        "automation_levels": AUTOMATION_LEVEL_ENUM,
        "approval_statuses": APPROVAL_STATUS_ENUM,
        "access_rules": {
            "sales_managers": "own buyers only",
            "senior_sales": "department scope",
            "finance_control": "credit and approval checks",
            "other_onec_folders": "owner approval required",
            "economics_numbers": (
                "managers see signals only (economics_status, buckets); "
                "raw money fields (gross_profit, margins, cost) visible to "
                "head role only; external operators never see economics"
            ),
        },
        "live_snapshot": live_snapshot or {"status": "not_requested"},
    }


def _safe_bitrix_call(webhook_base: str, method: str, params: dict[str, Any]) -> Any:
    if method not in READ_ONLY_BITRIX_METHODS:
        raise RuntimeError(f"Refusing non-read-only Bitrix method: {method}")
    return bitrix_setup.bitrix_call(webhook_base, method, params).get("result")


def _resolve_entity_type_id(
    types: list[dict[str, Any]], *, current_entity_type_id: int
) -> int | None:
    if current_entity_type_id > 0:
        return current_entity_type_id
    for item in types:
        if str(item.get("title") or "").strip() == DEFAULT_PROCESS_TITLE:
            entity_type_id = item.get("entityTypeId")
            if str(entity_type_id).isdigit():
                return int(entity_type_id)
        if str(item.get("code") or "").strip() == DEFAULT_PROCESS_CODE:
            entity_type_id = item.get("entityTypeId")
            if str(entity_type_id).isdigit():
                return int(entity_type_id)
    return None


def build_live_snapshot(webhook_base: str, *, current_entity_type_id: int) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "status": "ready",
        "requested_entity_type_id": current_entity_type_id,
        "write_methods_used": [],
    }
    types: list[dict[str, Any]] = []
    resolved_entity_type_id: int | None = None
    try:
        types_result = _safe_bitrix_call(webhook_base, "crm.type.list", {})
        types = types_result.get("types") if isinstance(types_result, dict) else []
        resolved_entity_type_id = _resolve_entity_type_id(
            types or [],
            current_entity_type_id=current_entity_type_id,
        )
        snapshot["current_process"] = next(
            (
                {
                    "id": item.get("id"),
                    "entityTypeId": item.get("entityTypeId"),
                    "title": item.get("title"),
                    "code": item.get("code"),
                }
                for item in types or []
                if str(item.get("title") or "").strip() == DEFAULT_PROCESS_TITLE
                or str(item.get("code") or "").strip() == DEFAULT_PROCESS_CODE
                or int(item.get("entityTypeId") or 0) == (resolved_entity_type_id or -1)
            ),
            None,
        )
        snapshot["title_collision"] = [
            {
                "id": item.get("id"),
                "entityTypeId": item.get("entityTypeId"),
                "title": item.get("title"),
                "code": item.get("code"),
            }
            for item in types or []
            if str(item.get("title") or "").strip() == DEFAULT_PROCESS_TITLE
        ]
        snapshot["resolved_entity_type_id"] = resolved_entity_type_id
    except Exception as exc:  # pragma: no cover
        snapshot["type_error"] = str(exc)

    if resolved_entity_type_id:
        try:
            categories_result = _safe_bitrix_call(
                webhook_base,
                "crm.category.list",
                {"entityTypeId": resolved_entity_type_id},
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
                        "filter[ENTITY_ID]": f"DYNAMIC_{resolved_entity_type_id}_STAGE_{category_id}",
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
                {"entityTypeId": resolved_entity_type_id},
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
                    "entityTypeId": resolved_entity_type_id,
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
    else:
        snapshot["status"] = "partial"
        snapshot["reason"] = "entity_type_not_resolved"

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
