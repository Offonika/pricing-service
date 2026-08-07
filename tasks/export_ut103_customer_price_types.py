from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.infrastructure.db import session_scope
from app.services.customer_price_type_exports import (
    ApprovedCustomerPriceTypeExportSelection,
    CustomerPriceTypeExportGateError,
    record_customer_price_type_exchange_result,
    record_customer_price_type_export_queued,
    record_customer_price_type_export_request,
    require_successful_customer_price_type_dry_run,
    select_approved_customer_price_type_updates,
)
from app.services.exporters.ut103_customer_price_types import (
    APPROVED_DECISION,
    DEFAULT_SOURCE,
    CustomerPriceTypeUpdateMessage,
    CustomerPriceTypeUpdateRow,
    build_customer_price_type_updates_xml,
    list_customer_price_type_exchange_results,
    one_c_guid_from_counterparty_ref,
    parse_customer_price_type_exchange_result,
    write_customer_price_type_updates_message,
)
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()

    if args.list_results:
        exchange_root = _exchange_root_or_exit(args.exchange_root)
        print(
            json.dumps(
                [
                    _result_to_json(result)
                    for result in list_customer_price_type_exchange_results(exchange_root)
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.record_result:
        try:
            result = parse_customer_price_type_exchange_result(args.record_result)
            with session_scope() as session:
                recorded = record_customer_price_type_exchange_result(session, result)
        except (CustomerPriceTypeExportGateError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(
            json.dumps(
                {
                    "message_id": recorded.message_id,
                    "mode": recorded.mode,
                    "case_ids": list(recorded.case_ids),
                    "succeeded": recorded.succeeded,
                    "idempotent_replay": recorded.idempotent_replay,
                    "result_path": str(args.record_result),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    message_id = (
        args.message_id or f"customer-price-types-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    if args.from_approved_cases:
        return _export_approved_cases(args, message_id)
    return _validate_csv(args, message_id)


def _export_approved_cases(args: argparse.Namespace, message_id: str) -> int:
    exchange_root = None if args.validate_only else _exchange_root_or_exit(args.exchange_root)
    try:
        with session_scope(read_only=args.validate_only) as session:
            selection = select_approved_customer_price_type_updates(
                session,
                snapshot_month=args.snapshot_month,
                case_ids=tuple(args.case_id),
            )
            message = _approved_message(args, message_id, selection)
            payload = build_customer_price_type_updates_xml(message)
            if args.mode == "apply":
                require_successful_customer_price_type_dry_run(
                    session,
                    selection=selection,
                    dry_run_message_id=args.validated_dry_run_message_id,
                )
            summary = _message_summary(
                message,
                validated_only=args.validate_only,
                input_source="approved_cases",
                case_ids=selection.case_ids,
                snapshot_month=args.snapshot_month,
            )
            if args.validate_only:
                if args.json:
                    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                else:
                    print(payload.decode("windows-1251"))
                return 0

            request_replay = record_customer_price_type_export_request(
                session,
                selection=selection,
                message=message,
            )

        # The request commit deliberately precedes the ready-file write. If the
        # filesystem or the following queue-state commit fails, an auditable
        # request remains and the same semantic package can be reconciled safely.
        with session_scope() as session:
            selection = select_approved_customer_price_type_updates(
                session,
                snapshot_month=args.snapshot_month,
                case_ids=tuple(args.case_id),
            )
            message = _approved_message(args, message_id, selection)
            if args.mode == "apply":
                require_successful_customer_price_type_dry_run(
                    session,
                    selection=selection,
                    dry_run_message_id=args.validated_dry_run_message_id,
                )
            record_customer_price_type_export_request(
                session,
                selection=selection,
                message=message,
            )
            output_path = write_customer_price_type_updates_message(
                exchange_root,
                message,
                overwrite=args.overwrite,
            )
            queue_replay = record_customer_price_type_export_queued(
                session,
                selection=selection,
                message=message,
                path=str(output_path),
            )
            summary.update(
                {
                    "path": str(output_path),
                    "request_idempotent_replay": request_replay,
                    "queue_idempotent_replay": queue_replay,
                }
            )
    except CustomerPriceTypeExportGateError as error:
        raise SystemExit(str(error)) from error

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(output_path)
    return 0


def _approved_message(
    args: argparse.Namespace,
    message_id: str,
    selection: ApprovedCustomerPriceTypeExportSelection,
) -> CustomerPriceTypeUpdateMessage:
    approved_by = args.approved_by.strip()
    if approved_by and approved_by != selection.approved_by:
        raise CustomerPriceTypeExportGateError(
            "--approved-by does not match the approver stored in selected cases"
        )
    return CustomerPriceTypeUpdateMessage(
        message_id=message_id,
        rows=selection.rows,
        mode=args.mode,
        approved_by=selection.approved_by,
        source=args.source,
    )


def _validate_csv(args: argparse.Namespace, message_id: str) -> int:
    rows = _rows_from_csv(args.input_csv, message_id)
    message = CustomerPriceTypeUpdateMessage(
        message_id=message_id,
        rows=tuple(rows),
        mode=args.mode,
        approved_by=args.approved_by.strip(),
        source=args.source,
    )
    payload = build_customer_price_type_updates_xml(message)
    summary = _message_summary(
        message,
        validated_only=True,
        input_source="csv_validate_only",
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(payload.decode("windows-1251"))
    return 0


def _message_summary(
    message: CustomerPriceTypeUpdateMessage,
    *,
    validated_only: bool,
    input_source: str,
    case_ids: tuple[int, ...] = (),
    snapshot_month: date | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "message_id": message.message_id,
        "mode": message.mode,
        "rows": len(message.rows),
        "schema": message.schema,
        "validated_only": validated_only,
        "input_source": input_source,
    }
    if message.approved_by:
        summary["approved_by"] = message.approved_by
    if case_ids:
        summary["case_ids"] = list(case_ids)
    if snapshot_month is not None:
        summary["snapshot_month"] = snapshot_month.strftime("%Y-%m")
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export approved 2.Бронзовый -> Розница customer changes to UT 10.3."
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root, e.g. /mnt/ut103")
    parser.add_argument("--message-id", help="Stable id for this one approved batch")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument(
        "--approved-by",
        default="",
        help="Optional expected approver; DB-backed export must match the stored approval",
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("UT103_CUSTOMER_PRICE_TYPES_SOURCE", DEFAULT_SOURCE),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--input-csv",
        type=Path,
        help="Legacy CSV accepted only with --validate-only; it cannot write the exchange queue",
    )
    source.add_argument(
        "--from-approved-cases",
        action="store_true",
        help="Read fail-closed approved READY_FOR_1C cases from pricing-service DB",
    )
    parser.add_argument(
        "--snapshot-month",
        type=_snapshot_month,
        help="Required with --from-approved-cases, format YYYY-MM",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        type=int,
        default=[],
        help="Optional approved case id; repeat to build a bounded pilot package",
    )
    parser.add_argument(
        "--validated-dry-run-message-id",
        help="Required for DB-backed apply; must identify the persisted successful dry_run",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and print the XML (or JSON summary); do not create a ready file",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing ready file")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    parser.add_argument(
        "--list-results",
        action="store_true",
        help="Print parsed customer_price_types result files and exit",
    )
    parser.add_argument(
        "--record-result",
        type=Path,
        help="Parse one 1C result file and append it to the matching case audit trail",
    )
    args = parser.parse_args()
    if args.list_results and args.record_result:
        parser.error("--list-results and --record-result are mutually exclusive")
    if args.list_results or args.record_result:
        return args
    if args.input_csv is None and not args.from_approved_cases:
        parser.error("choose --from-approved-cases or --input-csv")
    if args.input_csv is not None and not args.validate_only:
        parser.error("--input-csv is allowed only with --validate-only")
    if args.from_approved_cases and args.snapshot_month is None:
        parser.error("--snapshot-month is required with --from-approved-cases")
    if args.from_approved_cases and not args.message_id:
        parser.error("--message-id is required with --from-approved-cases")
    if args.from_approved_cases and args.mode == "apply":
        if not args.validated_dry_run_message_id:
            parser.error("DB-backed apply requires --validated-dry-run-message-id")
        if args.message_id == args.validated_dry_run_message_id:
            parser.error("apply must use a new --message-id distinct from dry_run")
    elif args.validated_dry_run_message_id:
        parser.error("--validated-dry-run-message-id is accepted only for DB-backed apply")
    return args


def _snapshot_month(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as error:
        raise argparse.ArgumentTypeError("snapshot month must use YYYY-MM") from error


def _rows_from_csv(path: Path, message_id: str) -> list[CustomerPriceTypeUpdateRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise SystemExit("CSV must contain a header row")
        return [_row_from_mapping(row, message_id) for row in reader]


def _row_from_mapping(item: dict[str, Any], message_id: str) -> CustomerPriceTypeUpdateRow:
    counterparty_ref = str(_field(item, "counterparty_ref", "CounterpartyRef"))
    return CustomerPriceTypeUpdateRow(
        idempotency_key=str(
            _optional_field(
                item,
                "idempotency_key",
                "IdempotencyKey",
                default=f"customer-price-type:{message_id}:{counterparty_ref}",
            )
        ),
        counterparty_ref=counterparty_ref,
        counterparty_guid=str(
            _optional_field(
                item,
                "counterparty_guid",
                "CounterpartyGuid",
                default=one_c_guid_from_counterparty_ref(counterparty_ref),
            )
        ),
        counterparty_name=str(_field(item, "counterparty_name", "CounterpartyName")),
        expected_current_price_type=str(
            _field(
                item,
                "current_price_type",
                "expected_current_price_type",
                "ExpectedCurrentPriceType",
            )
        ),
        target_price_type=str(_field(item, "target_price_type", "TargetPriceType")),
        decision=str(_optional_field(item, "decision", "Decision", default=APPROVED_DECISION)),
        reason=str(_optional_field(item, "reason", "Reason", default="")),
    )


def _field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    raise SystemExit(f"Missing required field; expected one of: {', '.join(names)}")


def _optional_field(item: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return default


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
        "item_results": [
            {
                "idempotency_key": item.idempotency_key,
                "counterparty_ref": item.counterparty_ref,
                "counterparty_guid": item.counterparty_guid,
                "counterparty_name": item.counterparty_name,
                "result": item.result,
                "message": item.message,
                "contract_guid": item.contract_guid,
                "contract_name": item.contract_name,
                "current_price_type": item.current_price_type,
                "target_price_type": item.target_price_type,
                "readback_price_type": item.readback_price_type,
                "found_contracts": item.found_contracts,
            }
            for item in result.item_results
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
