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
DEFAULT_DETAILS_CONFIG_PATH = (
    REPO_ROOT / "build/bitrix/procurement_order_details_configuration.json"
)
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/procurement_order_ensure_result.json"
DEFAULT_PROCESS_TITLE = "Закупка/Заказ"
DEFAULT_PROCESS_CODE = "procurement_order"
ONEC_CONTOUR_REQUISITE = "КонтурЗакупки"
CRM_SUPPLIER_ORIGINATOR_ID = "mm_onec_supplier"
VED_SUPPLIER_PROCESS_TITLE = "Поставщик (ВЭД)"
CERTIFICATE_PROCESS_TITLE = "Сертификаты"
PRODUCT_PASSPORT_PROCESS_TITLE = "Паспорт товара (ВЭД)"
PROCUREMENT_TYPE_REQUIRED_FLAGS = {
    "isLinkWithProductsEnabled": True,
}

CONTOUR_CONTRACT = [
    {
        "logical_key": "ordinary",
        "onec_values": ["Обычный"],
        "bitrix_value": "Обычный",
        "default": True,
    },
    {
        "logical_key": "cargo",
        "onec_values": ["Cargo", "Карго"],
        "bitrix_value": "Карго",
        "bitrix_aliases": ["Cargo"],
        "default": False,
    },
    {
        "logical_key": "ved_import",
        "onec_values": ["ВЭД импорт", "ВЭДИмпорт"],
        "bitrix_value": "ВЭД импорт",
        "default": False,
    },
]

CONTOUR_ENUM = [
    {
        "xml_id": item["logical_key"],
        "value": item["bitrix_value"],
        **({"default": True} if item.get("default") else {}),
        **({"aliases": item["bitrix_aliases"]} if item.get("bitrix_aliases") else {}),
    }
    for item in CONTOUR_CONTRACT
]

LABEL_STATUS_ENUM = [
    {"value": "Черновик", "xml_id": "draft"},
    {"value": "Заблокировано", "xml_id": "blocked"},
    {"value": "Утверждено", "xml_id": "approved"},
    {"value": "Отправлено фабрике", "xml_id": "sent_to_factory"},
]

CERTIFICATION_DOCS_STATUS_ENUM = [
    {"value": "Черновик", "xml_id": "draft"},
    {"value": "Нужны данные", "xml_id": "needs_data"},
    {"value": "Заблокировано", "xml_id": "blocked"},
    {"value": "Пакет готов", "xml_id": "ready"},
    {"value": "GTIN заказан", "xml_id": "gtin_requested"},
    {"value": "Передано сертификатору", "xml_id": "sent_to_certifier"},
]

PAYMENT_TASK_STATUS_ENUM = [
    {"value": "Создана", "xml_id": "created"},
    {"value": "Исполнена", "xml_id": "done"},
    {"value": "Пропущена", "xml_id": "skipped"},
    {"value": "Ошибка", "xml_id": "error"},
]

ORDER_LIFECYCLE_STATUS_ENUM = [
    {"value": "Черновик", "xml_id": "draft"},
    {"value": "На проверке", "xml_id": "review"},
    {"value": "Заблокирован", "xml_id": "blocked"},
    {"value": "Передаётся в 1С", "xml_id": "transmitting"},
    {"value": "Активен", "xml_id": "active"},
    {"value": "В пути", "xml_id": "in_transit"},
    {"value": "Частично поступил", "xml_id": "partially_received"},
    {"value": "Поступил", "xml_id": "received"},
    {"value": "Отменён", "xml_id": "cancelled"},
]

AUTO_ORDER_DECISION_ENUM = [
    {"value": "К заказу", "xml_id": "order"},
    {"value": "Ручная проверка", "xml_id": "manual_review"},
    {"value": "Не заказывать", "xml_id": "do_not_order"},
]

ASSORTMENT_STATUS_DECISION_ENUM = [
    {"value": "Без изменения", "xml_id": "no_change", "default": True},
    {"value": "Матричный", "xml_id": "matrix"},
    {"value": "Рабочий", "xml_id": "working"},
    {"value": "Под заказ", "xml_id": "on_demand"},
    {"value": "Кандидат на замену", "xml_id": "replace_candidate"},
    {"value": "Неликвид", "xml_id": "nonliquid"},
    {"value": "Не закупать", "xml_id": "do_not_order"},
]

CERTIFICATE_STATUS_ENUM = [
    {"value": "Найдено", "xml_id": "found"},
    {"value": "Проверено", "xml_id": "verified"},
    {"value": "Требует обновления", "xml_id": "needs_update"},
    {"value": "Истекло", "xml_id": "expired"},
]

CERTIFICATE_DOCUMENT_TYPE_ENUM = [
    {"value": "ДС ЕАЭС", "xml_id": "declaration_eaeu"},
    {"value": "ДС ГОСТ Р", "xml_id": "declaration_gost_r"},
    {"value": "Мастер-таблица ДС", "xml_id": "declaration_master_register"},
    {"value": "GTIN/EAN-13", "xml_id": "gtin_ean13"},
    {"value": "RoHS", "xml_id": "rohs"},
    {"value": "MSDS", "xml_id": "msds"},
    {"value": "UN38.3", "xml_id": "un38_3"},
    {"value": "CE-EMC", "xml_id": "ce_emc"},
    {"value": "Другое", "xml_id": "other"},
]

CRM_SYNC_FIELD_SPECS = [
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_ONEC_SUPPLIER_REF",
        "title": "1С ref поставщика",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_ONEC_SUPPLIER_CODE",
        "title": "1С код поставщика",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_ONEC_SUPPLIER_UPDATED_AT",
        "title": "1С поставщик изменен",
        "type": "datetime",
        "multiple": False,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_SUPPLIER_REG_NO",
        "title": "Регистрационный номер поставщика",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_SUPPLIER_ROLE",
        "title": "Роль поставщика MM",
        "type": "string",
        "multiple": True,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_SUPPLIER_COUNTRY",
        "title": "Страна поставщика",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_SUPPLIER_CITY",
        "title": "Город поставщика",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_WECHAT",
        "title": "WeChat",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "company",
        "field_name": "UF_CRM_MM_WHATSAPP",
        "title": "WhatsApp",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "contact",
        "field_name": "UF_CRM_MM_ONEC_CONTACT_REF",
        "title": "1С ref контакта поставщика",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "contact",
        "field_name": "UF_CRM_MM_ONEC_CONTACT_CODE",
        "title": "1С код контакта поставщика",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "contact",
        "field_name": "UF_CRM_MM_ONEC_CONTACT_UPDATED_AT",
        "title": "1С контакт изменен",
        "type": "datetime",
        "multiple": False,
    },
    {
        "entity": "contact",
        "field_name": "UF_CRM_MM_WECHAT",
        "title": "WeChat",
        "type": "string",
        "multiple": False,
    },
    {
        "entity": "contact",
        "field_name": "UF_CRM_MM_WHATSAPP",
        "title": "WhatsApp",
        "type": "string",
        "multiple": False,
    },
]

CRM_USERFIELD_METHODS = {
    "company": ("crm.company.userfield.list", "crm.company.userfield.add"),
    "contact": ("crm.contact.userfield.list", "crm.contact.userfield.add"),
}

CRM_USERFIELD_TYPE_MAP = {
    "string": "string",
    "datetime": "datetime",
    "integer": "integer",
    "boolean": "boolean",
}

