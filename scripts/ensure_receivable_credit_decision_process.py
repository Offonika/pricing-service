#!/usr/bin/env python3
"""Preview or explicitly create task #2494 metadata without enabling its worker."""

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

DEFAULT_PROCESS_TITLE = "Кредитное решение"
DEFAULT_PROCESS_CODE = "receivable_decision"
DEFAULT_CATEGORY_NAME = "Кредитные условия"
DEFAULT_MAPPING_PATH = REPO_ROOT / "build/bitrix/receivable_credit_decision_mapping.json"
DEFAULT_DETAILS_CONFIG_PATH = (
    REPO_ROOT / "build/bitrix/receivable_credit_decision_details_configuration.json"
)

STAGE_SPECS = [
    {
        "logical_key": "draft",
        "code": "NEW",
        "name": "Черновик",
        "sort": 100,
        "semantics": None,
    },
    {
        "logical_key": "review",
        "code": "REVIEW",
        "name": "На согласовании",
        "sort": 200,
        "semantics": None,
    },
    {
        "logical_key": "approved",
        "code": "APPROVED",
        "name": "Утверждено",
        "sort": 300,
        "semantics": None,
    },
    {
        "logical_key": "onec_check",
        "code": "ONEC_CHECK",
        "name": "Проверка 1С",
        "sort": 400,
        "semantics": None,
    },
    {
        "logical_key": "applying",
        "code": "APPLYING",
        "name": "Применение",
        "sort": 500,
        "semantics": None,
    },
    {
        "logical_key": "applied",
        "code": "SUCCESS",
        "name": "Применено",
        "sort": 600,
        "semantics": "S",
    },
    {
        "logical_key": "rejected",
        "code": "FAIL",
        "name": "Отклонено",
        "sort": 700,
        "semantics": "F",
    },
    {
        "logical_key": "onec_error",
        "code": "ONEC_ERROR",
        "name": "Ошибка 1С",
        "sort": 800,
        "semantics": None,
    },
]

