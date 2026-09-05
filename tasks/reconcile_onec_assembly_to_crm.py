from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.db import session_scope
from app.infrastructure.db.engines import build_engine
from app.services import site_order_execution_ingest
from app.services import site_order_execution_reconciliation as execution_reconciliation
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_stage_outbox as stage_outbox_service
from infra.cron import order_fulfillment_sync as fulfillment_sync

DEFAULT_CRM_URL = "https://crm.master-mobile.ru/local/tools/mm_crm_1c_assembly_status.php"
DEFAULT_STATE_PATH = Path(".local/onec_assembly_crm_reconciler.sqlite3")
TOKEN_ENV_NAMES = (
    "MM_CRM_1C_ASSEMBLY_TOKEN",
    "CRM_1C_ASSEMBLY_TOKEN",
)
TRANSPORT_LEGACY_PHP = "legacy-php"
TRANSPORT_SERVICE_DB = "service-db"


@dataclass(frozen=True)
class AssemblyEvent:
    event_key: str
    crm_status: str
    event_at: datetime
    rtu_external_id: str
    rtu_number: str
    rtu_date: datetime | None
    onec_order_number: str
    site_order_number: str
    is_posted: bool
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
    parser.add_argument(
        "--transport",
        choices=(TRANSPORT_LEGACY_PHP, TRANSPORT_SERVICE_DB),
        default=os.environ.get("ONEC_ASSEMBLY_CRM_TRANSPORT", TRANSPORT_LEGACY_PHP),
        help=(
            "Event transport. legacy-php keeps the current bridge; service-db writes "
            "append-only evidence for the unified reconciler."
        ),
    )
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
        rtu_external_id=(data["rtu_external_id"] or "").strip(),
        rtu_number=(data["rtu_number"] or "").strip(),
        rtu_date=data["rtu_date"],
        onec_order_number=(data["onec_order_number"] or "").strip(),
        site_order_number=(data["site_order_number"] or "").strip(),
        is_posted=bool(data["is_posted"]),
        document_amount=(
            str(data["document_amount"]) if data.get("document_amount") is not None else None
        ),
    )


def fetch_assembly_events(
    onec_database_url: str, *, since: datetime, limit: int
) -> list[AssemblyEvent]:
    limit_clause = f"TOP ({max(1, int(limit))})"
    statement = text(f"""
        WITH crm_events AS (
            SELECT
                'assembled:' + CONVERT(varchar(34), hist._SimpleKey, 1) AS event_key,
                'assembled' AS crm_status,
                hist._Fld9450 AS event_at,
                CONVERT(varchar(34), rtu._IDRRef, 1) AS rtu_external_id,
                LTRIM(RTRIM(rtu._Number)) AS rtu_number,
                rtu._Date_Time AS rtu_date,
                LTRIM(RTRIM(ord._Number)) AS onec_order_number,
                NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') AS site_order_number,
                CASE WHEN rtu._Posted = 0x01 THEN 1 ELSE 0 END AS is_posted,
                CAST(NULL AS decimal(18, 2)) AS document_amount
            FROM dbo._InfoRg9448 AS hist WITH (NOLOCK)
            JOIN dbo._Document203 AS rtu WITH (NOLOCK)
                ON rtu._IDRRef = hist._Fld9449_RRRef
            JOIN dbo._Document132 AS ord WITH (NOLOCK)
                ON ord._IDRRef = rtu._Fld4939_RRRef
            WHERE hist._Fld9454 = N'Собран'
              AND hist._Fld9449_RTRef = 0x000000CB
              AND hist._Fld9450 >= :since
              AND rtu._Marked = 0x00
              AND NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') IS NOT NULL

            UNION ALL

            SELECT
                'issued-scan:' + CONVERT(varchar(34), scan_event._SimpleKey, 1) AS event_key,
                'issued' AS crm_status,
                scan_event._Fld9450 AS event_at,
                CONVERT(varchar(34), rtu._IDRRef, 1) AS rtu_external_id,
                LTRIM(RTRIM(rtu._Number)) AS rtu_number,
                rtu._Date_Time AS rtu_date,
                LTRIM(RTRIM(ord._Number)) AS onec_order_number,
                NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') AS site_order_number,
                CASE WHEN rtu._Posted = 0x01 THEN 1 ELSE 0 END AS is_posted,
                CAST(rtu._Fld4948 AS decimal(18, 2)) AS document_amount
            FROM dbo._InfoRg9448 AS scan_event WITH (NOLOCK)
            JOIN dbo._Document203 AS rtu WITH (NOLOCK)
                ON rtu._IDRRef = scan_event._Fld9449_RRRef
            JOIN dbo._Document132 AS ord WITH (NOLOCK)
                ON ord._IDRRef = rtu._Fld4939_RRRef
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
            rtu_external_id,
            rtu_number,
            rtu_date,
            onec_order_number,
            site_order_number,
            is_posted,
            document_amount
        FROM crm_events
        ORDER BY event_at ASC, rtu_date ASC, crm_status ASC
        """)
    engine = build_engine(onec_database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        return [_event_from_row(row) for row in connection.execute(statement, {"since": since})]


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
            event.rtu_number,
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
        "rtu": event.rtu_number,
    }
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


