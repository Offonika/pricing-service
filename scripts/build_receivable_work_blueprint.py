#!/usr/bin/env python3
"""Build a local, write-free blueprint for the receivable work smart-process."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = REPO_ROOT / "build/bitrix/receivable_work_blueprint.json"
PROCESS_TITLE = "Работа с дебиторкой"
PROCESS_CODE = "receivable_work"
LEGACY_PROCESS_TITLE = "Дебиторка покупателей"

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


def build_blueprint() -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "bitrix_writes": False,
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
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)
    payload = build_blueprint()
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Dry-run blueprint '{PROCESS_TITLE}' written to {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
