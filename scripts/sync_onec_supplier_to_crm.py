#!/usr/bin/env python3
"""Dry-run/apply sync for 1C suppliers into Bitrix CRM companies and contacts."""

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
from app.services.procurement_supplier_crm import sync_suppliers_to_crm  # noqa: E402
from scripts.ensure_procurement_bitrix_process import (  # noqa: E402
    DEFAULT_ENV_FILE,
    DEFAULT_MAPPING_PATH,
    load_env,
)

DEFAULT_INPUT_PATH = REPO_ROOT / "build/bitrix/onec_suppliers_input.json"
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/onec_supplier_crm_sync_result.json"


class BitrixRestApi:
    def __init__(self, webhook_base: str) -> None:
        self.webhook_base = webhook_base

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return bitrix_setup.bitrix_call(self.webhook_base, method, params or {})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--input-json", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--assigned-by-id", default="")
    parser.add_argument(
        "--apply", action="store_true", help="Write CRM changes. Default is dry-run."
    )
    return parser.parse_args(argv)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_suppliers(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("suppliers") or payload.get("rows") or []
    else:
        rows = []
    return [item for item in rows if isinstance(item, dict)]


def load_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = load_json(path)
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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

    suppliers = load_suppliers(args.input_json)
    if not suppliers:
        raise SystemExit(f"No suppliers found in {args.input_json}")

    result = sync_suppliers_to_crm(
        BitrixRestApi(webhook_base),
        suppliers,
        mapping=load_mapping(args.mapping_path),
        apply=args.apply,
        assigned_by_id=args.assigned_by_id or None,
    )
    args.result_path.parent.mkdir(parents=True, exist_ok=True)
    args.result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
