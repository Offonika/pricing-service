#!/usr/bin/env python3
"""Ensure CRM deal stages for site order fulfillment control.

Dry-run by default. Use --apply only after the stage model is approved.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import requests

DEFAULT_ENTITY_ID = "DEAL_STAGE"
DEFAULT_ENV_FILE = Path(".env")
REQUIRED_STAGES = (
    {
        "STATUS_ID": "PICKUP_WAITING",
        "NAME": "Ожидает самовывоза",
        "SORT": 65,
    },
    {
        "STATUS_ID": "PICKUP_STORAGE",
        "NAME": "Хранение в ПВЗ / отделении",
        "SORT": 70,
    },
    {
        "STATUS_ID": "DISMANTLING",
        "NAME": "Расформирование / отмена",
        "SORT": 80,
    },
)


class BitrixError(RuntimeError):
    """Raised when Bitrix REST returns an error."""


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


class BitrixClient:
    def __init__(self, webhook_base: str) -> None:
        self._webhook_base = webhook_base.rstrip("/") + "/"
        self._session = requests.Session()
        self._session.trust_env = False

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = self._session.post(
            self._webhook_base + method + ".json",
            json=params or {},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise BitrixError(
                f"{method}: {payload.get('error')} {payload.get('error_description')}"
            )
        return payload.get("result")


def load_current_stages(client: BitrixClient, entity_id: str) -> list[dict[str, Any]]:
    stages = client.call(
        "crm.status.list",
        {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}},
    )
    return list(stages or [])


def build_plan(
    current_stages: list[dict[str, Any]],
    required_stages: tuple[dict[str, Any], ...] = REQUIRED_STAGES,
) -> list[dict[str, Any]]:
    current_by_status_id = {str(stage.get("STATUS_ID")): stage for stage in current_stages}
    plan: list[dict[str, Any]] = []
    for required in required_stages:
        existing = current_by_status_id.get(required["STATUS_ID"])
        if existing is None:
            plan.append({"action": "add", "stage": required})
            continue
        mismatches = {
            field: {"current": existing.get(field), "required": required[field]}
            for field in ("NAME", "SORT")
            if str(existing.get(field)) != str(required[field])
        }
        if mismatches:
            plan.append(
                {
                    "action": "manual_review",
                    "stage": required,
                    "existing": {
                        "STATUS_ID": existing.get("STATUS_ID"),
                        "NAME": existing.get("NAME"),
                        "SORT": existing.get("SORT"),
                    },
                    "mismatches": mismatches,
                }
            )
    return plan


def apply_plan(client: BitrixClient, entity_id: str, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in plan:
        if item["action"] != "add":
            results.append({**item, "applied": False})
            continue
        stage = item["stage"]
        result = client.call(
            "crm.status.add",
            {
                "fields": {
                    "ENTITY_ID": entity_id,
                    "STATUS_ID": stage["STATUS_ID"],
                    "NAME": stage["NAME"],
                    "SORT": stage["SORT"],
                }
            },
        )
        results.append({**item, "applied": True, "result": result})
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Path to .env with BITRIX_BOX_WEBHOOK_BASE.",
    )
    parser.add_argument(
        "--entity-id",
        default=DEFAULT_ENTITY_ID,
        help="Bitrix CRM status entity id. Default: DEAL_STAGE.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually add missing stages in Bitrix24. Default is dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env = load_env(args.env_file)
    webhook_base = env.get("BITRIX_BOX_WEBHOOK_BASE")
    if not webhook_base:
        raise SystemExit(f"BITRIX_BOX_WEBHOOK_BASE not found in {args.env_file}")

    client = BitrixClient(webhook_base)
    current_stages = load_current_stages(client, args.entity_id)
    plan = build_plan(current_stages)
    output: dict[str, Any] = {
        "entity_id": args.entity_id,
        "mode": "apply" if args.apply else "dry-run",
        "current_stages": [
            {
                "STATUS_ID": stage.get("STATUS_ID"),
                "NAME": stage.get("NAME"),
                "SORT": stage.get("SORT"),
                "SEMANTICS": stage.get("SEMANTICS"),
            }
            for stage in current_stages
        ],
        "plan": plan,
    }
    if args.apply:
        output["apply_results"] = apply_plan(client, args.entity_id, plan)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