def persist_event_to_service_db(event: AssemblyEvent) -> dict[str, Any]:
    fact = site_order_execution_ingest.OnecExecutionFact(
        signal="issued" if event.crm_status == "issued" else "assembled",
        event_at=event.event_at,
        site_order_number=event.site_order_number,
        onec_order_number=event.onec_order_number or None,
        rtu_external_id=event.rtu_external_id or None,
        rtu_number=event.rtu_number or None,
        rtu_date=event.rtu_date,
        is_posted=event.is_posted,
        document_amount=(Decimal(event.document_amount) if event.document_amount else None),
    )
    with session_scope() as session:
        result = site_order_execution_ingest.ingest_onec_execution_fact(session, fact)
    return {
        "ok": True,
        "transport": TRANSPORT_SERVICE_DB,
        "event_id": result.event_id,
        "duplicate": result.duplicate,
        "message": "duplicate_event" if result.duplicate else "execution_event_persisted",
    }


def reconcile_service_db_orders(
    order_numbers: list[str],
    *,
    apply: bool,
) -> dict[str, Any]:
    unique_orders = [item for item in dict.fromkeys(order_numbers) if item]
    if not unique_orders:
        return {"orders": 0, "snapshots": 0, "outbox": 0, "worker": 0}
    settings = get_settings()
    if not settings.order_fulfillment_bitrix_webhook_url:
        raise RuntimeError("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
    client = fulfillment.BitrixChatClient(settings.order_fulfillment_bitrix_webhook_url)
    response = client.call(
        "crm.deal.list",
        {
            "filter": {f"@{fulfillment.CRM_ORDER_NUMBER_FIELD}": unique_orders},
            "select": [
                *fulfillment.CRM_REVIEW_SELECT_FIELDS,
                fulfillment_sync.CRM_ASSEMBLED_FIELD,
            ],
            "order": {"ID": "ASC"},
        },
    )
    deals = [
        deal
        for item in response.get("result") or []
        if (deal := fulfillment.bitrix_deal_from_payload(item)) is not None
    ]
    order_statuses = fulfillment_sync.fetch_sale_order_statuses(unique_orders)
    settlements = fulfillment_sync.fetch_onec_order_settlements(unique_orders, strict=True)
    rtu_signals = fulfillment_sync.query_rtu_signal_by_orders(unique_orders)
    order_states = fulfillment_sync.query_onec_order_states_by_orders(unique_orders)
    for order_number, state in order_states.items():
        rtu_signals.setdefault(order_number, {}).update(state)
    snapshots = fulfillment_sync.build_execution_snapshots(
        deals,
        order_statuses=order_statuses,
        onec_settlements=settlements,
        rtu_signals=rtu_signals,
        cutover_at=settings.order_fulfillment_execution_cutover_at,
        onec_evidence_available=True,
        dismantling_started_at=fulfillment_sync._dismantling_started_at_for_deals(  # noqa: SLF001
            client,
            deals,
        ),
    )
    decisions = [execution_reconciliation.decide_execution_stage(item) for item in snapshots]
    persisted = fulfillment_sync.persist_execution_snapshots(snapshots, decisions)
    with session_scope() as session:
        worker = stage_outbox_service.process_stage_outbox(
            session,
            client=client,
            apply=apply,
            settings=settings,
            limit=max(1, len(snapshots)),
            site_order_numbers=unique_orders,
        )
    return {
        "orders": len(unique_orders),
        "snapshots": len(snapshots),
        "outbox": sum(item.outbox_id is not None for item in persisted),
        "worker": len(worker),
    }


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
        "rtu": event.rtu_number,
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

    token = "" if args.no_send or args.transport == TRANSPORT_SERVICE_DB else _token_from_env()
    if not args.no_send and args.transport == TRANSPORT_LEGACY_PHP and not token:
        names = " or ".join(TOKEN_ENV_NAMES)
        raise SystemExit(f"CRM token is not configured. Set {names}.")
    if (
        args.apply
        and args.transport == TRANSPORT_SERVICE_DB
        and not (
            settings.order_fulfillment_execution_master_enabled
            and settings.order_fulfillment_execution_ingest_enabled
        )
    ):
        raise SystemExit(
            "service-db transport requires ORDER_FULFILLMENT_EXECUTION_MASTER_ENABLED=true "
            "and ORDER_FULFILLMENT_EXECUTION_INGEST_ENABLED=true"
        )

    since = datetime.now() - timedelta(hours=max(1, int(args.since_hours)))
    events = fetch_assembly_events(
        settings.onec_database_url,
        since=since,
        limit=max(1, int(args.limit)),
    )

    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.state_path) as state_connection:
        processed = set() if args.include_processed else load_processed(state_connection)
        pending_events = [event for event in events if event.event_key not in processed]

        summary = {
            "mode": "apply" if args.apply else "no-send" if args.no_send else "dry-run",
            "transport": args.transport,
            "since": _format_dt(since),
            "found": len(events),
            "pending": len(pending_events),
            "already_processed": len(events) - len(pending_events),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))

        exit_code = 0
        affected_service_db_orders: list[str] = []
        for event in pending_events:
            if args.no_send:
                print_event_result(event, status="candidate")
                continue

            if args.transport == TRANSPORT_SERVICE_DB:
                crm_response = (
                    persist_event_to_service_db(event)
                    if args.apply
                    else {
                        "ok": True,
                        "transport": TRANSPORT_SERVICE_DB,
                        "dry_run": True,
                        "message": "execution_event_candidate",
                    }
                )
            else:
                crm_response = send_to_crm(
                    event,
                    crm_url=args.crm_url,
                    token=token,
                    dry_run=not args.apply,
                )
            ok = bool(crm_response.get("ok"))
            print_event_result(
                event,
                status="sent" if ok else "crm_rejected",
                crm_response=crm_response,
            )
            if ok and args.apply:
                record_processed(state_connection, event, crm_response=crm_response)
                if args.transport == TRANSPORT_SERVICE_DB:
                    affected_service_db_orders.append(event.site_order_number)
            if not ok:
                exit_code = 1

        if affected_service_db_orders and args.transport == TRANSPORT_SERVICE_DB:
            try:
                reconcile_summary = reconcile_service_db_orders(
                    affected_service_db_orders,
                    apply=args.apply,
                )
                print(
                    json.dumps(
                        {"service_db_reconciliation": reconcile_summary},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {"service_db_reconciliation_error": str(exc)},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
