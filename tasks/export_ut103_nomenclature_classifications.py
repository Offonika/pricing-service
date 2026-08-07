from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.infrastructure.db.session import session_scope
from app.services.exporters.ut103_exchange import (
    load_ut103_env_file,
    resolve_ut103_exchange_root,
)
from app.services.exporters.ut103_nomenclature_classifications import (
    DEFAULT_SOURCE,
    DEFAULT_TARGET,
    NomenclatureClassificationIntentRow,
    OneCClassificationReference,
    prepare_nomenclature_classification_command,
)
from app.services.nomenclature_classification_operations import (
    cancel_nomenclature_classification_operation,
    get_nomenclature_classification_status,
    register_nomenclature_classification_operation,
    request_nomenclature_classification_apply,
    run_nomenclature_classification_cycle,
)

FORBIDDEN_INPUT_FIELDS = frozenset(
    {
        "decision_hash",
        "DecisionHash",
        "command_hash",
        "CommandHash",
        "message_id",
        "MessageId",
        "mode",
        "Mode",
    }
)


def main(argv: list[str] | None = None) -> int:
    load_ut103_env_file()
    args = _parse_args(argv)
    if args.command == "validate-only":
        rows = tuple(_row_from_mapping(item) for item in _load_items(args.input_json))
        prepared, command_hash, canonical = prepare_nomenclature_classification_command(
            rows,
            approved_by=args.approved_by,
            source=args.source,
            target=args.target,
        )
        print(
            json.dumps(
                {
                    "command_hash": command_hash,
                    "items": [
                        {
                            "decision_hash": row.decision_hash,
                            "idempotency_key": row.idempotency_key,
                            "nomenclature_code": row.nomenclature_code,
                        }
                        for row in prepared
                    ],
                    "canonical_payload": canonical,
                    "validated_only": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    settings = get_settings()
    with session_scope(read_only=args.command == "status") as db:
        if args.command == "register":
            rows = tuple(_row_from_mapping(item) for item in _load_items(args.input_json))
            operation = register_nomenclature_classification_operation(
                db,
                rows,
                approved_by=args.approved_by,
                requested_by=args.requested_by,
                settings=settings,
                source=args.source,
                target=args.target,
            )
            payload = get_nomenclature_classification_status(db, operation.operation_id)
        elif args.command == "status":
            payload = get_nomenclature_classification_status(db, args.operation_id)
        elif args.command == "request-apply":
            operation = request_nomenclature_classification_apply(
                db,
                args.operation_id,
                requested_by=args.requested_by,
                settings=settings,
            )
            payload = get_nomenclature_classification_status(db, operation.operation_id)
        elif args.command == "run-cycle":
            payload = run_nomenclature_classification_cycle(
                db,
                exchange_root=resolve_ut103_exchange_root(args.exchange_root),
                settings=settings,
            )
        elif args.command == "cancel":
            operation = cancel_nomenclature_classification_operation(
                db,
                args.operation_id,
                requested_by=args.requested_by,
                confirm_read_only_reconciled=args.confirm_read_only_reconciled,
            )
            payload = get_nomenclature_classification_status(db, operation.operation_id)
        else:  # pragma: no cover - argparse guarantees the command
            raise AssertionError(args.command)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if args.command == "run-cycle" and payload.get("errors") else 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manage durable explicit nomenclature classification commands for UT 10.3. "
            "This task never calculates classification."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-only")
    _add_input_arguments(validate, requested_by=False)

    register = subparsers.add_parser("register")
    _add_input_arguments(register, requested_by=True)

    status = subparsers.add_parser("status")
    status.add_argument("--operation-id", required=True)

    request_apply = subparsers.add_parser("request-apply")
    request_apply.add_argument("--operation-id", required=True)
    request_apply.add_argument("--requested-by", required=True)

    run_cycle = subparsers.add_parser("run-cycle")
    run_cycle.add_argument("--exchange-root")

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--operation-id", required=True)
    cancel.add_argument("--requested-by", required=True)
    cancel.add_argument("--confirm-read-only-reconciled", action="store_true")
    return parser.parse_args(argv)


def _add_input_arguments(parser: argparse.ArgumentParser, *, requested_by: bool) -> None:
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--approved-by", required=True)
    if requested_by:
        parser.add_argument("--requested-by", required=True)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--target", default=DEFAULT_TARGET)


def _load_items(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read classification JSON: {error}") from error
    if isinstance(payload, dict):
        forbidden = FORBIDDEN_INPUT_FIELDS.intersection(payload)
        if forbidden:
            raise SystemExit(
                f"service-managed input fields are forbidden: {', '.join(sorted(forbidden))}"
            )
        items = payload.get("items")
    else:
        items = payload
    if not isinstance(items, list) or not items:
        raise SystemExit("classification JSON must contain a non-empty items array")
    if not all(isinstance(item, dict) for item in items):
        raise SystemExit("every classification item must be an object")
    return items


def _row_from_mapping(item: dict[str, Any]) -> NomenclatureClassificationIntentRow:
    forbidden = FORBIDDEN_INPUT_FIELDS.intersection(item)
    if forbidden:
        raise SystemExit(
            f"service-managed input fields are forbidden: {', '.join(sorted(forbidden))}"
        )
    if "approved_by" in item or "ApprovedBy" in item:
        raise SystemExit("ApprovedBy is allowed only at command/header level")
    group_mode = str(_optional_field(item, "group_mode", "GroupMode", default="set"))
    category_mode = str(
        _optional_field(item, "category_mode", "CategoryMode", default="ensure_present")
    )
    return NomenclatureClassificationIntentRow(
        idempotency_key=str(_field(item, "idempotency_key", "IdempotencyKey")),
        nomenclature_code=str(_field(item, "nomenclature_code", "NomenclatureCode")),
        nomenclature_guid=str(_field(item, "nomenclature_guid", "NomenclatureGuid")),
        expected_kind=_reference(item, "expected_kind", "ExpectedKind", allow_empty=True),
        target_kind=_reference(item, "target_kind", "TargetKind"),
        expected_group=_reference(item, "expected_group", "ExpectedGroup", allow_empty=True),
        target_group=_reference(
            item,
            "target_group",
            "TargetGroup",
            allow_empty=group_mode.strip().lower() == "clear_expected",
        ),
        group_mode=group_mode,
        category_mode=category_mode,
        expected_category=_reference(
            item,
            "expected_category",
            "ExpectedCategory",
            allow_empty=True,
        ),
        target_category=_reference(
            item,
            "target_category",
            "TargetCategory",
            allow_empty=category_mode.strip().lower() == "remove_expected",
        ),
        reason=str(_optional_field(item, "reason", "Reason", default="")),
    )


def _reference(
    item: dict[str, Any],
    *names: str,
    allow_empty: bool = False,
) -> OneCClassificationReference:
    raw = _optional_field(item, *names, default=None)
    if raw is None and allow_empty:
        return OneCClassificationReference()
    if not isinstance(raw, dict):
        raise SystemExit(f"{names[0]} must be an object with guid/code/name")
    return OneCClassificationReference(
        guid=str(_optional_field(raw, "guid", "Guid", default="")),
        code=str(_optional_field(raw, "code", "Code", default="")),
        name=str(_optional_field(raw, "name", "Name", default="")),
    )


def _field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    raise SystemExit(f"missing required field; expected one of: {', '.join(names)}")


def _optional_field(item: dict[str, Any], *names: str, default: Any) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return default


if __name__ == "__main__":
    raise SystemExit(main())
