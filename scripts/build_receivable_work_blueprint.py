#!/usr/bin/env python3
"""Build a local, write-free blueprint for the receivable work smart-process."""

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
DEFAULT_OUTPUT_PATH = REPO_ROOT / "build/bitrix/receivable_work_blueprint.json"
PROCESS_TITLE = "Работа с дебиторкой"
PROCESS_CODE = "receivable_work"
LEGACY_PROCESS_TITLE = "Дебиторка покупателей"
LEGACY_ENTITY_TYPE_ID = 1132
ARSEN_NAME = "Арсен"
ARSEN_LAST_NAME = "Сагиян"

READ_ONLY_BITRIX_METHODS = {
    "crm.type.list",
    "crm.category.list",
    "crm.status.list",
    "crm.item.fields",
    "user.search",
    "department.get",
}

STAGES = [
    {"logical_key": "new", "code": "NEW", "name": "Новый", "sort": 100},
    {"logical_key": "in_progress", "code": "IN_PROGRESS", "name": "В работе", "sort": 200},
    {
        "logical_key": "waiting_payment",
        "code": "WAITING_PAYMENT",
        "name": "Ожидаем оплату",
        "sort": 300,
    },
    {"logical_key": "dispute", "code": "DISPUTE", "name": "Спор", "sort": 400},
    {
        "logical_key": "escalation",
        "code": "ESCALATION",
        "name": "Эскалация",
        "sort": 500,
    },
    {
        "logical_key": "closed",
        "code": "CLOSED",
        "name": "Закрыто",
        "sort": 600,
        "semantics": "S",
    },
]

FIELDS = [
    ("stable_key", "Технический ключ кейса", "string"),
    ("company_id", "Компания CRM", "crm_company"),
    ("counterparty_ref", "Контрагент 1С", "string"),
    ("current_balance", "Текущий долг", "double"),
    ("last_contact_at", "Последний контакт", "datetime"),
    ("last_contact_comment", "Последний комментарий", "text"),
    ("promised_payment_date", "Обещанная дата оплаты", "datetime"),
    ("next_action_date", "Следующий контакт", "datetime"),
    ("phone_status", "Наличие телефона", "string"),
    ("last_sms_status", "Последний статус SMS", "string"),
    ("legacy_item_url", "Историческая карточка", "url"),
]


def build_blueprint(*, live_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "bitrix_writes": False,
            "allowed_bitrix_methods": sorted(READ_ONLY_BITRIX_METHODS),
            "apply_requires_separate_approval": True,
            "legacy_items_mutated": False,
        },
        "process": {
            "title": PROCESS_TITLE,
            "code": PROCESS_CODE,
            "legacy_process_kept_as_history": LEGACY_PROCESS_TITLE,
        },
        "pilot": {
            "onec_folder": "Покупатели",
            "department_owner": "Арсен Сагиян",
            "expansion_gate": "5 рабочих дней без дублей и расхождений",
        },
        "stages": STAGES,
        "fields": [
            {"logical_key": key, "title": title, "type": field_type}
            for key, title, field_type in FIELDS
        ],
        "migration": {
            "source": "receivable_work_item",
            "active_fields_only": [
                "current_balance",
                "status",
                "last_contact_at",
                "promised_payment_date",
                "next_action_date",
                "last_contact_comment",
            ],
            "full_legacy_history_copy": False,
            "legacy_link_required": True,
        },
        "non_stage_signals": [
            "last_sms_status",
            "phone_status",
            "promised_payment_date",
            "next_action_date",
        ],
        "live_snapshot": live_snapshot or {"status": "not_requested"},
    }


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


def _safe_bitrix_call(webhook_base: str, method: str, params: dict[str, Any]) -> Any:
    if method not in READ_ONLY_BITRIX_METHODS:
        raise RuntimeError(f"Refusing non-read-only Bitrix method: {method}")
    return bitrix_setup.bitrix_call(webhook_base, method, params).get("result")


def _error_code(error: Exception) -> str:
    return type(error).__name__


