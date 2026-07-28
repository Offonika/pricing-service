from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.services.exporters.ut103_credit_terms import (
    CreditTermsCommand,
    CreditTermsMessage,
    build_credit_terms_xml,
    list_credit_terms_results,
    write_credit_terms_message,
)
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    if args.list_results:
        root = _exchange_root_or_exit(args.exchange_root)
        print(
            json.dumps(
                [_result_to_json(item) for item in list_credit_terms_results(root)],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    payload = json.loads(args.input_json.read_text(encoding="utf-8"))
    commands_payload = payload if isinstance(payload, list) else [payload]
    commands = tuple(_command_from_mapping(item) for item in commands_payload)
    message_id = args.message_id or f"credit-terms-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    message = CreditTermsMessage(message_id=message_id, mode=args.mode, commands=commands)
    xml = build_credit_terms_xml(message)
    summary: dict[str, Any] = {
        "message_id": message.message_id,
        "mode": message.mode,
        "commands": len(message.commands),
        "schema": message.schema,
        "validated_only": bool(args.validate_only),
    }
    if args.validate_only:
        print(
            json.dumps(summary, ensure_ascii=False, sort_keys=True)
            if args.json
            else xml.decode("windows-1251")
        )
        return 0

    path = write_credit_terms_message(
        _exchange_root_or_exit(args.exchange_root),
        message,
        overwrite=args.overwrite,
    )
    summary["path"] = str(path)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True) if args.json else path)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export atomic approved credit-limit/depth decisions to UT 10.3."
    )
    parser.add_argument("--exchange-root")
    parser.add_argument("--message-id")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--input-json", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list-results", action="store_true")
    args = parser.parse_args()
    if not args.list_results and args.input_json is None:
        parser.error("--input-json is required unless --list-results is used")
    return args


def _command_from_mapping(item: dict[str, Any]) -> CreditTermsCommand:
    approved_at = datetime.fromisoformat(str(_field(item, "approved_at")))
    return CreditTermsCommand(
        idempotency_key=str(_field(item, "idempotency_key")),
        decision_id=str(_field(item, "decision_id")),
        decision_hash=str(_field(item, "decision_hash")),
        revision=str(_field(item, "revision")),
        counterparty_ref=str(_field(item, "counterparty_ref")),
        counterparty_guid=str(_field(item, "counterparty_guid")),
        counterparty_code=str(_field(item, "counterparty_code")),
        counterparty_name=str(_field(item, "counterparty_name")),
        expected_current_limit=Decimal(str(_field(item, "expected_current_limit"))),
        expected_current_depth=int(_field(item, "expected_current_depth")),
        new_limit=Decimal(str(_field(item, "new_limit"))),
        new_depth=int(_field(item, "new_depth")),
        currency=str(_field(item, "currency")),
        reason=str(_field(item, "reason")),
        approved_by=str(_field(item, "approved_by")),
        approved_at=approved_at,
    )


def _field(item: dict[str, Any], name: str) -> Any:
    value = item.get(name)
    if value in (None, ""):
        raise SystemExit(f"Missing required field: {name}")
    return value


def _exchange_root_or_exit(explicit: str | Path | None) -> str:
    try:
        return resolve_ut103_exchange_root(explicit)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def _result_to_json(result: Any) -> dict[str, Any]:
    return {
        "message_id": result.message_id,
        "schema": result.schema,
        "status": result.status,
        "loaded": result.loaded,
        "failed": result.failed,
        "errors": result.errors,
        "path": str(result.path) if result.path else None,
        "command_results": [
            {
                "idempotency_key": item.idempotency_key,
                "decision_id": item.decision_id,
                "decision_hash": item.decision_hash,
                "counterparty_code": item.counterparty_code,
                "status": item.status,
                "message": item.message,
                "old_limit": str(item.old_limit) if item.old_limit is not None else None,
                "old_depth": item.old_depth,
                "requested_limit": (
                    str(item.requested_limit) if item.requested_limit is not None else None
                ),
                "requested_depth": item.requested_depth,
                "readback_limit": (
                    str(item.readback_limit) if item.readback_limit is not None else None
                ),
                "readback_depth": item.readback_depth,
            }
            for item in result.command_results
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
