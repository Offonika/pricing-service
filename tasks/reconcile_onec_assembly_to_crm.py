from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine, get_application_engine
from app.models.order_assembly_queue import OrderAssemblyCrmOutbox
from app.services import order_assembly_outbox

DEFAULT_CRM_URL = "https://crm.master-mobile.ru/local/tools/mm_crm_1c_assembly_status.php"
DEFAULT_STATE_PATH = Path(".local/onec_assembly_crm_reconciler.sqlite3")
TOKEN_ENV_NAMES = (
    "MM_CRM_1C_ASSEMBLY_TOKEN",
    "CRM_1C_ASSEMBLY_TOKEN",
)


@dataclass(frozen=True)
class AssemblyEvent:
    event_key: str
    crm_status: str
    event_at: datetime
    assembly_source: str
    assembly_ref: str
    rtu_external_id: str | None
    rtu_number: str | None
    rtu_date: datetime | None
    onec_order_number: str
    site_order_number: str
    is_posted: bool
    execution_status: str
    delivery_code: str
    payment_mode: str | None = None
    document_amount: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Read 1C RTU assembly events from SQL and send safe assembled signal to CRM.")
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Send real CRM signal and remember processed events. Default sends dry-run.",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Only print 1C candidates, do not call CRM and do not require token.",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Look back this many hours in 1C history. Default: 24.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max events to inspect. Default: 100.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"SQLite state file. Default: {DEFAULT_STATE_PATH}",
    )
    parser.add_argument(
        "--crm-url",
        default=DEFAULT_CRM_URL,
        help="CRM assembly webhook URL.",
    )
    parser.add_argument(
        "--include-processed",
        action="store_true",
        help="Do not filter events already recorded in local state.",
    )
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--retry-base-seconds", type=int, default=60)
    return parser.parse_args()


def _token_from_env() -> str:
    for env_name in TOKEN_ENV_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _event_from_row(row: Any) -> AssemblyEvent:
    data = row._mapping
    event_key = (data["event_key"] or "").strip()
    if not event_key:
        event_key = f"{data['crm_status']}:{data['rtu_external_id']}:{_format_dt(data['event_at'])}"
    return AssemblyEvent(
        event_key=event_key,
        crm_status=(data["crm_status"] or "assembled").strip(),
        event_at=data["event_at"],
        assembly_source=(data["assembly_source"] or "customer_order").strip(),
        assembly_ref=(data["assembly_ref"] or "").strip(),
        rtu_external_id=(data["rtu_external_id"] or "").strip() or None,
        rtu_number=(data["rtu_number"] or "").strip() or None,
        rtu_date=data["rtu_date"],
        onec_order_number=(data["onec_order_number"] or "").strip(),
        site_order_number=(data["site_order_number"] or "").strip(),
        is_posted=bool(data["is_posted"]),
        execution_status=(data["execution_status"] or "").strip(),
        delivery_code=(data["delivery_code"] or "").strip().upper(),
        payment_mode=(data["payment_mode"] or "").strip().lower() or None,
        document_amount=(
            str(data["document_amount"]) if data.get("document_amount") is not None else None
        ),
    )


