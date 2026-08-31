from __future__ import annotations

import argparse
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.infrastructure.db import build_application_engine, build_onec_engine, session_scope
from app.models import Base, ReceivableBalanceSnapshot
from app.services.receivables import (
    OneCReceivableLedgerExtractor,
    ReceivableLedgerRow,
    fetch_employee_counterparty_refs_from_onec,
    fetch_staff_members_from_onec,
    sync_receivable_ledger,
)
from app.services.staffing import StaffMemberRow, upsert_staff_members


@contextmanager
def _onec_engine_scope(
    database_url: str,
    *,
    query_timeout_seconds: int | float,
    login_timeout_seconds: int | float,
) -> Iterator[Engine]:
    engine = build_onec_engine(
        database_url,
        query_timeout_seconds=query_timeout_seconds,
        login_timeout_seconds=login_timeout_seconds,
    )
    try:
        yield engine
    finally:
        engine.dispose()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _normalize_name(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").strip().split())


def _parse_decimal(value: str) -> Decimal:
    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not cleaned:
        return Decimal("0")
    return Decimal(cleaned)


def filter_ledger_events(
    events: list[ReceivableLedgerRow],
    *,
    contract_kind_names: set[str] | None = None,
    source_layer: str | None = None,
) -> list[ReceivableLedgerRow]:
    filtered: list[ReceivableLedgerRow] = []
    for event in events:
        if source_layer and event.source_layer != source_layer:
            continue
        if contract_kind_names and event.contract_kind_name not in contract_kind_names:
            continue
        filtered.append(event)
    return filtered


def parse_report_opening_balances(path: Path) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = {}
    current_group: list[list[str]] = []

    def flush_group() -> None:
        if not current_group:
            return
        head = current_group[0]
        if len(head) < 3:
            current_group.clear()
            return
        name = _normalize_name(head[1] if len(head) > 1 else "")
        opening_raw = head[2] if len(head) > 2 else ""
        if not name or name in {
            "Контрагент",
            "Договор контрагента",
            "ДоговорКонтрагента.Вид договора",
            "Итог",
        }:
            current_group.clear()
            return
        if opening_raw.strip():
            balances[name] = _parse_decimal(opening_raw)
        current_group.clear()

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            flush_group()
            continue
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 3:
            continue
        if parts[1] and parts[2]:
            current_group.append(parts)

    flush_group()
    return balances


def parse_report_contract_balances(path: Path) -> dict[str, list[tuple[str, Decimal]]]:
    balances: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
    current_group: list[list[str]] = []

    def flush_group() -> None:
        if not current_group:
            return
        head = current_group[0]
        if len(head) < 3:
            current_group.clear()
            return
        name = _normalize_name(head[1] if len(head) > 1 else "")
        if not name or name in {
            "Контрагент",
            "Договор контрагента",
            "ДоговорКонтрагента.Вид договора",
            "Итог",
        }:
            current_group.clear()
            return
        for row in current_group[1:]:
            if len(row) < 3:
                continue
            contract_name = _normalize_name(row[1] if len(row) > 1 else "")
            opening_raw = row[2] if len(row) > 2 else ""
            if not contract_name or not opening_raw.strip():
                continue
            balances[name].append((contract_name, _parse_decimal(opening_raw)))
        current_group.clear()

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            flush_group()
            continue
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 3:
            continue
        if parts[1] and parts[2]:
            current_group.append(parts)

    flush_group()
    return dict(balances)


def build_temp_snapshots(
    *,
    operations_sql: str,
    opening_balance_date: date | None,
    window_start: datetime | None,
    window_end: datetime | None,
    snapshot_date: date | None,
    onec_url: str,
    onec_query_timeout_seconds: int | float,
    onec_login_timeout_seconds: int | float,
    contract_kind_names: set[str] | None = None,
    source_layer: str | None = None,
) -> tuple[
    dict[str, Decimal],
    dict[str, Decimal],
    dict[str, list[dict[str, Decimal | str | None]]],
]:
    with _onec_engine_scope(
        onec_url,
        query_timeout_seconds=onec_query_timeout_seconds,
        login_timeout_seconds=onec_login_timeout_seconds,
    ) as onec_engine:
        employee_refs = fetch_employee_counterparty_refs_from_onec(onec_engine)
        staff_rows = [
            StaffMemberRow.from_mapping(item, default_source="onec_physical_person")
            for item in fetch_staff_members_from_onec(onec_engine)
        ]
        extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=operations_sql)
        events = extractor.fetch_receivable_events(
            window_start=window_start,
            window_end=window_end,
            opening_balance_date=opening_balance_date,
        )
    events = filter_ledger_events(
        events,
        contract_kind_names=contract_kind_names,
        source_layer=source_layer,
    )

    opening_by_name: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    opening_contracts_by_name: dict[
        str, dict[tuple[str | None, str | None, str | None], Decimal]
    ] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    for event in events:
        if event.event_type != "opening_balance":
            continue
        if event.counterparty_name:
            normalized_name = _normalize_name(event.counterparty_name)
            opening_by_name[normalized_name] += Decimal(event.amount_delta)
            opening_contracts_by_name[normalized_name][
                (event.contract_ref, event.contract_name, event.contract_kind_name)
            ] += Decimal(event.amount_delta)

    with tempfile.NamedTemporaryFile(prefix="receivables-compare-", suffix=".sqlite") as tmp:
        temporary_database_url = f"sqlite:///{tmp.name}"
        schema_engine = build_application_engine(temporary_database_url)
        try:
            Base.metadata.create_all(schema_engine)
        finally:
            schema_engine.dispose()
        with session_scope(database_url=temporary_database_url) as session:
            upsert_staff_members(session, staff_rows)
            sync_receivable_ledger(
                session,
                events,
                snapshot_date=snapshot_date,
                employee_counterparty_refs=employee_refs,
            )

            snapshot_by_name: dict[str, Decimal] = {}
            if snapshot_date is not None:
                rows = (
                    session.execute(
                        select(ReceivableBalanceSnapshot).where(
                            ReceivableBalanceSnapshot.snapshot_date == snapshot_date
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    if row.counterparty_name:
                        snapshot_by_name[_normalize_name(row.counterparty_name)] = Decimal(
                            row.current_balance
                        )
            opening_contracts_payload: dict[str, list[dict[str, Decimal | str | None]]] = {}
            for counterparty_name, contracts in opening_contracts_by_name.items():
                items: list[dict[str, Decimal | str | None]] = []
                for (contract_ref, contract_name, contract_kind_name), amount in sorted(
                    contracts.items(),
                    key=lambda item: (
                        abs(item[1]),
                        item[0][1] or "",
                        item[0][0] or "",
                    ),
                    reverse=True,
                ):
                    items.append(
                        {
                            "contract_ref": contract_ref,
                            "contract_name": contract_name,
                            "contract_kind_name": contract_kind_name,
                            "amount": amount,
                        }
                    )
                opening_contracts_payload[counterparty_name] = items
            return dict(opening_by_name), snapshot_by_name, opening_contracts_payload


def fetch_employee_summary_opening_breakdown(
    *,
    onec_url: str,
    onec_query_timeout_seconds: int | float,
    onec_login_timeout_seconds: int | float,
    opening_balance_date: date,
    counterparty_names: list[str],
    contract_kind_names: set[str] | None = None,
) -> dict[str, list[dict[str, object]]]:
    sql = text("""
SELECT
  c._Description AS counterparty_name,
  master.dbo.fn_varbintohexstr(t._Fld7615RRef) AS contract_ref,
  contract._Description AS contract_name,
  CASE master.dbo.fn_varbintohexstr(contract._Fld515RRef)
    WHEN '0x9363c6f0a10557bf4822a55db4862286' THEN N'С покупателем'
    WHEN '0x95db9a602e142ed645d7ccf13094909f' THEN N'С поставщиком'
    WHEN '0xa49b7e34b5f2cbb643d8f36270f8009f' THEN N'Прочее'
    ELSE N'Неизвестно'
  END AS contract_kind_name,
  master.dbo.fn_varbintohexstr(t._Fld7617RRef) AS return_ref,
  master.dbo.fn_varbintohexstr(t._Fld7616_RTRef) AS deal_tref,
  CONVERT(int, COALESCE(d._Marked, 0x00)) AS marked,
  CONVERT(int, COALESCE(d._Posted, 0x00)) AS posted,
  COUNT(*) AS row_count,
  SUM(CAST(t._Fld7620 AS decimal(18,2))) AS amount_sum,
  SUM(
    CASE
      WHEN d._Marked = 0x01 AND d._Posted = 0x00 THEN CAST(t._Fld7620 AS decimal(18,2))
      ELSE 0
    END
  ) AS stale_sum
FROM _AccumRgT7622 t WITH (NOLOCK)
JOIN _Reference54 c WITH (NOLOCK)
  ON c._IDRRef = t._Fld7619RRef
LEFT JOIN _Reference37 contract WITH (NOLOCK)
  ON contract._IDRRef = t._Fld7615RRef
LEFT JOIN _Document132 d WITH (NOLOCK)
  ON t._Fld7616_RTRef = 0x00000084 AND d._IDRRef = t._Fld7616_RRRef
WHERE t._Period = :opening_period
  AND c._Description = :counterparty_name
GROUP BY
  c._Description,
  t._Fld7615RRef,
  contract._Description,
  contract._Fld515RRef,
  t._Fld7617RRef,
  t._Fld7616_RTRef,
  d._Marked,
  d._Posted
ORDER BY
  ABS(SUM(CAST(t._Fld7620 AS decimal(18,2)))) DESC,
  contract._Description,
  t._Fld7617RRef,
  t._Fld7616_RTRef
        """)
    result: dict[str, list[dict[str, object]]] = {}
    with _onec_engine_scope(
        onec_url,
        query_timeout_seconds=onec_query_timeout_seconds,
        login_timeout_seconds=onec_login_timeout_seconds,
    ) as engine:
        with engine.connect() as conn:
            for counterparty_name in counterparty_names:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        sql,
                        {
                            "opening_period": datetime.combine(
                                opening_balance_date, datetime.min.time()
                            ),
                            "counterparty_name": counterparty_name,
                        },
                    ).mappings()
                ]
                if contract_kind_names:
                    rows = [
                        row for row in rows if row.get("contract_kind_name") in contract_kind_names
                    ]
                result[counterparty_name] = rows
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare employee receivable opening from live 1C SQL against text report"
    )
    parser.add_argument(
        "--sql-file",
        default="samples/onec_receivables_hybrid_opening_plus_detail.sql",
        help="Path to normalized SQL projection",
    )
    parser.add_argument(
        "--report-file",
        default="docs/Ведомость с сотрудниками.txt",
        help="Path to text report exported from 1C",
    )
    parser.add_argument("--opening-balance-date", default="2025-01-01")
    parser.add_argument("--window-start", default="2025-01-01T00:00:00")
    parser.add_argument("--window-end")
    parser.add_argument(
        "--snapshot-date",
        help="Optional snapshot date for temp SQLite sync",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="Counterparty name to print; may be repeated",
    )
    parser.add_argument(
        "--contract-kind-name",
        action="append",
        default=[],
        help="Optional exact contract kind name filter; may be repeated",
    )
    parser.add_argument(
        "--source-layer",
        default=None,
        help="Optional source layer filter, e.g. employee_summary or regular_receivables",
    )
    parser.add_argument(
        "--contract-details",
        action="append",
        default=[],
        help="Counterparty name to print contract-level details for; may be repeated",
    )
    parser.add_argument(
        "--opening-breakdown",
        action="append",
        default=[],
        help="Counterparty name to print live _AccumRgT7622 opening breakdown for; may be repeated",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many rows to print when --name is not specified",
    )
    parser.add_argument(
        "--onec-url",
        default=None,
        help="Override ONEC database URL; defaults to env ONEC_DATABASE_URL",
    )
    args = parser.parse_args()

    sql_path = Path(args.sql_file)
    report_path = Path(args.report_file)
    if not sql_path.exists():
        raise SystemExit(f"SQL file not found: {sql_path}")
    if not report_path.exists():
        raise SystemExit(f"Report file not found: {report_path}")

    settings = get_settings()
    onec_url = args.onec_url or settings.onec_database_url

    if not onec_url:
        raise SystemExit("ONEC_DATABASE_URL is not configured")

    report_opening = parse_report_opening_balances(report_path)
    contract_kind_names = {
        _normalize_name(item) for item in args.contract_kind_name if item.strip()
    }
    sql_opening, snapshot_balances, sql_contracts = build_temp_snapshots(
        operations_sql=sql_path.read_text(encoding="utf-8"),
        opening_balance_date=_parse_date(args.opening_balance_date),
        window_start=_parse_datetime(args.window_start),
        window_end=_parse_datetime(args.window_end),
        snapshot_date=_parse_date(args.snapshot_date),
        onec_url=onec_url,
        onec_query_timeout_seconds=settings.onec_query_timeout_seconds,
        onec_login_timeout_seconds=settings.onec_login_timeout_seconds,
        contract_kind_names=contract_kind_names or None,
        source_layer=args.source_layer,
    )

    names = [_normalize_name(item) for item in args.name]
    if not names:
        common_names = sorted(set(report_opening) & set(sql_opening))
        common_names.sort(
            key=lambda item: abs(report_opening[item] - sql_opening[item]),
            reverse=True,
        )
        names = common_names[: args.limit]

    print("name\treport_opening\tsql_opening\tdiff\tsnapshot_balance")
    for name in names:
        report_value = report_opening.get(name)
        sql_value = sql_opening.get(name)
        snapshot_value = snapshot_balances.get(name)
        diff = None
        if report_value is not None and sql_value is not None:
            diff = report_value - sql_value
        print(
            "\t".join(
                [
                    name,
                    "" if report_value is None else str(report_value),
                    "" if sql_value is None else str(sql_value),
                    "" if diff is None else str(diff),
                    "" if snapshot_value is None else str(snapshot_value),
                ]
            )
        )

    contract_detail_names = [_normalize_name(item) for item in args.contract_details]
    if contract_detail_names:
        report_contracts = parse_report_contract_balances(report_path)
        for name in contract_detail_names:
            print()
            print(f"[contracts] {name}")
            print("report_contract_name\treport_amount")
            for contract_name, amount in report_contracts.get(name, []):
                print(f"{contract_name}\t{amount}")
            print("sql_contract_ref\tsql_contract_name\tsql_contract_kind\tsql_amount")
            for item in sql_contracts.get(name, []):
                print(
                    "\t".join(
                        [
                            "" if item["contract_ref"] is None else str(item["contract_ref"]),
                            "" if item["contract_name"] is None else str(item["contract_name"]),
                            (
                                ""
                                if item["contract_kind_name"] is None
                                else str(item["contract_kind_name"])
                            ),
                            str(item["amount"]),
                        ]
                    )
                )

    opening_breakdown_names = [_normalize_name(item) for item in args.opening_breakdown]
    if opening_breakdown_names:
        opening_balance_date = _parse_date(args.opening_balance_date)
        if opening_balance_date is None:
            raise SystemExit("--opening-breakdown requires --opening-balance-date")
        rows_by_name = fetch_employee_summary_opening_breakdown(
            onec_url=onec_url,
            onec_query_timeout_seconds=settings.onec_query_timeout_seconds,
            onec_login_timeout_seconds=settings.onec_login_timeout_seconds,
            opening_balance_date=opening_balance_date,
            counterparty_names=opening_breakdown_names,
            contract_kind_names=contract_kind_names or None,
        )
        for name in opening_breakdown_names:
            print()
            print(f"[opening_breakdown] {name}")
            print(
                "\t".join(
                    [
                        "contract_ref",
                        "contract_name",
                        "contract_kind_name",
                        "return_ref",
                        "deal_tref",
                        "marked",
                        "posted",
                        "row_count",
                        "amount_sum",
                        "stale_sum",
                    ]
                )
            )
            for row in rows_by_name.get(name, []):
                print(
                    "\t".join(
                        [
                            "" if row["contract_ref"] is None else str(row["contract_ref"]),
                            "" if row["contract_name"] is None else str(row["contract_name"]),
                            (
                                ""
                                if row["contract_kind_name"] is None
                                else str(row["contract_kind_name"])
                            ),
                            "" if row["return_ref"] is None else str(row["return_ref"]),
                            "" if row["deal_tref"] is None else str(row["deal_tref"]),
                            str(row["marked"]),
                            str(row["posted"]),
                            str(row["row_count"]),
                            str(row["amount_sum"]),
                            str(row["stale_sum"]),
                        ]
                    )
                )


if __name__ == "__main__":
    main()