def build_live_snapshot(webhook_base: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"status": "ready", "write_methods_used": []}
    target_types: list[dict[str, Any]] = []
    try:
        result = _safe_bitrix_call(webhook_base, "crm.type.list", {})
        types = result.get("types") if isinstance(result, dict) else []
        compact_types = [
            {
                "id": item.get("id"),
                "entityTypeId": item.get("entityTypeId"),
                "title": item.get("title"),
                "code": item.get("code"),
            }
            for item in types or []
        ]
        target_types = [
            item
            for item in compact_types
            if str(item.get("title") or "").strip() == PROCESS_TITLE
            or str(item.get("code") or "").strip() == PROCESS_CODE
        ]
        snapshot["target_process_candidates"] = target_types
        snapshot["target_collision"] = bool(target_types)
        snapshot["legacy_process"] = next(
            (
                item
                for item in compact_types
                if int(item.get("entityTypeId") or 0) == LEGACY_ENTITY_TYPE_ID
                or str(item.get("title") or "").strip() == LEGACY_PROCESS_TITLE
            ),
            None,
        )
    except Exception as error:  # pragma: no cover - live reporting
        snapshot["type_error"] = _error_code(error)

    if len(target_types) == 1 and int(target_types[0].get("entityTypeId") or 0) > 0:
        entity_type_id = int(target_types[0]["entityTypeId"])
        snapshot["target_entity_type_id"] = entity_type_id
        try:
            categories_result = _safe_bitrix_call(
                webhook_base, "crm.category.list", {"entityTypeId": entity_type_id}
            )
            categories = (
                categories_result.get("categories") if isinstance(categories_result, dict) else []
            )
            snapshot["target_categories"] = categories or []
            stages_by_category: dict[str, list[dict[str, Any]]] = {}
            for category in categories or []:
                category_id = int(category.get("id"))
                stages = _safe_bitrix_call(
                    webhook_base,
                    "crm.status.list",
                    {
                        "filter[ENTITY_ID]": f"DYNAMIC_{entity_type_id}_STAGE_{category_id}",
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
            snapshot["target_stages"] = stages_by_category
        except Exception as error:  # pragma: no cover
            snapshot["category_stage_error"] = _error_code(error)
        try:
            fields_result = _safe_bitrix_call(
                webhook_base, "crm.item.fields", {"entityTypeId": entity_type_id}
            )
            fields = fields_result.get("fields") if isinstance(fields_result, dict) else {}
            snapshot["target_field_count"] = len(fields or {})
            snapshot["target_user_fields"] = [
                {
                    "name": name,
                    "title": meta.get("title") or meta.get("formLabel") or name,
                    "type": meta.get("type"),
                }
                for name, meta in sorted((fields or {}).items())
                if name.startswith("ufCrm")
            ]
        except Exception as error:  # pragma: no cover
            snapshot["field_error"] = _error_code(error)

    try:
        result = _safe_bitrix_call(
            webhook_base,
            "user.search",
            {"FILTER[NAME]": ARSEN_NAME, "FILTER[LAST_NAME]": ARSEN_LAST_NAME},
        )
        users = result if isinstance(result, list) else []
        snapshot["arsen_candidates"] = [
            {
                "ID": user.get("ID"),
                "NAME": user.get("NAME"),
                "LAST_NAME": user.get("LAST_NAME"),
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
                webhook_base, "department.get", {"ID": department_id}
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
    except Exception as error:  # pragma: no cover
        snapshot["arsen_error"] = _error_code(error)
    return snapshot


def resolve_webhook(args: argparse.Namespace) -> str:
    if args.webhook_url:
        return args.webhook_url.strip()
    env = load_env(args.env_file)
    return (
        env.get("RECEIVABLE_BITRIX_WEBHOOK_URL") or env.get("BITRIX_BOX_WEBHOOK_BASE") or ""
    ).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--bitrix-readonly",
        action="store_true",
        help="Read process and pilot-owner metadata. Never writes to Bitrix.",
    )
    args = parser.parse_args(argv)
    live_snapshot: dict[str, Any] | None = None
    if args.bitrix_readonly:
        webhook = resolve_webhook(args)
        live_snapshot = (
            build_live_snapshot(webhook)
            if webhook
            else {"status": "skipped", "reason": "webhook_not_configured"}
        )
    payload = build_blueprint(live_snapshot=live_snapshot)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
