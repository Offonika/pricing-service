from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.infrastructure.db import session_scope

DEFAULT_CONTROL_NAMES = (
    "Байрамов Эльвин Эйваз Оглы",
    "Хыдыров Ахмет",
    "Куценко Дмитрий Алексеевич",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check authoritative receivable snapshots in local PostgreSQL without 1C access."
    )
    parser.add_argument("--snapshot-date", required=True, help="Snapshot date in YYYY-MM-DD")
    parser.add_argument(
        "--control-name",
        action="append",
        default=[],
        help="Counterparty name to include in control output; may be repeated",
    )
    return parser.parse_args()


def _decimal_to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_authoritative_snapshot_report(
    session: Session,
    *,
    snapshot_date: date,
    control_names: tuple[str, ...],
) -> dict[str, Any]:
    snapshot_row = (
        session.execute(
            text("""
                select
                    snapshot_date::text as snapshot_date,
                    max(updated_at)::text as updated_at,
                    count(*) as row_count
                from receivable_balance_snapshot
                where snapshot_date = :snapshot_date
                group by snapshot_date
                """),
            {"snapshot_date": snapshot_date},
        )
        .mappings()
        .first()
    )

    case_rows = (
        session.execute(
            text("""
                select
                    segment,
                    count(*) as row_count,
                    round(sum(current_balance)::numeric, 2) as total_balance
                from receivable_case
                where snapshot_date = :snapshot_date
                group by segment
                order by segment
                """),
            {"snapshot_date": snapshot_date},
        )
        .mappings()
        .all()
    )

    synthetic_row = (
        session.execute(
            text("""
                select
                    (select count(*)
                     from receivable_balance_snapshot
                     where snapshot_date = :snapshot_date
                       and counterparty_ref like 'synthetic:%') as balance_snapshot_rows,
                    (select count(*)
                     from receivable_reconciliation_snapshot
                     where snapshot_date = :snapshot_date
                       and counterparty_ref like 'synthetic:%') as reconciliation_snapshot_rows,
                    (select count(*)
                     from receivable_case
                     where snapshot_date = :snapshot_date
                       and counterparty_ref like 'synthetic:%') as case_rows
                """),
            {"snapshot_date": snapshot_date},
        )
        .mappings()
        .one()
    )

    controls = (
        session.execute(
            text("""
                select
                    counterparty_name,
                    current_balance,
                    activity_segment,
                    aged_bucket,
                    coalesce(current_manager_name, '<NULL>') as current_manager_name,
                    coalesce(origin_document_date::text, '<NULL>') as origin_document_date
                from receivable_balance_snapshot
                where snapshot_date = :snapshot_date
                  and counterparty_name = any(:control_names)
                order by counterparty_name
                """),
            {"snapshot_date": snapshot_date, "control_names": list(control_names)},
        )
        .mappings()
        .all()
    )

    return {
        "snapshot": dict(snapshot_row) if snapshot_row is not None else None,
        "synthetic": dict(synthetic_row),
        "case_segments": [
            {
                "segment": row["segment"],
                "row_count": row["row_count"],
                "total_balance": _decimal_to_float(row["total_balance"]),
            }
            for row in case_rows
        ],
        "controls": [
            {
                "counterparty_name": row["counterparty_name"],
                "current_balance": _decimal_to_float(row["current_balance"]),
                "activity_segment": row["activity_segment"],
                "aged_bucket": row["aged_bucket"],
                "current_manager_name": row["current_manager_name"],
                "origin_document_date": row["origin_document_date"],
            }
            for row in controls
        ],
    }


def main() -> None:
    args = _parse_args()
    snapshot_date = date.fromisoformat(args.snapshot_date)
    control_names = tuple(args.control_name) or DEFAULT_CONTROL_NAMES
    with session_scope(read_only=True) as session:
        result = build_authoritative_snapshot_report(
            session,
            snapshot_date=snapshot_date,
            control_names=control_names,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