VED_SUPPLIER_PASSPORT_FIELD_SPECS = [
    {
        "logical_key": "crm_company",
        "title": "CRM company",
        "type": "crm_company",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
]

CERTIFICATE_FIELD_SPECS = [
    {
        "logical_key": "document_type",
        "title": "Тип документа",
        "type": "enumeration",
        "enum": CERTIFICATE_DOCUMENT_TYPE_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "declaration_number",
        "title": "Номер ДС",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "verification_status",
        "title": "Статус проверки",
        "type": "enumeration",
        "enum": CERTIFICATE_STATUS_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "valid_from",
        "title": "Действует с",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "applicant",
        "title": "Заявитель",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "manufacturer",
        "title": "Изготовитель",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "brand",
        "title": "Бренд",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "product_series",
        "title": "Серия/бренд",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "tnved",
        "title": "ТН ВЭД",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "technical_regulations",
        "title": "Техрегламенты",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "test_report_number",
        "title": "Номер протокола",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "covered_skus",
        "title": "Покрытые SKU",
        "type": "text",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "eac_allowed",
        "title": "EAC разрешен",
        "type": "boolean",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
]

PRODUCT_PASSPORT_FIELD_SPECS = [
    {
        "logical_key": "manufacturer",
        "title": "Изготовитель",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "product_series",
        "title": "Серия/бренд",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
]

CERTIFICATE_TITLE_ALIASES = {
    "document_type": ("Тип документа",),
    "declaration_number": ("Номер ДС",),
    "verification_status": ("Статус проверки",),
    "valid_from": ("Действует с",),
    "valid_to": ("Действует до",),
    "file": ("Файл ДС",),
    "applicant": ("Заявитель",),
    "manufacturer": ("Изготовитель",),
    "brand": ("Бренд",),
    "product_series": ("Серия/бренд",),
    "tnved": ("ТН ВЭД",),
    "technical_regulations": ("Техрегламенты",),
    "test_report_number": ("Номер протокола",),
    "covered_skus": ("Покрытые SKU",),
    "eac_allowed": ("EAC разрешен",),
}

PRODUCT_PASSPORT_TITLE_ALIASES = {
    "trade_name": ("Trade name", "Торговое наименование", "Наименование для этикетки"),
    "sku": ("Артикул", "SKU"),
    "onec_item_code": ("Код 1С", "1С код", "Заводской код"),
    "factory_code": ("Заводской код",),
    "barcode_gtin": ("Barcode/GTIN", "Штрихкод", "Штрихкод 1С"),
    "tnved": ("ТН ВЭД",),
    "gs1_gtin": ("GTIN (GS1)",),
    "internal_barcode": ("Внутренний штрихкод", "Штрихкод 1С"),
    "manufacturer": ("Изготовитель",),
    "product_series": ("Серия/бренд",),
}

CONTOUR_ALIASES = {
    "": "ordinary",
    "ordinary": "ordinary",
    "обычный": "ordinary",
    "обычная": "ordinary",
    "cargo": "cargo",
    "карго": "cargo",
    "vedimport": "ved_import",
    "ved_import": "ved_import",
    "вэдимпорт": "ved_import",
    "вэд импорт": "ved_import",
}

RUBLE_CURRENCY_TOKENS = {
    "643",
    "rub",
    "rur",
    "руб",
    "руб.",
    "рубль",
    "рубли",
    "российскийрубль",
    "российскиерубли",
}

CATEGORY_SPECS = [
    {
        "logical_key": "ordinary",
        "name": "Обычный",
        "sort": 100,
        "reuse_default": False,
        "stages": [
            {
                "logical_key": "supplier_order",
                "code": "NEW",
                "name": "Заказ поставщику",
                "sort": 100,
            },
            {
                "logical_key": "waiting_delivery",
                "code": "WAITING",
                "name": "Ожидаем поставку",
                "sort": 200,
            },
            {
                "logical_key": "receiving",
                "code": "RECEIVING",
                "name": "Приемка на склад",
                "sort": 300,
            },
            {
                "logical_key": "closed",
                "code": "CLOSED",
                "name": "Закрыто",
                "sort": 900,
                "semantics": "S",
            },
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
        "name": "Карго",
        "aliases": ["Cargo"],
        "sort": 200,
        "reuse_default": False,
        "stages": [
            {
                "logical_key": "supplier_order",
                "code": "NEW",
                "name": "Заказ поставщику",
                "sort": 100,
            },
            {
                "logical_key": "ready_for_cargo",
                "code": "CLIENT",
                "name": "Товар готов к сдаче в карго",
                "sort": 200,
            },
            {
                "logical_key": "cargo_dropoff",
                "code": "CARGO",
                "name": "Сдано в карго",
                "sort": 300,
            },
            {
                "logical_key": "payment_work",
                "code": "PAYREQ",
                "name": "Заявка на оплату / оплата в работе",
                "sort": 400,
            },
            {
                "logical_key": "in_transit",
                "code": "TRANSIT",
                "name": "Товар в пути",
                "sort": 500,
            },
            {
                "logical_key": "own_delivery",
                "code": "SUPPLIER_DISPATCH",
                "name": "Доставка своими силами",
                "sort": 700,
            },
            {
                "logical_key": "unpacking",
                "code": "UNPACKING",
                "name": "Зона разборки",
                "sort": 800,
            },
            {
                "logical_key": "receiving",
                "code": "RECEIVING",
                "name": "Приемка товара",
                "sort": 900,
            },
            {
                "logical_key": "onec_receipt",
                "code": "ONEC_RECEIPT",
                "name": "Поступление в 1С",
                "sort": 1000,
            },
            {
                "logical_key": "barcode",
                "code": "BARCODE",
                "name": "Штрихкодирование",
                "sort": 1100,
            },
            {
                "logical_key": "placement",
                "code": "PLACEMENT",
                "name": "Зона раскладки",
                "sort": 1200,
            },
            {
                "logical_key": "closed",
                "code": "CLOSED",
                "name": "Раскладка завершена",
                "sort": 1300,
                "semantics": "S",
            },
            {
                "logical_key": "exception",
                "code": "EXCEPTION",
                "name": "Проблема / отмена",
                "sort": 1400,
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
            {
                "logical_key": "supplier_order",
                "code": "NEW",
                "name": "Заказ поставщику",
                "sort": 100,
            },
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
                "logical_key": "payment_agent",
                "code": "PAYMENT",
                "name": "Оплата / платежный агент",
                "sort": 400,
            },
            {
                "logical_key": "logistics_customs",
                "code": "LOGISTICS",
                "name": "Логистика / таможня",
                "sort": 500,
            },
            {
                "logical_key": "customs_clearance",
                "code": "CUSTOMS",
                "name": "Растаможка",
                "sort": 600,
            },
            {
                "logical_key": "receiving",
                "code": "RECEIVING",
                "name": "Приемка на склад",
                "sort": 700,
            },
            {
                "logical_key": "closed",
                "code": "CLOSED",
                "name": "Закрыто",
                "sort": 900,
                "semantics": "S",
            },
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

OBSOLETE_STAGE_MERGES = {
    "ordinary": [
        {
            "old_codes": ["PREPARATION", "TERMS"],
            "old_names": ["Согласование условий"],
            "target_code": "NEW",
            "reason": "supplier_terms moved to order_formation; procurement starts at supplier_order",
        },
        {
            "old_codes": ["CLIENT", "ORDER"],
            "old_names": ["Заказ поставщику"],
            "target_code": "NEW",
            "reason": "supplier_order merged into system start stage",
        },
    ],
    "cargo": [
        {
            "old_codes": ["PREPARATION"],
            "target_code": "NEW",
            "reason": "supplier_assembly moved to order_formation; procurement starts at supplier_order",
        },
        {
            "old_codes": ["ORDER"],
            "old_names": ["Заказ поставщику"],
            "target_code": "NEW",
            "reason": "supplier_order merged into system start stage",
        },
        {
            "old_codes": ["PAYOK"],
            "target_code": "SUPPLIER_DISPATCH",
            "reason": "arrived is an action before own_delivery, not a separate stage",
        },
    ],
    "ved_import": [
        {
            "old_codes": ["ORDER", "UC_OW8QYB"],
            "old_names": ["Заказ поставщику"],
            "target_code": "NEW",
            "reason": "supplier_order merged into system start stage",
        },
    ],
}

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "procurement_contour",
        "title": "Вид транспортной отправки",
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
        "logical_key": "onec_document_ref",
        "title": "1С GUID заказа поставщику",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
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
        "logical_key": "order_lifecycle_status",
        "title": "Статус заказа",
        "type": "enumeration",
        "enum": ORDER_LIFECYCLE_STATUS_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "ordered_quantity",
        "title": "Заказано",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": False,
    },
    {
        "logical_key": "open_quantity",
        "title": "Открытый остаток",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": False,
    },
    {
        "logical_key": "received_quantity",
        "title": "Поступило",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": False,
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
        "logical_key": "supplier_onec_ref",
        "title": "1С ref поставщика",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "supplier_resolution_status",
        "title": "Статус CRM-поставщика",
        "type": "enumeration",
        "enum": [
            {"xml_id": "resolved_existing", "value": "Найден в CRM"},
            {"xml_id": "created_from_onec", "value": "Создан из 1С"},
            {"xml_id": "manual_review", "value": "Ручная проверка"},
            {"xml_id": "blocked_duplicate", "value": "Конфликт дублей"},
        ],
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "supplier_resolution_basis",
        "title": "Как найден поставщик",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "supplier_conflicts",
        "title": "Конфликты CRM-поставщика",
        "type": "text",
        "required": False,
        "searchable": False,
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
        "logical_key": "supplier_dispatch_date",
        "title": "Отправка поставщику на обсуждение",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "cargo_dropoff_date",
        "title": "Сдача в карго",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "expected_receipt_date",
        "title": "Поступление",
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
    {
        "logical_key": "label_generation_status",
        "title": "Этикетки: статус",
        "type": "enumeration",
        "enum": LABEL_STATUS_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "label_generation_version",
        "title": "Этикетки: версия макета",
        "type": "integer",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "label_generation_zip_url",
        "title": "Этикетки: ZIP",
        "type": "url",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "label_generation_disk_file_id",
        "title": "Этикетки: Disk file id",
        "type": "string",
        "required": False,
        "searchable": False,
        "edit_in_list": False,
    },
    {
        "logical_key": "label_generation_errors",
        "title": "Этикетки: ошибки",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "label_generation_approved_at",
        "title": "Этикетки: утверждено",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "label_generation_sent_to_factory_at",
        "title": "Этикетки: отправлено фабрике",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "certification_docs_status",
        "title": "Сертификация: статус",
        "type": "enumeration",
        "enum": CERTIFICATION_DOCS_STATUS_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "certification_docs_version",
        "title": "Сертификация: версия пакета",
        "type": "integer",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "certification_docs_zip_url",
        "title": "Сертификация: ZIP",
        "type": "url",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "certification_docs_disk_file_id",
        "title": "Сертификация: Disk file id",
        "type": "string",
        "required": False,
        "searchable": False,
        "edit_in_list": False,
    },
    {
        "logical_key": "certification_docs_errors",
        "title": "Сертификация: ошибки/что дозаполнить",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "certification_docs_generated_at",
        "title": "Сертификация: пакет собран",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "payment_task_id",
        "title": "Задача оплаты: ID",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "payment_task_status",
        "title": "Задача оплаты: статус",
        "type": "enumeration",
        "enum": PAYMENT_TASK_STATUS_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "payment_request_created_at",
        "title": "Заявка оплаты создана",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_source",
        "title": "Автозаказ: источник",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_run_id",
        "title": "Автозаказ: run id",
        "type": "integer",
        "required": False,
        "searchable": True,
        "edit_in_list": False,
    },
    {
        "logical_key": "auto_order_sku_code",
        "title": "Автозаказ: код номенклатуры",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_sku_name",
        "title": "Автозаказ: номенклатура",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_decision",
        "title": "Автозаказ: решение",
        "type": "enumeration",
        "enum": AUTO_ORDER_DECISION_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_recommended_qty",
        "title": "Автозаказ: рекомендовано, шт.",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_raw_qty",
        "title": "Автозаказ: расчет до лимита, шт.",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_target_stock_qty",
        "title": "Автозаказ: целевой остаток, шт.",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_free_stock_qty",
        "title": "Автозаказ: свободно, шт.",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_incoming_qty",
        "title": "Автозаказ: в пути, шт.",
        "type": "double",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_reason",
        "title": "Автозаказ: объяснение",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_warnings",
        "title": "Автозаказ: предупреждения",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_blockers",
        "title": "Автозаказ: блокеры",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "auto_order_calculated_at",
        "title": "Автозаказ: рассчитано",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "assortment_status_decision",
        "title": "Статус ассортимента: решение",
        "type": "enumeration",
        "enum": ASSORTMENT_STATUS_DECISION_ENUM,
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "assortment_status_reason",
        "title": "Статус ассортимента: причина",
        "type": "text",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "assortment_status_approved_by",
        "title": "Статус ассортимента: утвердил",
        "type": "string",
        "required": False,
        "searchable": True,
        "edit_in_list": True,
    },
    {
        "logical_key": "assortment_status_changed_at",
        "title": "Статус ассортимента: дата решения",
        "type": "datetime",
        "required": False,
        "searchable": False,
        "edit_in_list": True,
    },
    {
        "logical_key": "assortment_commercial_marks",
        "title": "Статус ассортимента: коммерческие признаки",
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
            "pilot_batch_id",
        ],
    },
    {
        "name": "transport",
        "title": "Вид транспортной отправки",
        "elements": [
            "procurement_contour",
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
            "supplier_dispatch_date",
            "cargo_dropoff_date",
            "expected_receipt_date",
        ],
    },
    {
        "name": "counterparties",
        "title": "Участники",
        "elements": [
            "supplier_company",
            "supplier_onec_ref",
            "supplier_resolution_status",
            "supplier_resolution_basis",
            "supplier_conflicts",
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
            "payment_task_id",
            "payment_task_status",
            "payment_request_created_at",
            "expects_import_gtd",
            "gtd_number",
            "blocker_comment",
        ],
    },
    {
        "name": "auto_order",
        "title": "Автозаказ",
        "elements": [
            "auto_order_source",
            "auto_order_run_id",
            "auto_order_sku_code",
            "auto_order_sku_name",
            "auto_order_decision",
            "auto_order_recommended_qty",
            "auto_order_raw_qty",
            "auto_order_target_stock_qty",
            "auto_order_free_stock_qty",
            "auto_order_incoming_qty",
            "auto_order_reason",
            "auto_order_warnings",
            "auto_order_blockers",
            "auto_order_calculated_at",
        ],
    },
    {
        "name": "assortment_status",
        "title": "Статус ассортимента",
        "elements": [
            "assortment_status_decision",
            "assortment_status_reason",
            "assortment_status_approved_by",
            "assortment_status_changed_at",
            "assortment_commercial_marks",
        ],
    },
    {
        "name": "labels",
        "title": "Этикетки ВЭД",
        "elements": [
            "label_generation_status",
            "label_generation_version",
            "label_generation_zip_url",
            "label_generation_disk_file_id",
            "label_generation_errors",
            "label_generation_approved_at",
            "label_generation_sent_to_factory_at",
        ],
    },
    {
        "name": "certification_docs",
        "title": "Сертификация / GTIN",
        "elements": [
            "certification_docs_status",
            "certification_docs_version",
            "certification_docs_zip_url",
            "certification_docs_disk_file_id",
            "certification_docs_errors",
            "certification_docs_generated_at",
        ],
    },
]


def _contour_token(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "").replace("_", "").replace(" ", "")


def _currency_token(value: Any) -> str:
    return str(value or "").strip().casefold().replace(" ", "")


def _is_ruble_currency(value: Any) -> bool:
    token = _currency_token(value)
    return bool(token and token in RUBLE_CURRENCY_TOKENS)


def _is_foreign_currency(value: Any) -> bool:
    token = _currency_token(value)
    return bool(token and token not in RUBLE_CURRENCY_TOKENS)


def normalize_onec_procurement_contour(
    value: Any,
    *,
    is_open_supplier_order: bool = False,
    currency: Any = None,
    has_cargo_dropoff: bool = False,
) -> str:
    """Map 1C ЗаказПоставщику.КонтурЗакупки to a stable integration key."""

    raw_value = str(value or "").strip()
    if not raw_value:
        if has_cargo_dropoff:
            return "cargo"
        if _is_foreign_currency(currency):
            return "cargo"
        if _is_ruble_currency(currency):
            return "ordinary"
        return "cargo" if is_open_supplier_order else "ordinary"

    direct_key = raw_value.casefold()
    if direct_key in CONTOUR_ALIASES:
        return CONTOUR_ALIASES[direct_key]

    compact_key = _contour_token(raw_value)
    if compact_key in CONTOUR_ALIASES:
        return CONTOUR_ALIASES[compact_key]

    raise ValueError(f"Unsupported procurement contour value from 1C: {raw_value!r}")


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


def crm_add_field_name(full_field_name: str) -> str:
    normalized = str(full_field_name or "").strip().upper()
    return normalized.removeprefix("UF_CRM_")


def crm_field_type(spec: dict[str, Any]) -> str:
    return CRM_USERFIELD_TYPE_MAP.get(str(spec.get("type") or "string"), "string")


def crm_userfield_payload(spec: dict[str, Any]) -> dict[str, Any]:
    field_name = str(spec["field_name"]).strip().upper()
    title = str(spec.get("title") or field_name)
    return {
        "fields": {
            "FIELD_NAME": crm_add_field_name(field_name),
            "USER_TYPE_ID": crm_field_type(spec),
            "XML_ID": field_name,
            "MULTIPLE": "Y" if spec.get("multiple") else "N",
            "MANDATORY": "N",
            "SHOW_FILTER": "Y",
            "EDIT_FORM_LABEL": {"ru": title, "en": title},
            "LIST_COLUMN_LABEL": {"ru": title, "en": title},
            "LIST_FILTER_LABEL": {"ru": title, "en": title},
        }
    }


def crm_existing_field_names(rows: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for row in rows:
        for key in ("FIELD_NAME", "fieldName", "field_name", "XML_ID", "xmlId"):
            value = str(row.get(key) or "").strip().upper()
            if value:
                result.add(value)
                if not value.startswith("UF_CRM_"):
                    result.add(f"UF_CRM_{value}")
    return result


def ensure_crm_sync_userfields(webhook_base: str) -> list[dict[str, Any]]:
    """Ensure CRM company/contact fields for guarded 1C supplier sync."""

    rows: list[dict[str, Any]] = []
    for entity in ("company", "contact"):
        list_method, add_method = CRM_USERFIELD_METHODS[entity]
        response = bitrix_setup.bitrix_call(webhook_base, list_method, {})
        current_rows = response.get("result") or []
        if isinstance(current_rows, dict):
            current_rows = current_rows.get("fields") or current_rows.get("items") or []
        existing_names = crm_existing_field_names(
            [item for item in current_rows if isinstance(item, dict)]
        )
        for spec in CRM_SYNC_FIELD_SPECS:
            if spec["entity"] != entity:
                continue
            field_name = str(spec["field_name"]).strip().upper()
            if field_name in existing_names:
                rows.append(
                    {
                        "entity": entity,
                        "field_name": field_name,
                        "title": spec["title"],
                        "type": crm_field_type(spec),
                        "action": "exists",
                    }
                )
                continue
            bitrix_setup.bitrix_call(webhook_base, add_method, crm_userfield_payload(spec))
            rows.append(
                {
                    "entity": entity,
                    "field_name": field_name,
                    "title": spec["title"],
                    "type": crm_field_type(spec),
                    "action": "created",
                }
            )
            existing_names.add(field_name)
    return rows


def crm_sync_field_map() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {"company": {}, "contact": {}}
    for spec in CRM_SYNC_FIELD_SPECS:
        entity = str(spec["entity"])
        logical_key = crm_add_field_name(str(spec["field_name"])).lower()
        result[entity][logical_key] = str(spec["field_name"]).strip().upper()
    return result


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


def ensure_procurement_type_flags(
    webhook_base: str,
    *,
    process_type: dict[str, Any],
    title: str,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, desired in PROCUREMENT_TYPE_REQUIRED_FLAGS.items():
        current = str(process_type.get(key) or "").strip().upper()
        desired_bool = bool(desired)
        if current != ("Y" if desired_bool else "N"):
            fields[key] = desired_bool
    if not fields:
        return process_type
    bitrix_setup.bitrix_call(
        webhook_base,
        "crm.type.update",
        {"id": int(process_type["id"]), "fields": fields},
    )
    return bitrix_setup.find_type_by_title(webhook_base, title) or {**process_type, **fields}


def desired_stage_specs(category_spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in category_spec["stages"]:
        rows.append({"semantics": None, **item})
    return rows


def move_terminal_stages_after_process_steps(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
    stage_specs: list[dict[str, Any]],
) -> None:
    """Bitrix refuses adding process stages after an existing success stage."""

    max_process_sort = max(
        (int(spec["sort"]) for spec in stage_specs if spec.get("semantics") is None),
        default=0,
    )
    terminal_sorts = {
        str(spec.get("semantics")): int(spec["sort"])
        for spec in stage_specs
        if spec.get("semantics") in {"S", "F"}
    }
    terminal_names = {
        str(spec.get("semantics")): str(spec["name"])
        for spec in stage_specs
        if spec.get("semantics") in {"S", "F"}
    }
    if not max_process_sort or not terminal_sorts:
        return
    stages = bitrix_setup.list_stages(
        webhook_base,
        entity_type_id=entity_type_id,
        category_id=category_id,
    )
    for stage in stages:
        semantics = str(stage.get("SEMANTICS") or "")
        target_sort = terminal_sorts.get(semantics)
        if not target_sort:
            continue
        current_sort = int(stage.get("SORT") or 0)
        if current_sort > max_process_sort:
            continue
        bitrix_setup.bitrix_call(
            webhook_base,
            "crm.status.update",
            {
                "id": stage["ID"],
                "fields": {
                    "NAME": terminal_names[semantics],
                    "SORT": target_sort,
                    "SEMANTICS": semantics,
                },
            },
        )


def precreate_missing_process_stages_before_success(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
    stage_specs: list[dict[str, Any]],
) -> None:
    """Create new process stages before success, then let ensure_stages sort them."""

    stages = bitrix_setup.list_stages(
        webhook_base,
        entity_type_id=entity_type_id,
        category_id=category_id,
    )
    status_ids = {str(stage.get("STATUS_ID") or "") for stage in stages}
    success_sorts = [
        int(stage.get("SORT") or 0) for stage in stages if stage.get("SEMANTICS") == "S"
    ]
    if not success_sorts:
        return
    success_sort = min(success_sorts)
    missing_after_success = []
    for spec in stage_specs:
        if spec.get("semantics") is not None or int(spec["sort"]) <= success_sort:
            continue
        status_id = f"DT{entity_type_id}_{category_id}:{spec['code']}"
        if status_id not in status_ids:
            missing_after_success.append((spec, status_id))
    if not missing_after_success:
        return
    entity_id = f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"
    start_sort = max(1, success_sort - len(missing_after_success))
    for index, (spec, status_id) in enumerate(missing_after_success):
        bitrix_setup.bitrix_call(
            webhook_base,
            "crm.status.add",
            {
                "fields": {
                    "ENTITY_ID": entity_id,
                    "STATUS_ID": status_id,
                    "NAME": spec["name"],
                    "SORT": start_sort + index,
                }
            },
        )


def list_stage_item_ids(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
    stage_id: str,
) -> list[int]:
    item_ids: list[int] = []
    start: int | None = 0
    while True:
        params: dict[str, Any] = {
            "entityTypeId": entity_type_id,
            "filter": {"categoryId": category_id, "stageId": stage_id},
            "select": ["id"],
        }
        if start is not None:
            params["start"] = start
        response = bitrix_setup.bitrix_call(webhook_base, "crm.item.list", params)
        result = response.get("result") or {}
        items = result.get("items") or []
        for item in items:
            try:
                item_ids.append(int(item.get("id")))
            except (TypeError, ValueError):
                continue
        next_start = response.get("next")
        if next_start is None or not items:
            break
        start = int(next_start)
    return item_ids


def merge_and_delete_obsolete_stages(
    webhook_base: str,
    *,
    entity_type_id: int,
    category_id: int,
    category_key: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    merges = OBSOLETE_STAGE_MERGES.get(category_key) or []
    if not merges:
        return actions
    stages = bitrix_setup.list_stages(
        webhook_base,
        entity_type_id=entity_type_id,
        category_id=category_id,
    )
    for merge in merges:
        target_stage_id = f"DT{entity_type_id}_{category_id}:{merge['target_code']}"
        old_codes = list(merge.get("old_codes") or [])
        if merge.get("old_code"):
            old_codes.append(str(merge["old_code"]))
        old_stage_ids = {f"DT{entity_type_id}_{category_id}:{old_code}" for old_code in old_codes}
        old_names = {
            str(name).strip() for name in merge.get("old_names") or [] if str(name).strip()
        }
        old_stages: list[dict[str, Any]] = []
        for stage in stages:
            stage_id = str(stage.get("STATUS_ID") or "")
            stage_name = str(stage.get("NAME") or "").strip()
            if stage_id == target_stage_id:
                continue
            if stage_id in old_stage_ids or (old_names and stage_name in old_names):
                old_stages.append(stage)
        for old_stage in old_stages:
            old_stage_id = str(old_stage.get("STATUS_ID") or "")
            item_ids = list_stage_item_ids(
                webhook_base,
                entity_type_id=entity_type_id,
                category_id=category_id,
                stage_id=old_stage_id,
            )
            for item_id in item_ids:
                bitrix_setup.bitrix_call(
                    webhook_base,
                    "crm.item.update",
                    {
                        "entityTypeId": entity_type_id,
                        "id": item_id,
                        "fields": {"stageId": target_stage_id},
                    },
                )
            bitrix_setup.bitrix_call(
                webhook_base,
                "crm.status.delete",
                {"id": old_stage["ID"], "FORCED": "Y"},
            )
            actions.append(
                {
                    "old_stage_id": old_stage_id,
                    "target_stage_id": target_stage_id,
                    "moved_items": len(item_ids),
                    "reason": merge["reason"],
                }
            )
    return actions


def obsolete_stage_merge_plan(
    stages: list[dict[str, Any]],
    *,
    entity_type_id: int,
    category_id: int,
    category_key: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for merge in OBSOLETE_STAGE_MERGES.get(category_key) or []:
        target_stage_id = f"DT{entity_type_id}_{category_id}:{merge['target_code']}"
        old_codes = list(merge.get("old_codes") or [])
        if merge.get("old_code"):
            old_codes.append(str(merge["old_code"]))
        old_stage_ids = {f"DT{entity_type_id}_{category_id}:{old_code}" for old_code in old_codes}
        old_names = {
            str(name).strip() for name in merge.get("old_names") or [] if str(name).strip()
        }
        for stage in stages:
            stage_id = str(stage.get("STATUS_ID") or "")
            stage_name = str(stage.get("NAME") or "").strip()
            if stage_id == target_stage_id:
                continue
            if stage_id in old_stage_ids or (old_names and stage_name in old_names):
                rows.append(
                    {
                        "old_stage_id": stage_id,
                        "old_stage_name": stage_name,
                        "target_stage_id": target_stage_id,
                        "action": "merge_then_delete",
                        "reason": merge["reason"],
                    }
                )
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
    match_names = category_match_names(category_spec)
    existing = next(
        (item for item in categories if str(item.get("name") or "") in match_names), None
    )
    if existing is None and category_spec.get("reuse_default"):
        existing = next(
            (item for item in categories if str(item.get("isDefault") or "") == "Y"), None
        )
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
    match_names = category_match_names(category_spec)
    existing = next(
        (item for item in categories if str(item.get("name") or "") in match_names), None
    )
    source = "name"
    if existing is not None and str(existing.get("name") or "") != name:
        source = "alias"
    if existing is None and category_spec.get("reuse_default"):
        existing = next(
            (item for item in categories if str(item.get("isDefault") or "") == "Y"), None
        )
        source = "default" if existing else "missing"
    action = "add" if existing is None else "update"
    if (
        existing is not None
        and str(existing.get("name") or "") == name
        and int(existing.get("sort") or 0) == int(category_spec["sort"])
    ):
        action = "keep"
    return {
        "logical_key": category_spec["logical_key"],
        "name": name,
        "sort": category_spec["sort"],
        "action": action,
        "matched_by": source,
        "existing": (
            None
            if existing is None
            else {
                "id": existing.get("id"),
                "name": existing.get("name"),
                "sort": existing.get("sort"),
                "isDefault": existing.get("isDefault"),
            }
        ),
    }


def category_match_names(category_spec: dict[str, Any]) -> set[str]:
    return {
        str(value).strip()
        for value in [category_spec.get("name"), *(category_spec.get("aliases") or [])]
        if str(value or "").strip()
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
                "existing": (
                    None
                    if current is None
                    else {
                        "ID": current.get("ID"),
                        "STATUS_ID": current.get("STATUS_ID"),
                        "NAME": current.get("NAME"),
                        "SORT": current.get("SORT"),
                        "SEMANTICS": current.get("SEMANTICS"),
                    }
                ),
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
                "existing": (
                    None
                    if current is None
                    else {
                        "id": current.get("id"),
                        "fieldName": current.get("fieldName"),
                        "xmlId": current.get("xmlId"),
                        "userTypeId": current.get("userTypeId"),
                    }
                ),
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


def enum_map_for_spec(spec: dict[str, Any], field: dict[str, Any]) -> dict[str, str]:
    current_by_value = {
        str(item.get("value") or item.get("VALUE") or "").strip(): item
        for item in field.get("enum") or []
    }
    result: dict[str, str] = {}
    for option in desired_enum_options(spec):
        current = current_by_value.get(option["value"])
        if current is None:
            source_option = next(
                (
                    item
                    for item in spec.get("enum") or []
                    if str(item.get("xml_id") or item.get("xmlId") or "") == option["xmlId"]
                ),
                {},
            )
            for alias in source_option.get("aliases") or []:
                current = current_by_value.get(str(alias).strip())
                if current is not None:
                    break
        enum_id = (current or {}).get("id") or (current or {}).get("ID")
        if enum_id:
            result[option["xmlId"]] = str(enum_id)
    return result


def field_with_enum_options(webhook_base: str, current: dict[str, Any]) -> dict[str, Any]:
    if current.get("enum") or not current.get("id"):
        return current
    response = bitrix_setup.bitrix_call(
        webhook_base,
        "userfieldconfig.get",
        {"moduleId": "crm", "id": current["id"]},
    )
    return (response.get("result") or {}).get("field") or current


def enum_options_for_update(spec: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    current_by_value = {
        str(item.get("value") or item.get("VALUE") or "").strip(): item
        for item in current.get("enum") or []
    }
    rows: list[dict[str, Any]] = []
    for source_option, option in zip(
        spec.get("enum") or [], desired_enum_options(spec), strict=True
    ):
        existing = current_by_value.get(option["value"])
        if existing is None:
            for alias in source_option.get("aliases") or []:
                existing = current_by_value.get(str(alias).strip())
                if existing is not None:
                    break
        if existing:
            enum_id = existing.get("id") or existing.get("ID")
            if enum_id:
                option["id"] = str(enum_id)
            option["xmlId"] = str(
                existing.get("xmlId") or existing.get("XML_ID") or option["xmlId"]
            )
        rows.append(option)
    return rows


def build_contour_contract(
    *,
    categories: dict[str, dict[str, Any]],
    category_stages: dict[str, dict[str, dict[str, Any]]],
    procurement_contour_enum_map: dict[str, str],
) -> dict[str, Any]:
    contours: dict[str, dict[str, Any]] = {}
    for item in CONTOUR_CONTRACT:
        logical_key = str(item["logical_key"])
        category = categories.get(logical_key) or {}
        stages = category_stages.get(logical_key) or {}
        default_stage = stages.get("supplier_order") or next(iter(stages.values()), {})
        contours[logical_key] = {
            "onec_requisite": ONEC_CONTOUR_REQUISITE,
            "onec_values": list(item["onec_values"]),
            "bitrix_value": item["bitrix_value"],
            "bitrix_enum_xml_id": logical_key,
            "bitrix_enum_id": procurement_contour_enum_map.get(logical_key),
            "category_key": logical_key,
            "category_id": int(category["id"]) if category.get("id") else None,
            "initial_stage_key": "supplier_order",
            "initial_stage_id": str(
                default_stage.get("STATUS_ID") or default_stage.get("statusId") or ""
            ),
        }
    return {
        "onec_document": "ЗаказПоставщику",
        "pre_onec_process": "Формирование заказа",
        "legacy_need_stage_policy": "system_new_stage_is_renamed_to_supplier_order",
        "onec_requisite": ONEC_CONTOUR_REQUISITE,
        "blank_value_policy": "foreign_currency_cargo_rub_ordinary_otherwise_open_supplier_order",
        "blank_cargo_dropoff_date_policy": "cargo",
        "blank_foreign_currency_policy": "cargo",
        "blank_rub_currency_policy": "ordinary",
        "blank_open_supplier_order_policy": "cargo",
        "unknown_value_policy": "block_import",
        "onec_date_fields": {
            "supplier_dispatch_date": "Отправка постав.",
            "cargo_dropoff_date": "Сдача в карго",
            "expected_receipt_date": "Поступление",
        },
        "contours": contours,
    }


def bitrix_contour_payload_for_onec_value(
    value: Any,
    *,
    mapping: dict[str, Any],
    initial_stage_key: str = "supplier_order",
    is_open_supplier_order: bool = False,
    currency: Any = None,
    has_cargo_dropoff: bool = False,
) -> dict[str, Any]:
    """Return category/stage/enum payload for a 1C procurement contour value."""

    logical_key = normalize_onec_procurement_contour(
        value,
        is_open_supplier_order=is_open_supplier_order,
        currency=currency,
        has_cargo_dropoff=has_cargo_dropoff,
    )
    category = (mapping.get("category_map") or {}).get(logical_key) or {}
    stage_map = (mapping.get("stage_map") or {}).get(logical_key) or {}
    field_map = mapping.get("field_map") or {}
    enum_map = (mapping.get("enum_map") or {}).get("procurement_contour") or {}

    category_id = category.get("id")
    stage_id = stage_map.get(initial_stage_key)
    field_name = field_map.get("procurement_contour")
    enum_id = enum_map.get(logical_key)
    missing = [
        name
        for name, current in [
            ("category_map", category_id),
            ("stage_map", stage_id),
            ("field_map.procurement_contour", field_name),
            ("enum_map.procurement_contour", enum_id),
        ]
        if not current
    ]
    if missing:
        raise KeyError(
            f"Procurement contour {logical_key!r} is not fully mapped for Bitrix: "
            + ", ".join(missing)
        )

    return {
        "logical_key": logical_key,
        "onec_requisite": ONEC_CONTOUR_REQUISITE,
        "category_id": int(category_id),
        "stage_id": str(stage_id),
        "enum_id": str(enum_id),
        "fields": {
            "categoryId": int(category_id),
            "stageId": str(stage_id),
            str(field_name): str(enum_id),
        },
    }


def ensure_procurement_custom_fields(
    webhook_base: str,
    *,
    process_type: dict[str, Any],
    update_existing: bool = True,
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
                "editFormLabel": {"ru": title},
                "listColumnLabel": {"ru": title},
                "listFilterLabel": {"ru": title},
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
        elif not update_existing:
            action = "exists"
            rows.append(
                {
                    "logical_key": spec["logical_key"],
                    "title": title,
                    "field_name": current.get("fieldName") or field_name,
                    "field_id": current.get("id"),
                    "xml_id": xml_id,
                    "enum_map": (
                        enum_map_for_spec(spec, current) if user_type_id == "enumeration" else {}
                    ),
                    "action": action,
                }
            )
            continue
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
                "enum_map": (
                    enum_map_for_spec(spec, current) if user_type_id == "enumeration" else {}
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
        existing_by_name[refreshed["fieldName"]] = refreshed
        existing_by_xml_id[xml_id] = refreshed

    return rows


def smart_process_field_xml_id(prefix: str, spec: dict[str, Any]) -> str:
    return f"{prefix}_{bitrix_setup._slug_suffix(spec['logical_key'])}"


def ensure_smart_process_fields(
    webhook_base: str,
    *,
    process_type: dict[str, Any],
    field_specs: list[dict[str, Any]],
    xml_prefix: str,
    update_existing: bool = True,
) -> list[dict[str, Any]]:
    type_id = int(process_type["id"])
    entity_id = bitrix_setup.smart_process_userfield_entity_id(type_id)
    existing_fields = bitrix_setup.list_userfields(webhook_base, entity_id=entity_id)
    existing_by_name = {str(item.get("fieldName") or ""): item for item in existing_fields}
    existing_by_xml_id = {
        str(item.get("xmlId") or ""): item for item in existing_fields if item.get("xmlId")
    }
    rows: list[dict[str, Any]] = []

    for index, spec in enumerate(field_specs, start=1):
        title = str(spec["title"])
        field_name = bitrix_setup._field_name_for_spec(entity_id, spec)
        xml_id = smart_process_field_xml_id(xml_prefix, spec)
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
                "editFormLabel": {"ru": title},
                "listColumnLabel": {"ru": title},
                "listFilterLabel": {"ru": title},
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
        elif not update_existing:
            action = "exists"
            rows.append(
                {
                    "logical_key": spec["logical_key"],
                    "title": title,
                    "field_name": current.get("fieldName") or field_name,
                    "field_id": current.get("id"),
                    "xml_id": xml_id,
                    "enum_map": (
                        enum_map_for_spec(spec, current) if user_type_id == "enumeration" else {}
                    ),
                    "action": action,
                }
            )
            continue
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
            {"moduleId": "crm", "id": current["id"], "field": update_field},
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
                "enum_map": (
                    enum_map_for_spec(spec, current) if user_type_id == "enumeration" else {}
                ),
                "action": action,
            }
        )
    return rows


def ensure_ved_supplier_passport_fields(
    webhook_base: str,
    *,
    update_existing: bool = True,
) -> dict[str, Any]:
    process_type = bitrix_setup.find_type_by_title(webhook_base, VED_SUPPLIER_PROCESS_TITLE)
    if process_type is None:
        return {"process": {"title": VED_SUPPLIER_PROCESS_TITLE, "action": "missing"}, "fields": []}
    return {
        "process": {
            "title": process_type.get("title"),
            "type_id": int(process_type["id"]),
            "entity_type_id": int(process_type["entityTypeId"]),
            "action": "update",
        },
        "fields": ensure_smart_process_fields(
            webhook_base,
            process_type=process_type,
            field_specs=VED_SUPPLIER_PASSPORT_FIELD_SPECS,
            xml_prefix="UF_CRM_VED_SUPPLIER",
            update_existing=update_existing,
        ),
    }


def crm_item_fields_by_title(webhook_base: str, *, entity_type_id: int) -> dict[str, str]:
    response = bitrix_setup.bitrix_call(
        webhook_base,
        "crm.item.fields",
        {"entityTypeId": entity_type_id},
    )
    result = response.get("result") or {}
    fields = result.get("fields") if isinstance(result, dict) else {}
    if not isinstance(fields, dict):
        return {}
    rows: dict[str, str] = {}
    for field_name, field in fields.items():
        if not isinstance(field, dict):
            continue
        titles = [
            field.get("title"),
            field.get("formLabel"),
            field.get("listLabel"),
            field.get("filterLabel"),
        ]
        labels = field.get("labels")
        if isinstance(labels, dict):
            titles.extend(labels.values())
        for title in titles:
            title_value = str(title or "").strip()
            if title_value and title_value not in rows:
                rows[title_value] = str(field_name)
    return rows


def field_map_from_titles(
    fields_by_title: dict[str, str],
    aliases: dict[str, tuple[str, ...]],
    ensured_fields: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    field_map: dict[str, str] = {}
    for row in ensured_fields or []:
        logical_key = str(row.get("logical_key") or "").strip()
        field_name = str(row.get("field_name") or "").strip()
        if logical_key and field_name:
            field_map[logical_key] = field_name
    for logical_key, titles in aliases.items():
        if field_map.get(logical_key):
            continue
        for title in titles:
            field_name = fields_by_title.get(title)
            if field_name:
                field_map[logical_key] = field_name
                break
    return field_map


def ensure_certificate_process_fields(
    webhook_base: str,
    *,
    update_existing: bool = True,
) -> dict[str, Any]:
    process_type = bitrix_setup.find_type_by_title(webhook_base, CERTIFICATE_PROCESS_TITLE)
    if process_type is None:
        return {
            "process": {"title": CERTIFICATE_PROCESS_TITLE, "action": "missing"},
            "fields": [],
            "field_map": {},
            "enum_map": {},
        }
    fields = ensure_smart_process_fields(
        webhook_base,
        process_type=process_type,
        field_specs=CERTIFICATE_FIELD_SPECS,
        xml_prefix="UF_CRM_CERTIFICATE",
        update_existing=update_existing,
    )
    fields_by_title = crm_item_fields_by_title(
        webhook_base,
        entity_type_id=int(process_type["entityTypeId"]),
    )
    return {
        "process": {
            "title": process_type.get("title"),
            "type_id": int(process_type["id"]),
            "entity_type_id": int(process_type["entityTypeId"]),
            "action": "update",
        },
        "fields": fields,
        "field_map": field_map_from_titles(
            fields_by_title,
            CERTIFICATE_TITLE_ALIASES,
            fields,
        ),
        "enum_map": {row["logical_key"]: row["enum_map"] for row in fields if row.get("enum_map")},
    }


def ensure_product_passport_process_fields(
    webhook_base: str,
    *,
    update_existing: bool = True,
) -> dict[str, Any]:
    process_type = bitrix_setup.find_type_by_title(webhook_base, PRODUCT_PASSPORT_PROCESS_TITLE)
    if process_type is None:
        return {
            "process": {"title": PRODUCT_PASSPORT_PROCESS_TITLE, "action": "missing"},
            "fields": [],
            "field_map": {},
            "enum_map": {},
        }
    fields = ensure_smart_process_fields(
        webhook_base,
        process_type=process_type,
        field_specs=PRODUCT_PASSPORT_FIELD_SPECS,
        xml_prefix="UF_CRM_PRODUCT_PASSPORT",
        update_existing=update_existing,
    )
    fields_by_title = crm_item_fields_by_title(
        webhook_base,
        entity_type_id=int(process_type["entityTypeId"]),
    )
    return {
        "process": {
            "title": process_type.get("title"),
            "type_id": int(process_type["id"]),
            "entity_type_id": int(process_type["entityTypeId"]),
            "action": "update",
        },
        "fields": fields,
        "field_map": field_map_from_titles(
            fields_by_title,
            PRODUCT_PASSPORT_TITLE_ALIASES,
            fields,
        ),
        "enum_map": {row["logical_key"]: row["enum_map"] for row in fields if row.get("enum_map")},
    }


def smart_process_field_plan(
    webhook_base: str,
    *,
    process_type: dict[str, Any] | None,
    field_specs: list[dict[str, Any]],
    xml_prefix: str,
) -> list[dict[str, Any]]:
    if process_type is None:
        return [
            {
                "logical_key": spec["logical_key"],
                "title": spec["title"],
                "action": "add_after_process_exists",
            }
            for spec in field_specs
        ]
    entity_id = bitrix_setup.smart_process_userfield_entity_id(int(process_type["id"]))
    fields = bitrix_setup.list_userfields(webhook_base, entity_id=entity_id)
    by_name = {str(item.get("fieldName") or ""): item for item in fields}
    by_xml_id = {str(item.get("xmlId") or ""): item for item in fields if item.get("xmlId")}
    rows: list[dict[str, Any]] = []
    for spec in field_specs:
        field_name = bitrix_setup._field_name_for_spec(entity_id, spec)
        xml_id = smart_process_field_xml_id(xml_prefix, spec)
        current = by_xml_id.get(xml_id) or by_name.get(field_name)
        rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": spec["title"],
                "type": spec["type"],
                "xml_id": xml_id,
                "field_name": field_name,
                "action": "add" if current is None else "update",
            }
        )
    return rows


def discover_process_fields(
    webhook_base: str,
    *,
    process_type: dict[str, Any],
    field_specs: list[dict[str, Any]],
    xml_prefix: str | None = None,
) -> list[dict[str, Any]]:
    entity_id = bitrix_setup.smart_process_userfield_entity_id(int(process_type["id"]))
    fields = bitrix_setup.list_userfields(webhook_base, entity_id=entity_id)
    by_name = {str(item.get("fieldName") or ""): item for item in fields}
    by_xml_id = {str(item.get("xmlId") or ""): item for item in fields if item.get("xmlId")}
    rows: list[dict[str, Any]] = []
    for spec in field_specs:
        field_name = bitrix_setup._field_name_for_spec(entity_id, spec)
        xml_id = (
            smart_process_field_xml_id(xml_prefix, spec)
            if xml_prefix
            else field_xml_id_for_spec(spec)
        )
        current = by_xml_id.get(xml_id) or by_name.get(field_name)
        user_type_id, _ = field_config_for_spec(spec)
        if current and user_type_id == "enumeration":
            current = field_with_enum_options(webhook_base, current)
        rows.append(
            {
                "logical_key": spec["logical_key"],
                "title": spec["title"],
                "field_name": (current or {}).get("fieldName") or field_name,
                "field_id": (current or {}).get("id"),
                "xml_id": xml_id,
                "enum_map": (
                    enum_map_for_spec(spec, current or {}) if user_type_id == "enumeration" else {}
                ),
                "action": "found" if current else "missing",
            }
        )
    return rows


def patch_mapping_process_fields(
    mapping: dict[str, Any],
    *,
    process_type: dict[str, Any],
    custom_fields: list[dict[str, Any]],
    certificate_process: dict[str, Any],
    product_passport_process: dict[str, Any],
) -> dict[str, Any]:
    field_map = dict(mapping.get("field_map") or BUILTIN_FIELD_MAPPING)
    for row in custom_fields:
        if row.get("field_name"):
            field_map[str(row["logical_key"])] = str(row["field_name"])
    enum_map = dict(mapping.get("enum_map") or {})
    for row in custom_fields:
        if row.get("enum_map"):
            enum_map[str(row["logical_key"])] = row["enum_map"]
    mapping["generated_at"] = datetime.now(timezone.utc).isoformat()
    mapping["process"] = {
        "title": process_type.get("title"),
        "code": process_type.get("code"),
        "type_id": int(process_type["id"]),
        "entity_type_id": int(process_type["entityTypeId"]),
    }
    mapping["field_map"] = field_map
    mapping["enum_map"] = enum_map
    env = dict(mapping.get("env") or {})
    env["PROCUREMENT_BITRIX_ENTITY_TYPE_ID"] = int(process_type["entityTypeId"])
    env["PROCUREMENT_BITRIX_FIELD_MAP"] = json.dumps(field_map, ensure_ascii=False)
    env["PROCUREMENT_BITRIX_ENUM_MAP"] = json.dumps(enum_map, ensure_ascii=False)
    mapping["env"] = env
    mapping["certificate_process"] = {
        "entity_type_id": (certificate_process.get("process") or {}).get("entity_type_id"),
        "field_map": certificate_process.get("field_map") or {},
        "enum_map": certificate_process.get("enum_map") or {},
        "fields": certificate_process.get("fields") or [],
    }
    mapping["product_passport_process"] = {
        "entity_type_id": (product_passport_process.get("process") or {}).get("entity_type_id"),
        "field_map": product_passport_process.get("field_map") or {},
        "enum_map": product_passport_process.get("enum_map") or {},
        "fields": product_passport_process.get("fields") or [],
    }
    return mapping


def ensure_procurement_details_configurations(
    webhook_base: str,
    *,
    mapping: dict[str, Any],
    details_config_path: Path,
) -> dict[str, str]:
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    if not entity_type_id:
        return {}
    details_paths = dict(mapping.get("details_configuration_paths") or {})
    for logical_key, category in (mapping.get("category_map") or {}).items():
        category_id = int((category or {}).get("id") or 0)
        if not category_id:
            continue
        details_mapping = {
            "process": {
                "entity_type_id": entity_type_id,
                "category_id": category_id,
            },
            "fields": mapping.get("field_map") or {},
        }
        path = details_config_path.with_name(
            f"{details_config_path.stem}_{logical_key}{details_config_path.suffix}"
        )
        _, saved_path = bitrix_setup.ensure_common_details_configuration(
            webhook_base,
            mapping=details_mapping,
            path=path,
        )
        details_paths[str(logical_key)] = str(saved_path)
    return details_paths


def apply_fields_only_setup(
    webhook_base: str,
    *,
    title: str,
    mapping_path: Path,
    details_config_path: Path,
    skip_details_config: bool = False,
) -> dict[str, Any]:
    configure_generic_setup()
    process_type = bitrix_setup.find_type_by_title(webhook_base, title)
    if process_type is None:
        raise RuntimeError(f"Bitrix process {title!r} is not found")
    ensure_procurement_custom_fields(
        webhook_base,
        process_type=process_type,
        update_existing=False,
    )
    certificate_process = ensure_certificate_process_fields(
        webhook_base,
        update_existing=False,
    )
    product_passport_process = ensure_product_passport_process_fields(
        webhook_base,
        update_existing=False,
    )
    custom_fields = discover_process_fields(
        webhook_base,
        process_type=process_type,
        field_specs=CUSTOM_FIELD_SPECS,
    )
    mapping = {}
    if mapping_path.exists():
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            mapping = {}
    mapping = patch_mapping_process_fields(
        mapping,
        process_type=process_type,
        custom_fields=custom_fields,
        certificate_process=certificate_process,
        product_passport_process=product_passport_process,
    )
    if not skip_details_config:
        mapping["details_configuration_paths"] = ensure_procurement_details_configurations(
            webhook_base,
            mapping=mapping,
            details_config_path=details_config_path,
        )
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapping


def build_read_only_plan(
    webhook_base: str,
    *,
    title: str,
) -> dict[str, Any]:
    process_type = bitrix_setup.find_type_by_title(webhook_base, title)
    ved_supplier_type = bitrix_setup.find_type_by_title(webhook_base, VED_SUPPLIER_PROCESS_TITLE)
    certificate_type = bitrix_setup.find_type_by_title(webhook_base, CERTIFICATE_PROCESS_TITLE)
    product_passport_type = bitrix_setup.find_type_by_title(
        webhook_base,
        PRODUCT_PASSPORT_PROCESS_TITLE,
    )
    if process_type is None:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "process": {"title": title, "action": "add"},
            "categories": [category_plan([], category_spec=item) for item in CATEGORY_SPECS],
            "fields": [],
            "stages": {},
            "crm_sync_fields": CRM_SYNC_FIELD_SPECS,
            "ved_supplier_passport": {
                "process": {
                    "title": VED_SUPPLIER_PROCESS_TITLE,
                    "action": "missing" if ved_supplier_type is None else "update",
                },
                "fields": smart_process_field_plan(
                    webhook_base,
                    process_type=ved_supplier_type,
                    field_specs=VED_SUPPLIER_PASSPORT_FIELD_SPECS,
                    xml_prefix="UF_CRM_VED_SUPPLIER",
                ),
            },
            "certificate_process": {
                "process": {
                    "title": CERTIFICATE_PROCESS_TITLE,
                    "action": "missing" if certificate_type is None else "update",
                },
                "fields": smart_process_field_plan(
                    webhook_base,
                    process_type=certificate_type,
                    field_specs=CERTIFICATE_FIELD_SPECS,
                    xml_prefix="UF_CRM_CERTIFICATE",
                ),
            },
            "product_passport_process": {
                "process": {
                    "title": PRODUCT_PASSPORT_PROCESS_TITLE,
                    "action": "missing" if product_passport_type is None else "update",
                },
                "fields": smart_process_field_plan(
                    webhook_base,
                    process_type=product_passport_type,
                    field_specs=PRODUCT_PASSPORT_FIELD_SPECS,
                    xml_prefix="UF_CRM_PRODUCT_PASSPORT",
                ),
            },
        }

    entity_type_id = int(process_type["entityTypeId"])
    categories = list_categories(webhook_base, entity_type_id=entity_type_id)
    category_plans = [category_plan(categories, category_spec=item) for item in CATEGORY_SPECS]
    stage_plans: dict[str, list[dict[str, Any]]] = {}
    obsolete_stage_plans: dict[str, list[dict[str, Any]]] = {}
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
            obsolete_stage_plans[spec["logical_key"]] = []
            continue
        category_id = int(existing["id"])
        stages = bitrix_setup.list_stages(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
        )
        obsolete_stage_plans[spec["logical_key"]] = obsolete_stage_merge_plan(
            stages,
            entity_type_id=entity_type_id,
            category_id=category_id,
            category_key=spec["logical_key"],
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
        "obsolete_stage_merges": obsolete_stage_plans,
        "crm_sync_fields": CRM_SYNC_FIELD_SPECS,
        "ved_supplier_passport": {
            "process": {
                "title": VED_SUPPLIER_PROCESS_TITLE,
                "action": "missing" if ved_supplier_type is None else "update",
            },
            "fields": smart_process_field_plan(
                webhook_base,
                process_type=ved_supplier_type,
                field_specs=VED_SUPPLIER_PASSPORT_FIELD_SPECS,
                xml_prefix="UF_CRM_VED_SUPPLIER",
            ),
        },
        "certificate_process": {
            "process": {
                "title": CERTIFICATE_PROCESS_TITLE,
                "action": "missing" if certificate_type is None else "update",
            },
            "fields": smart_process_field_plan(
                webhook_base,
                process_type=certificate_type,
                field_specs=CERTIFICATE_FIELD_SPECS,
                xml_prefix="UF_CRM_CERTIFICATE",
            ),
        },
        "product_passport_process": {
            "process": {
                "title": PRODUCT_PASSPORT_PROCESS_TITLE,
                "action": "missing" if product_passport_type is None else "update",
            },
            "fields": smart_process_field_plan(
                webhook_base,
                process_type=product_passport_type,
                field_specs=PRODUCT_PASSPORT_FIELD_SPECS,
                xml_prefix="UF_CRM_PRODUCT_PASSPORT",
            ),
        },
    }


def build_mapping(
    *,
    process_type: dict[str, Any],
    categories: dict[str, dict[str, Any]],
    category_stages: dict[str, dict[str, dict[str, Any]]],
    custom_fields: list[dict[str, Any]],
    certificate_process: dict[str, Any],
    product_passport_process: dict[str, Any],
    details_paths: dict[str, str],
) -> dict[str, Any]:
    field_map = dict(BUILTIN_FIELD_MAPPING)
    for row in custom_fields:
        field_map[row["logical_key"]] = row["field_name"]
    enum_map = {row["logical_key"]: row["enum_map"] for row in custom_fields if row.get("enum_map")}
    contour_contract = build_contour_contract(
        categories=categories,
        category_stages=category_stages,
        procurement_contour_enum_map=enum_map.get("procurement_contour") or {},
    )
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
        "certificate_process": {
            "entity_type_id": (certificate_process.get("process") or {}).get("entity_type_id"),
            "field_map": certificate_process.get("field_map") or {},
            "enum_map": certificate_process.get("enum_map") or {},
        },
        "product_passport_process": {
            "entity_type_id": (product_passport_process.get("process") or {}).get("entity_type_id"),
            "field_map": product_passport_process.get("field_map") or {},
            "enum_map": product_passport_process.get("enum_map") or {},
        },
        "onec_contour_contract": contour_contract,
        "crm_supplier_sync_contract": {
            "originator_id": CRM_SUPPLIER_ORIGINATOR_ID,
            "source_system": "1c_ut103",
            "company_is_master_profile": True,
            "safe_match_order": [
                "onec_ref",
                "registration_or_tax_number",
                "normalized_title",
                "phone_or_email",
            ],
            "no_match_policy": "create_crm_company",
            "single_match_policy": "fill_empty_fields_only",
            "multiple_match_policy": "manual_review_no_duplicate",
            "manual_crm_data_policy": "do_not_overwrite_non_empty_values",
            "crm_field_map": crm_sync_field_map(),
            "procurement_field_map": {
                "supplier_company": field_map.get("supplier_company"),
                "supplier_onec_ref": field_map.get("supplier_onec_ref"),
                "supplier_resolution_status": field_map.get("supplier_resolution_status"),
                "supplier_resolution_basis": field_map.get("supplier_resolution_basis"),
                "supplier_conflicts": field_map.get("supplier_conflicts"),
                "blocker_comment": field_map.get("blocker_comment"),
            },
            "procurement_supplier_status_enum": enum_map.get("supplier_resolution_status") or {},
        },
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
            "PROCUREMENT_BITRIX_ONEC_CONTOUR_CONTRACT": json.dumps(
                contour_contract,
                ensure_ascii=False,
            ),
            "PROCUREMENT_BITRIX_CRM_SUPPLIER_SYNC_CONTRACT": json.dumps(
                {
                    "originator_id": CRM_SUPPLIER_ORIGINATOR_ID,
                    "crm_field_map": crm_sync_field_map(),
                    "procurement_field_map": {
                        "supplier_company": field_map.get("supplier_company"),
                        "supplier_onec_ref": field_map.get("supplier_onec_ref"),
                        "supplier_resolution_status": field_map.get("supplier_resolution_status"),
                        "supplier_resolution_basis": field_map.get("supplier_resolution_basis"),
                        "supplier_conflicts": field_map.get("supplier_conflicts"),
                        "blocker_comment": field_map.get("blocker_comment"),
                    },
                    "procurement_supplier_status_enum": enum_map.get("supplier_resolution_status")
                    or {},
                },
                ensure_ascii=False,
            ),
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
    fast_field_create: bool,
) -> dict[str, Any]:
    configure_generic_setup()
    process_type = bitrix_setup.ensure_type(webhook_base, title=title, code=code)
    process_type = ensure_procurement_type_flags(
        webhook_base,
        process_type=process_type,
        title=title,
    )
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
        stage_specs = desired_stage_specs(category_spec)
        bitrix_setup.STAGE_SPECS = stage_specs
        category_id = int(category["id"])
        merge_and_delete_obsolete_stages(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
            category_key=logical_key,
        )
        precreate_missing_process_stages_before_success(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
            stage_specs=stage_specs,
        )
        move_terminal_stages_after_process_steps(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
            stage_specs=stage_specs,
        )
        category_stages[logical_key] = bitrix_setup.ensure_stages(
            webhook_base,
            entity_type_id=entity_type_id,
            category_id=category_id,
        )

    custom_fields = ensure_procurement_custom_fields(
        webhook_base,
        process_type=process_type,
        update_existing=not fast_field_create,
    )
    crm_sync_fields = ensure_crm_sync_userfields(webhook_base)
    ved_supplier_passport = ensure_ved_supplier_passport_fields(
        webhook_base,
        update_existing=not fast_field_create,
    )
    certificate_process = ensure_certificate_process_fields(
        webhook_base,
        update_existing=not fast_field_create,
    )
    product_passport_process = ensure_product_passport_process_fields(
        webhook_base,
        update_existing=not fast_field_create,
    )
    mapping = build_mapping(
        process_type=process_type,
        categories=categories,
        category_stages=category_stages,
        custom_fields=custom_fields,
        certificate_process=certificate_process,
        product_passport_process=product_passport_process,
        details_paths=details_paths,
    )
    mapping["crm_supplier_sync_fields"] = crm_sync_fields
    mapping["ved_supplier_passport"] = ved_supplier_passport
    mapping["certificate_process"]["fields"] = certificate_process.get("fields") or []
    mapping["product_passport_process"]["fields"] = product_passport_process.get("fields") or []

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
    parser.add_argument(
        "--fast-field-create",
        action="store_true",
        help="Create only missing fields and do not refresh/update existing field configs.",
    )
    parser.add_argument(
        "--fields-only",
        action="store_true",
        help="Only ensure procurement label/certificate fields and rewrite mapping.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Update Bitrix. Default is read-only plan."
    )
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
    if args.apply and args.fields_only:
        mapping = apply_fields_only_setup(
            webhook_base,
            title=args.title,
            mapping_path=args.mapping_path,
            details_config_path=args.details_config_path,
            skip_details_config=args.skip_details_config,
        )
        result = {
            "mode": "apply-fields-only",
            "mapping_path": str(args.mapping_path),
            "mapping": mapping,
        }
    elif args.apply:
        mapping = apply_setup(
            webhook_base,
            title=args.title,
            code=args.code,
            mapping_path=args.mapping_path,
            details_config_path=args.details_config_path,
            skip_details_config=args.skip_details_config,
            fast_field_create=args.fast_field_create,
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