def fetch_assembly_events(
    onec_database_url: str,
    *,
    since: datetime,
    limit: int,
    delivery_code_column: str | None = None,
) -> list[AssemblyEvent]:
    delivery_expression = _delivery_code_expression(delivery_code_column)
    limit_clause = f"TOP ({max(1, int(limit))})"
    statement = text(f"""
        WITH crm_events AS (
            SELECT
                'assembled-order:' + CONVERT(varchar(34), hist._SimpleKey, 1) AS event_key,
                'assembled' AS crm_status,
                hist._Fld9450 AS event_at,
                'customer_order' AS assembly_source,
                CONVERT(varchar(34), ord._IDRRef, 1) AS assembly_ref,
                CAST(NULL AS varchar(34)) AS rtu_external_id,
                CAST(NULL AS nvarchar(32)) AS rtu_number,
                CAST(NULL AS datetime) AS rtu_date,
                LTRIM(RTRIM(ord._Number)) AS onec_order_number,
                NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') AS site_order_number,
                CASE WHEN ord._Posted = 0x01 THEN 1 ELSE 0 END AS is_posted,
                LTRIM(RTRIM(execution_status._Code)) AS execution_status,
                {delivery_expression} AS delivery_code,
                CAST(NULL AS nvarchar(32)) AS payment_mode,
                CAST(NULL AS decimal(18, 2)) AS document_amount
            FROM dbo._InfoRg9448 AS hist WITH (NOLOCK)
            JOIN dbo._Document132 AS ord WITH (NOLOCK)
                ON ord._IDRRef = hist._Fld9449_RRRef
            JOIN dbo._Reference10081 AS execution_status WITH (NOLOCK)
                ON execution_status._IDRRef = ord._Fld10083RRef
            WHERE hist._Fld9454 = N'Собран'
              AND hist._Fld9449_RTRef = 0x00000084
              AND hist._Fld9450 >= :since
              AND ord._Marked = 0x00
              AND LTRIM(RTRIM(execution_status._Code)) IN (N'05', N'06')
              AND NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') IS NOT NULL

            UNION ALL

            SELECT
                'issued-scan:' + CONVERT(varchar(34), scan_event._SimpleKey, 1) AS event_key,
                'issued' AS crm_status,
                scan_event._Fld9450 AS event_at,
                'rtu' AS assembly_source,
                CONVERT(varchar(34), rtu._IDRRef, 1) AS assembly_ref,
                CONVERT(varchar(34), rtu._IDRRef, 1) AS rtu_external_id,
                LTRIM(RTRIM(rtu._Number)) AS rtu_number,
                rtu._Date_Time AS rtu_date,
                LTRIM(RTRIM(ord._Number)) AS onec_order_number,
                NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') AS site_order_number,
                CASE WHEN rtu._Posted = 0x01 THEN 1 ELSE 0 END AS is_posted,
                LTRIM(RTRIM(execution_status._Code)) AS execution_status,
                {delivery_expression} AS delivery_code,
                CAST(NULL AS nvarchar(32)) AS payment_mode,
                CAST(rtu._Fld4948 AS decimal(18, 2)) AS document_amount
            FROM dbo._InfoRg9448 AS scan_event WITH (NOLOCK)
            JOIN dbo._Document203 AS rtu WITH (NOLOCK)
                ON rtu._IDRRef = scan_event._Fld9449_RRRef
            JOIN dbo._Document132 AS ord WITH (NOLOCK)
                ON ord._IDRRef = rtu._Fld4939_RRRef
            JOIN dbo._Reference10081 AS execution_status WITH (NOLOCK)
                ON execution_status._IDRRef = ord._Fld10083RRef
            WHERE scan_event._Fld9454 = N'Отсканирован'
              AND scan_event._Fld9449_RTRef = 0x000000CB
              AND scan_event._Fld9450 >= :since
              AND rtu._Posted = 0x01
              AND rtu._Marked = 0x00
              AND NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') IS NOT NULL
              AND UPPER(COALESCE(CAST(ord._Fld9266 AS nvarchar(max)), N'')) LIKE N'%САМОВЫВОЗ%'
              AND EXISTS (
                  SELECT 1
                  FROM dbo._InfoRg9448 AS print_event WITH (NOLOCK)
                  WHERE print_event._Fld9449_RRRef = rtu._IDRRef
                    AND print_event._Fld9449_RTRef = 0x000000CB
                    AND print_event._Fld9454 = N'Распечатан'
              )
        )
        SELECT {limit_clause}
            event_key,
            crm_status,
            event_at,
            assembly_source,
            assembly_ref,
            rtu_external_id,
            rtu_number,
            rtu_date,
            onec_order_number,
            site_order_number,
            is_posted,
            execution_status,
            delivery_code,
            payment_mode,
            document_amount
        FROM crm_events
        ORDER BY event_at ASC, rtu_date ASC, crm_status ASC
        """)
    engine = build_engine(onec_database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        return [_event_from_row(row) for row in connection.execute(statement, {"since": since})]


def _delivery_code_expression(column: str | None) -> str:
    fallback = """CASE
        WHEN UPPER(COALESCE(CAST(ord._Fld9266 AS nvarchar(max)), N'')) LIKE N'%\u0421\u0410\u041c\u041e\u0412\u042b\u0412\u041e\u0417%' THEN N'PICKUP'
        WHEN UPPER(COALESCE(CAST(ord._Fld9266 AS nvarchar(max)), N'')) LIKE N'%\u0414\u041e\u0421\u0422\u0410\u0412\u0418\u0421\u0422%' THEN N'DOSTAVISTA'
        WHEN UPPER(COALESCE(CAST(ord._Fld9266 AS nvarchar(max)), N'')) LIKE N'%\u042f\u041d\u0414\u0415\u041a\u0421%\u0422\u0410\u041a\u0421\u0418%' THEN N'YANDEX_TAXI'
        WHEN UPPER(COALESCE(CAST(ord._Fld9266 AS nvarchar(max)), N'')) LIKE N'%\u041a\u0423\u0420\u042c\u0415\u0420%' THEN N'MM_COURIER'
        WHEN UPPER(COALESCE(CAST(ord._Fld9266 AS nvarchar(max)), N'')) LIKE N'%\u0421\u0414\u042d\u041a%' THEN N'CDEK_COURIER'
        WHEN UPPER(COALESCE(CAST(ord._Fld9266 AS nvarchar(max)), N'')) LIKE N'%\u041f\u041e\u0427\u0422\u0410%' THEN N'RUSSIAN_POST'
        ELSE N'OTHER'
    END"""
    if column:
        if not re.fullmatch(r"_Fld\d+", column):
            raise ValueError("ONEC_ORDER_DELIVERY_CODE_COLUMN must look like _Fld12345")
        configured = f"UPPER(LTRIM(RTRIM(COALESCE(CAST(ord.{column} AS nvarchar(32)), N''))))"
        return f"COALESCE(NULLIF({configured}, N''), {fallback})"
    return fallback


def ensure_state(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS processed_events (
            event_key TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            site_order_number TEXT NOT NULL,
            rtu_number TEXT NOT NULL,
            crm_response TEXT
        )
        """)
    connection.commit()


def load_processed(connection: sqlite3.Connection) -> set[str]:
    ensure_state(connection)
    rows = connection.execute("SELECT event_key FROM processed_events").fetchall()
    return {str(row[0]) for row in rows}


def record_processed(
    connection: sqlite3.Connection,
    event: AssemblyEvent,
    *,
    crm_response: dict[str, Any],
) -> None:
    ensure_state(connection)
    connection.execute(
        """
        INSERT OR REPLACE INTO processed_events
            (event_key, processed_at, site_order_number, rtu_number, crm_response)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            event.event_key,
            _format_dt(datetime.now()),
            event.site_order_number,
            event.rtu_number or "",
            json.dumps(crm_response, ensure_ascii=False, sort_keys=True),
        ),
    )
    connection.commit()