CUSTOM_FIELD_SPECS = [
    {
        "logical_key": "counterparty_ref",
        "title": "Тех. ref контрагента 1С",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "counterparty_guid",
        "title": "GUID контрагента 1С",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "counterparty_code",
        "title": "Код контрагента 1С",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "counterparty_name",
        "title": "Контрагент",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "contract_ref",
        "title": "Тех. ref договора 1С",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "contract_guid",
        "title": "GUID договора 1С",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "contract_code",
        "title": "Код договора 1С",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "contract_name",
        "title": "Точный договор 1С",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "contract_organization_ref",
        "title": "Тех. ref организации договора",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "contract_organization_guid",
        "title": "GUID организации договора",
        "type": "string",
        "required": True,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "current_limit",
        "title": "Текущий кредитный лимит, RUB",
        "type": "double",
        "required": True,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "current_depth",
        "title": "Текущая глубина кредита, дней",
        "type": "integer",
        "required": True,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "current_control_enabled",
        "title": "Текущий контроль суммы договора",
        "type": "boolean",
        "required": True,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "proposed_limit",
        "title": "Предлагаемый кредитный лимит, RUB",
        "type": "double",
        "required": True,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "proposed_depth",
        "title": "Предлагаемая глубина кредита, дней",
        "type": "integer",
        "required": True,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "proposed_control_enabled",
        "title": "Целевой контроль суммы договора",
        "type": "boolean",
        "required": True,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "reason",
        "title": "Основание решения",
        "type": "text",
        "required": True,
        "edit_in_list": True,
        "searchable": False,
    },
    {
        "logical_key": "decision_revision",
        "title": "Ревизия расчета",
        "type": "string",
        "required": True,
        "edit_in_list": True,
        "searchable": True,
    },
    {
        "logical_key": "decision_hash",
        "title": "Тех. хеш решения",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "approved_by",
        "title": "Согласующий (ID Bitrix)",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "approved_at",
        "title": "Время согласования",
        "type": "datetime",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "connector_state",
        "title": "Состояние передачи в 1С",
        "type": "string",
        "required": False,
        "edit_in_list": False,
        "searchable": True,
    },
    {
        "logical_key": "readback_limit",
        "title": "Фактический лимит по readback",
        "type": "double",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "readback_depth",
        "title": "Фактическая глубина по readback",
        "type": "integer",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "readback_control_enabled",
        "title": "Фактический контроль суммы договора",
        "type": "boolean",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
    {
        "logical_key": "connector_error",
        "title": "Ошибка передачи в 1С",
        "type": "text",
        "required": False,
        "edit_in_list": False,
        "searchable": False,
    },
]

DETAIL_SECTION_SPECS = [
    {
        "name": "decision",
        "title": "Решение",
        "elements": [
            "stage",
            "title",
            "assigned_by",
            "counterparty_name",
            "counterparty_code",
            "current_limit",
            "current_depth",
            "proposed_limit",
            "proposed_depth",
            "reason",
            "decision_revision",
        ],
    },
    {
        "name": "approval",
        "title": "Согласование и целостность",
        "elements": [
            "approved_by",
            "approved_at",
            "decision_hash",
            "counterparty_ref",
            "counterparty_guid",
        ],
    },
    {
        "name": "onec",
        "title": "Передача и readback 1С",
        "elements": [
            "connector_state",
            "readback_limit",
            "readback_depth",
            "readback_control_enabled",
            "connector_error",
        ],
    },
]

RESET_ON_RETURN_TO_STAGE_KEYS = ("draft", "review")
RESET_LOGICAL_FIELDS = (
    "decision_hash",
    "approved_by",
    "approved_at",
    "readback_limit",
    "readback_depth",
    "readback_control_enabled",
    "connector_state",
    "connector_error",
)


def blueprint() -> dict[str, Any]:
    return {
        "process": {
            "title": DEFAULT_PROCESS_TITLE,
            "code": DEFAULT_PROCESS_CODE,
            "category": DEFAULT_CATEGORY_NAME,
        },
        "stages": STAGE_SPECS,
        "fields": CUSTOM_FIELD_SPECS,
        "details": DETAIL_SECTION_SPECS,
        "automation": {
            "reset_on_return_to_stages": list(RESET_ON_RETURN_TO_STAGE_KEYS),
            "clear_logical_fields": list(RESET_LOGICAL_FIELDS),
            "worker_enable_gate": (
                "Сначала проверить робота сброса на возврате в Черновик/На согласовании"
            ),
        },
        "safety": {
            "live_apply_requires_flag": True,
            "existing_receivable_entity_type_id_1132_untouched": True,
            "worker_stays_disabled_until_reset_rule_is_verified": True,
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or create/update the receivable_decision smart-process."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--webhook-url")
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument(
        "--details-config-path",
        type=Path,
        default=DEFAULT_DETAILS_CONFIG_PATH,
    )
    parser.add_argument("--title", default=DEFAULT_PROCESS_TITLE)
    parser.add_argument("--code", default=DEFAULT_PROCESS_CODE)
    parser.add_argument("--category-name", default=DEFAULT_CATEGORY_NAME)
    args = parser.parse_args(argv)
    if args.apply and not str(args.webhook_url or "").strip():
        parser.error("--apply requires an explicit --webhook-url")
    return args


def apply_blueprint(args: argparse.Namespace) -> dict[str, Any]:
    webhook = str(args.webhook_url).strip()
    _configure_generic_setup()
    current_user = bitrix_setup.bitrix_call(webhook, "user.current").get("result") or {}
    process_type = bitrix_setup.ensure_type(
        webhook,
        title=args.title,
        code=args.code,
    )
    category = bitrix_setup.ensure_category(
        webhook,
        entity_type_id=int(process_type["entityTypeId"]),
        name=args.category_name,
    )
    stages = bitrix_setup.ensure_stages(
        webhook,
        entity_type_id=int(process_type["entityTypeId"]),
        category_id=int(category["id"]),
    )
    created_fields = bitrix_setup.ensure_custom_fields(
        webhook,
        process_type=process_type,
    )
    field_mapping, field_specs = bitrix_setup.discover_field_mapping(
        webhook,
        process_type,
    )
    mapping = bitrix_setup.build_mapping_payload(
        process_type=process_type,
        category=category,
        stages=stages,
        field_mapping=field_mapping,
        field_specs=field_specs,
    )
    bitrix_setup.save_mapping(mapping, path=args.mapping_path)
    details, details_path = bitrix_setup.ensure_common_details_configuration(
        webhook,
        mapping=mapping,
        path=args.details_config_path,
    )
    return {
        "applied": True,
        "process": mapping["process"],
        "mapping_path": str(args.mapping_path),
        "details_path": str(details_path),
        "details_sections": len(details),
        "created_fields": created_fields,
        "current_webhook_user_id": current_user.get("ID"),
        "automation": blueprint()["automation"],
        "reset_automation_configured": False,
        "worker_enable_blocked": True,
    }


def _configure_generic_setup() -> None:
    bitrix_setup.STAGE_SPECS = STAGE_SPECS
    bitrix_setup.CUSTOM_FIELD_SPECS = CUSTOM_FIELD_SPECS
    bitrix_setup.DETAIL_SECTION_SPECS = DETAIL_SECTION_SPECS
    bitrix_setup._field_xml_id_for_spec = (  # noqa: SLF001
        lambda spec: "UF_CRM_RECEIVABLE_DECISION_"
        + bitrix_setup._slug_suffix(spec["logical_key"])  # noqa: SLF001
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = apply_blueprint(args) if args.apply else {**blueprint(), "applied": False}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
