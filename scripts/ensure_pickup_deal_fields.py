#!/usr/bin/env python3
"""Ensure pickup-control fields on Bitrix deals; dry-run by default."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.site_order_fulfillment import BitrixChatClient  # noqa: E402
from infra.cron import order_fulfillment_sync as fulfillment_sync  # noqa: E402

REQUIRED_FIELDS = (
    ("UF_CRM_MM_PICKUP_STORAGE_STARTED_AT", "datetime", "Самовывоз: поступил на точку"),
    ("UF_CRM_MM_PICKUP_SLA_STARTED_AT", "datetime", "Самовывоз: начало SLA"),
    ("UF_CRM_MM_PICKUP_HOLD_UNTIL", "date", "Самовывоз: удерживать до"),
    ("UF_CRM_MM_PICKUP_DERIVED_STATUS", "string", "Самовывоз: внутренний статус"),
    ("UF_CRM_MM_PICKUP_LAST_EVIDENCE", "string", "Самовывоз: последнее основание"),
)
REUSED_FIELDS = (
    (
        "UF_CRM_MM_PICKUP_READY_SMS_AT",
        "datetime",
        "SMS «Самовывоз готов к выдаче» отправлена",
    ),
)


def fetch_all_deal_user_fields(client: BitrixChatClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start: int | str = 0
    seen_starts: set[str] = set()
    while True:
        response = client.call(
            "crm.deal.userfield.list",
            {"order": {"SORT": "ASC"}, "start": start},
        )
        result = response.get("result") or []
        if not isinstance(result, list):
            raise RuntimeError("crm.deal.userfield.list returned invalid result")
        rows.extend(item for item in result if isinstance(item, dict))
        next_value = response.get("next")
        if next_value in (None, "") or not result:
            break
        next_key = str(next_value)
        if next_key in seen_starts:
            raise RuntimeError("crm.deal.userfield.list pagination loop")
        seen_starts.add(next_key)
        start = next_value
    return rows


def build_plan(current_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current: dict[str, list[dict[str, Any]]] = {}
    for item in current_fields:
        field_name = str(item.get("FIELD_NAME") or "")
        if field_name:
            current.setdefault(field_name, []).append(item)
    plan: list[dict[str, Any]] = []
    definitions = [
        *(tuple(item) + (True,) for item in REQUIRED_FIELDS),
        *(tuple(item) + (False,) for item in REUSED_FIELDS),
    ]
    for field_name, field_type, label, may_create in definitions:
        matches = current.get(field_name, [])
        if len(matches) > 1:
            plan.append(
                {
                    "action": "manual_review",
                    "field_name": field_name,
                    "reason": "duplicate_field_code",
                    "field_ids": [item.get("ID") for item in matches],
                }
            )
            continue
        existing = matches[0] if matches else None
        if existing is None and may_create:
            plan.append(
                {
                    "action": "add",
                    "field_name": field_name,
                    "user_type_id": field_type,
                    "label": label,
                }
            )
            continue
        if existing is None:
            plan.append(
                {
                    "action": "manual_review",
                    "field_name": field_name,
                    "reason": "reused_field_missing",
                    "expected_type": field_type,
                }
            )
            continue
        if str(existing.get("USER_TYPE_ID") or "") != field_type:
            plan.append(
                {
                    "action": "manual_review",
                    "field_name": field_name,
                    "expected_type": field_type,
                    "actual_type": existing.get("USER_TYPE_ID"),
                }
            )
    return plan


def apply_plan(
    client: BitrixChatClient,
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if any(item.get("action") == "manual_review" for item in plan):
        return [{**item, "applied": False, "blocked_by_preflight": True} for item in plan]
    results: list[dict[str, Any]] = []
    for item in plan:
        if item["action"] != "add":
            results.append({**item, "applied": False})
            continue
        label = item["label"]
        result = client.add_deal_user_field(
            {
                "FIELD_NAME": item["field_name"],
                "USER_TYPE_ID": item["user_type_id"],
                "EDIT_FORM_LABEL": {"ru": label},
                "LIST_COLUMN_LABEL": {"ru": label},
                "LIST_FILTER_LABEL": {"ru": label},
                "SHOW_IN_LIST": "Y",
                "EDIT_IN_LIST": "Y",
            }
        )
        results.append({**item, "applied": True, "result": result})
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fulfillment_sync.apply_env_defaults(fulfillment_sync.load_env_files())
    get_settings.cache_clear()
    settings = get_settings()
    webhook_url = fulfillment_sync.resolve_bitrix_webhook_url()
    if not webhook_url:
        raise SystemExit("Bitrix webhook is not configured")
    client = BitrixChatClient(
        webhook_url,
        bot_client_id=settings.order_fulfillment_bot_client_id,
    )
    current = fetch_all_deal_user_fields(client)
    plan = build_plan(current)
    output: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "plan": plan,
    }
    if args.apply:
        output["apply_results"] = apply_plan(client, plan)
        readback = fetch_all_deal_user_fields(client)
        remaining = build_plan(readback)
        output["readback_ok"] = not remaining
        output["remaining"] = remaining
        if remaining:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