def send_to_crm(
    event: AssemblyEvent,
    *,
    crm_url: str,
    token: str,
    dry_run: bool,
) -> dict[str, Any]:
    payload = {
        "token": token,
        "order": event.site_order_number,
        "status": event.crm_status,
        "assembly_source": event.assembly_source,
        "assembly_ref": event.assembly_ref,
        "idempotency_key": event.event_key,
        "execution_status": event.execution_status,
        "delivery_code": event.delivery_code,
    }
    if event.payment_mode:
        payload["payment_mode"] = event.payment_mode
    if event.rtu_number:
        payload["rtu"] = event.rtu_number
    if event.crm_status == "issued":
        payload["issued_at"] = _format_dt(event.event_at)
        if event.document_amount:
            payload["document_amount"] = event.document_amount
    else:
        payload["assembled_at"] = _format_dt(event.event_at)
    if dry_run:
        payload["dry_run"] = "1"
    response = requests.post(crm_url, data=payload, timeout=20)
    response_text = response.text[:1000]
    try:
        response_json: dict[str, Any] = response.json()
    except ValueError:
        response_json = {"ok": False, "raw": response_text}
    response_json.setdefault("http_status", response.status_code)
    return response_json


def enqueue_assembled_event(
    session: Session,
    event: AssemblyEvent,
) -> order_assembly_outbox.AssemblyOutboxEnqueueResult:
    return order_assembly_outbox.enqueue_assembly_event(
        session,
        order_assembly_outbox.AssemblyOutboxInput(
            event_key=event.event_key,
            event_at=event.event_at,
            assembly_source=event.assembly_source,
            assembly_ref=event.assembly_ref,
            site_order_number=event.site_order_number,
            execution_status=event.execution_status,
            delivery_code=event.delivery_code,
            payment_mode=event.payment_mode,
            onec_order_number=event.onec_order_number,
            crm_status=event.crm_status,
        ),
    )


def send_outbox_row_to_crm(
    row: OrderAssemblyCrmOutbox,
    *,
    crm_url: str,
    token: str,
) -> dict[str, Any]:
    payload = order_assembly_outbox.crm_payload(row)
    payload["token"] = token
    response = requests.post(crm_url, data=payload, timeout=20)
    response_text = response.text[:1000]
    try:
        response_json: dict[str, Any] = response.json()
    except ValueError:
        response_json = {"ok": False, "raw": response_text}
    response_json.setdefault("http_status", response.status_code)
    if not 200 <= response.status_code < 300:
        response_json["ok"] = False
    return response_json


