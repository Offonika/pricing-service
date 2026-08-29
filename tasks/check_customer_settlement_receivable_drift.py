from __future__ import annotations

import argparse
import json
import os
import re
import stat
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import (
    build_readonly_postgres_engine,
    get_application_session_factory,
    get_onec_engine,
)
from app.services.customer_settlement_receivable_drift import (
    CustomerSettlementReceivableDriftError,
    compare_customer_settlement_with_receivables,
)
from app.services.customer_settlement_source import fetch_customer_settlement_balances
from app.services.customer_settlements import (
    CustomerSettlementContextBusyError,
    CustomerSettlementRuntimeGuardError,
    active_pilot_counterparty_refs,
    assert_expected_application_database,
    try_customer_settlement_context_read_lock,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DATABASE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,63}")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("completed date must be YYYY-MM-DD") from exc


def _default_completed_date() -> date:
    return datetime.now(MOSCOW_TZ).date() - timedelta(days=1)


def _read_database_url_from_env_file(path_value: str) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        raise CustomerSettlementReceivableDriftError("receivable_env_path_not_absolute")
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError as exc:
        raise CustomerSettlementReceivableDriftError("receivable_env_file_unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & 0o022:
        raise CustomerSettlementReceivableDriftError("receivable_env_file_is_not_secure")
    if file_stat.st_uid not in {0, os.geteuid()}:
        raise CustomerSettlementReceivableDriftError("receivable_env_file_owner_invalid")

    database_url: str | None = None
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if ENV_NAME_RE.fullmatch(key) is None:
            raise CustomerSettlementReceivableDriftError("receivable_env_file_invalid")
        if key != "DATABASE_URL":
            continue
        if database_url is not None:
            raise CustomerSettlementReceivableDriftError("receivable_database_url_duplicated")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        database_url = value.strip()
    if not database_url:
        raise CustomerSettlementReceivableDriftError("receivable_database_url_missing")
    try:
        backend_name = make_url(database_url).get_backend_name()
    except Exception as exc:
        raise CustomerSettlementReceivableDriftError("receivable_database_url_invalid") from exc
    if backend_name != "postgresql":
        raise CustomerSettlementReceivableDriftError("receivable_database_is_not_postgresql")
    return database_url


def _build_readonly_receivable_engine(database_url: str) -> Engine:
    return build_readonly_postgres_engine(database_url, pool_size=1, max_overflow=0)


def _load_receivable_rows(
    engine: Engine,
    *,
    completed_date: date,
    counterparty_refs: tuple[str, ...],
    expected_database_name: str,
) -> tuple[list[tuple[object, object]], int]:
    if DATABASE_NAME_RE.fullmatch(expected_database_name) is None:
        raise CustomerSettlementReceivableDriftError("invalid_expected_receivable_database_name")
    statement = text("""
        SELECT lower(counterparty_ref) AS counterparty_ref, current_balance
        FROM receivable_balance_snapshot
        WHERE snapshot_date = :snapshot_date
          AND lower(counterparty_ref) IN :counterparty_refs
        """).bindparams(bindparam("counterparty_refs", expanding=True))
    with engine.connect() as connection:
        read_only = str(connection.scalar(text("SHOW transaction_read_only")) or "").lower()
        if read_only != "on":
            raise CustomerSettlementReceivableDriftError("receivable_database_is_not_read_only")
        current_database = str(connection.scalar(text("SELECT current_database()")) or "")
        if current_database != expected_database_name:
            raise CustomerSettlementReceivableDriftError("receivable_database_name_mismatch")
        total_rows = int(
            connection.scalar(
                text("""
                    SELECT COUNT(*)
                    FROM receivable_balance_snapshot
                    WHERE snapshot_date = :snapshot_date
                    """),
                {"snapshot_date": completed_date},
            )
            or 0
        )
        if total_rows <= 0:
            raise CustomerSettlementReceivableDriftError("receivable_snapshot_is_missing")
        rows = list(
            connection.execute(
                statement,
                {
                    "snapshot_date": completed_date,
                    "counterparty_refs": [value.lower() for value in counterparty_refs],
                },
            ).tuples()
        )
    return rows, total_rows


def _safe_error_payload(error_code: str) -> dict[str, object]:
    return {"status": "critical", "error_code": error_code}


def _close_session(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.rollback()
    except Exception:
        pass
    try:
        session.close()
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only drift check: customer settlements versus receivables snapshot"
    )
    parser.add_argument(
        "--completed-date",
        type=_parse_date,
        default=None,
        help="Completed Europe/Moscow day; default is yesterday",
    )
    parser.add_argument(
        "--receivable-env-file",
        default=os.getenv("CUSTOMER_SETTLEMENTS_RECEIVABLE_ENV_FILE"),
    )
    parser.add_argument(
        "--expected-receivable-database-name",
        default=os.getenv("CUSTOMER_SETTLEMENTS_RECEIVABLE_EXPECTED_DATABASE_NAME"),
    )
    parser.add_argument(
        "--expected-pilot-count",
        type=int,
        default=int(os.getenv("CUSTOMER_SETTLEMENTS_EXPECTED_PILOT_COUNT", "10")),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session: Session | None = None
    onec_engine: Engine | None = None
    receivable_engine: Engine | None = None
    try:
        if not args.receivable_env_file:
            raise CustomerSettlementReceivableDriftError("receivable_env_file_missing")
        if not args.expected_receivable_database_name:
            raise CustomerSettlementReceivableDriftError(
                "expected_receivable_database_name_missing"
            )
        if args.expected_pilot_count <= 0:
            raise CustomerSettlementReceivableDriftError("invalid_expected_pilot_count")

        settings = get_settings()
        if (
            settings.environment.strip().lower() != "staging"
            or settings.customer_settlements_shadow_enabled is not True
            or settings.customer_settlements_enabled is not False
            or settings.customer_settlements_eligibility_enabled is not False
        ):
            raise CustomerSettlementReceivableDriftError("shadow_runtime_guard_failed")

        session = get_application_session_factory()()
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        if not try_customer_settlement_context_read_lock(session):
            raise CustomerSettlementContextBusyError("customer_settlement_context_busy")
        counterparty_refs = active_pilot_counterparty_refs(session)
        if len(counterparty_refs) != args.expected_pilot_count:
            raise CustomerSettlementReceivableDriftError("pilot_count_mismatch")

        completed_date = args.completed_date or _default_completed_date()
        source_as_of = datetime.combine(
            completed_date + timedelta(days=1),
            time.min,
            tzinfo=MOSCOW_TZ,
        ).astimezone(ZoneInfo("UTC"))
        onec_engine = get_onec_engine()
        source = fetch_customer_settlement_balances(
            onec_engine,
            organization_ref=str(settings.customer_settlements_organization_ref or ""),
            organization_guid=str(settings.customer_settlements_organization_guid or ""),
            opening_organization_field=str(
                settings.customer_settlements_opening_organization_field or ""
            ),
            movement_organization_field=str(
                settings.customer_settlements_movement_organization_field or ""
            ),
            counterparty_refs=counterparty_refs,
            query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
            onec_timezone="Europe/Moscow",
            as_of=source_as_of,
            max_counterparties=settings.customer_settlements_max_scope_users,
        )

        receivable_database_url = _read_database_url_from_env_file(str(args.receivable_env_file))
        receivable_engine = _build_readonly_receivable_engine(receivable_database_url)
        receivable_rows, receivable_total_rows = _load_receivable_rows(
            receivable_engine,
            completed_date=completed_date,
            counterparty_refs=counterparty_refs,
            expected_database_name=str(args.expected_receivable_database_name),
        )
        result = compare_customer_settlement_with_receivables(
            completed_date=completed_date,
            source_as_of=source.as_of,
            expected_pilot_count=args.expected_pilot_count,
            source_rows=((row.counterparty_ref, row.signed_balance) for row in source.balances),
            receivable_rows=receivable_rows,
        )
        payload = result.safe_payload()
        payload["receivable_snapshot_total_rows"] = receivable_total_rows
        payload["source_duration_seconds"] = round(source.duration_seconds, 3)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if result.status == "ok" else 2
    except CustomerSettlementReceivableDriftError as exc:
        print(json.dumps(_safe_error_payload(str(exc)), ensure_ascii=False, sort_keys=True))
        return 2
    except CustomerSettlementContextBusyError:
        print(
            json.dumps(
                _safe_error_payload("customer_settlement_context_busy"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except CustomerSettlementRuntimeGuardError:
        print(
            json.dumps(
                _safe_error_payload("runtime_database_guard_failed"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                _safe_error_payload("receivable_drift_check_failed"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    finally:
        _close_session(session)
        if onec_engine is not None:
            onec_engine.dispose()
        if receivable_engine is not None:
            receivable_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