def print_event_result(
    event: AssemblyEvent,
    *,
    status: str,
    crm_response: dict[str, Any] | None = None,
) -> None:
    row = {
        "status": status,
        "crm_status": event.crm_status,
        "event_at": _format_dt(event.event_at),
        "site_order": event.site_order_number,
        "onec_order": event.onec_order_number,
        "assembly_source": event.assembly_source,
        "assembly_ref": event.assembly_ref,
        "rtu": event.rtu_number or "",
        "posted": event.is_posted,
    }
    if crm_response is not None:
        row["crm_ok"] = bool(crm_response.get("ok"))
        row["crm_stage"] = crm_response.get("stage") or crm_response.get("target_stage")
        row["crm_message"] = crm_response.get("message") or crm_response.get("error")
    print(json.dumps(row, ensure_ascii=False, sort_keys=True))


def main() -> int:
    args = parse_args()
    settings = get_settings()
    if not settings.onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is not configured")

    if args.apply and args.no_send:
        raise SystemExit("--apply and --no-send cannot be used together")

    token = "" if args.no_send else _token_from_env()
    if not args.no_send and not token:
        names = " or ".join(TOKEN_ENV_NAMES)
        raise SystemExit(f"CRM token is not configured. Set {names}.")

    since = datetime.now() - timedelta(hours=max(1, int(args.since_hours)))
    events = fetch_assembly_events(
        settings.onec_database_url,
        since=since,
        limit=max(1, int(args.limit)),
        delivery_code_column=os.environ.get("ONEC_ORDER_DELIVERY_CODE_COLUMN") or None,
    )

    if args.no_send:
        print(
            json.dumps(
                {
                    "mode": "no-send",
                    "since": _format_dt(since),
                    "found": len(events),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        for event in events:
            print_event_result(event, status="candidate")
        return 0

    if not args.apply:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "since": _format_dt(since),
                    "found": len(events),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        exit_code = 0
        for event in events:
            crm_response = send_to_crm(
                event,
                crm_url=args.crm_url,
                token=token,
                dry_run=True,
            )
            ok = bool(crm_response.get("ok"))
            print_event_result(
                event,
                status="dry_run_sent" if ok else "crm_rejected",
                crm_response=crm_response,
            )
            if not ok:
                exit_code = 1
        return exit_code

    assembled_events = [event for event in events if event.crm_status == "assembled"]
    issued_events = [event for event in events if event.crm_status == "issued"]
    exit_code = 0

    application_engine = get_application_engine()
    with Session(application_engine) as application_session:
        for event in assembled_events:
            try:
                result = enqueue_assembled_event(application_session, event)
                application_session.commit()
                print_event_result(
                    event,
                    status="outbox_enqueued" if result.created else result.row.status,
                )
            except order_assembly_outbox.AssemblyOutboxError as exc:
                application_session.rollback()
                print_event_result(
                    event,
                    status=f"outbox_rejected:{type(exc).__name__}",
                )
                exit_code = 1

    with Session(application_engine) as application_session:
        delivery_result = order_assembly_outbox.deliver_due_events(
            application_session,
            sender=lambda row: send_outbox_row_to_crm(
                row,
                crm_url=args.crm_url,
                token=token,
            ),
            limit=max(1, int(args.limit)),
            max_attempts=max(1, int(args.max_attempts)),
            retry_base_seconds=max(1, int(args.retry_base_seconds)),
        )
        application_session.commit()
    print(json.dumps({"outbox_delivery": delivery_result}, sort_keys=True))
    if delivery_result["retry"] or delivery_result["manual_review"]:
        exit_code = 1

    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.state_path) as state_connection:
        processed = set() if args.include_processed else load_processed(state_connection)
        pending_events = [event for event in issued_events if event.event_key not in processed]

        summary = {
            "mode": "apply" if args.apply else "no-send" if args.no_send else "dry-run",
            "since": _format_dt(since),
            "found": len(issued_events),
            "pending": len(pending_events),
            "already_processed": len(issued_events) - len(pending_events),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))

        for event in pending_events:
            crm_response = send_to_crm(
                event,
                crm_url=args.crm_url,
                token=token,
                dry_run=False,
            )
            ok = bool(crm_response.get("ok"))
            print_event_result(
                event,
                status="sent" if ok else "crm_rejected",
                crm_response=crm_response,
            )
            if ok:
                record_processed(state_connection, event, crm_response=crm_response)
            if not ok:
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
