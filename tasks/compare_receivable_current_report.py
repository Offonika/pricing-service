from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import ReceivableBalanceSnapshot
from app.services.bi import _buyers_snapshot_total, get_receivables_contract_balances
from app.services.receivables import (
    CurrentBalanceCounterpartyFilterMode,
    fetch_current_balances_from_onec,
    load_receivable_current_balance_override_payload,
)


def _normalize_counterparty_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).replace("\xa0", " ").split()).strip()
    if not cleaned:
        return None
    return " ".join(cleaned.casefold().split())


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _sum_balances(values: dict[str, Decimal]) -> Decimal:
    return _quantize_amount(sum(values.values(), Decimal("0.00")))


def _aggregate_balance_snapshot_by_name(
    session: Session,
    *,
    snapshot_date: date,
) -> tuple[dict[str, Decimal], dict[str, str], int]:
    rows = session.execute(
        select(
            ReceivableBalanceSnapshot.counterparty_name,
            ReceivableBalanceSnapshot.current_balance,
        ).where(ReceivableBalanceSnapshot.snapshot_date == snapshot_date)
    ).all()

    balances: dict[str, Decimal] = {}
    display_names: dict[str, str] = {}
    for counterparty_name, current_balance in rows:
        key = _normalize_counterparty_name(counterparty_name)
        if key is None:
            continue
        balances[key] = _quantize_amount(
            balances.get(key, Decimal("0.00")) + Decimal(str(current_balance))
        )
        display_names.setdefault(key, counterparty_name or key)
    return balances, display_names, len(rows)


def _aggregate_buyers_rub_only_by_name(
    session: Session,
    *,
    snapshot_date: date,
) -> tuple[dict[str, Decimal], dict[str, str], int]:
    rows = get_receivables_contract_balances(
        session,
        snapshot_date=snapshot_date,
        buyers_rub_only=True,
    )

    balances: dict[str, Decimal] = {}
    display_names: dict[str, str] = {}
    for row in rows:
        counterparty_name = row.get("counterparty_name")
        key = _normalize_counterparty_name(counterparty_name)
        if key is None:
            continue
        balances[key] = _quantize_amount(
            balances.get(key, Decimal("0.00")) + Decimal(str(row["current_balance"]))
        )
        display_names.setdefault(key, str(counterparty_name or key))
    return balances, display_names, len(rows)


def _aggregate_authoritative_rows_by_name(rows) -> tuple[dict[str, Decimal], dict[str, str], int]:
    balances: dict[str, Decimal] = {}
    display_names: dict[str, str] = {}
    row_count = 0
    for row in rows:
        row_count += 1
        key = _normalize_counterparty_name(row.counterparty_name)
        if key is None:
            continue
        balances[key] = _quantize_amount(
            balances.get(key, Decimal("0.00")) + Decimal(str(row.current_balance))
        )
        display_names.setdefault(key, str(row.counterparty_name or key))
    return balances, display_names, row_count


def _buyers_rub_only_resolution(
    session: Session,
    *,
    snapshot_date: date,
) -> dict[str, Any]:
    direct_total = _buyers_snapshot_total(session, snapshot_date=snapshot_date)
    return {
        "mode": "direct_snapshot",
        "base_snapshot_date": None,
        "direct_total": direct_total,
    }


def _build_candidate_diff_summary(
    *,
    file_balances: dict[str, Decimal],
    candidate_balances: dict[str, Decimal],
    display_names: dict[str, str],
    top: int,
) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []
    mismatch_count = 0
    missing_in_candidate_count = 0
    extra_in_candidate_count = 0

    for key in sorted(set(file_balances) | set(candidate_balances)):
        file_balance = _quantize_amount(file_balances.get(key, Decimal("0.00")))
        candidate_balance = _quantize_amount(candidate_balances.get(key, Decimal("0.00")))
        candidate_minus_file = _quantize_amount(candidate_balance - file_balance)
        if candidate_minus_file == 0:
            continue

        mismatch_count += 1
        if file_balance != 0 and candidate_balance == 0:
            missing_in_candidate_count += 1
        if file_balance == 0 and candidate_balance != 0:
            extra_in_candidate_count += 1

        differences.append(
            {
                "counterparty_name": display_names.get(key, key),
                "file_balance": file_balance,
                "candidate_balance": candidate_balance,
                "candidate_minus_file": candidate_minus_file,
            }
        )

    differences.sort(
        key=lambda item: (
            abs(item["candidate_minus_file"]),
            item["counterparty_name"],
        ),
        reverse=True,
    )
    if top >= 0:
        differences = differences[:top]

    return {
        "exact_match": mismatch_count == 0,
        "mismatch_count": mismatch_count,
        "missing_in_candidate_count": missing_in_candidate_count,
        "extra_in_candidate_count": extra_in_candidate_count,
        "top_differences": differences,
    }


def compare_receivable_current_report(
    session: Session,
    report_path: Path,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
    onec_engine=None,
    top: int = 20,
) -> dict[str, Any]:
    snapshot_date, file_balances, file_display_names = (
        load_receivable_current_balance_override_payload(
            report_path,
            counterparty_filter_mode=counterparty_filter_mode,
        )
    )
    balance_snapshot_balances, balance_display_names, balance_row_count = (
        _aggregate_balance_snapshot_by_name(session, snapshot_date=snapshot_date)
    )
    buyers_rub_balances, buyers_display_names, buyers_rub_row_count = (
        _aggregate_buyers_rub_only_by_name(session, snapshot_date=snapshot_date)
    )
    buyers_rub_resolution = _buyers_rub_only_resolution(session, snapshot_date=snapshot_date)
    onec_candidate: dict[str, Any] | None = None
    onec_display_names: dict[str, str] = {}
    if onec_engine is not None:
        onec_rows, onec_meta = fetch_current_balances_from_onec(
            onec_engine,
            snapshot_date=snapshot_date,
        )
        onec_balances, onec_display_names, onec_row_count = _aggregate_authoritative_rows_by_name(
            onec_rows
        )
        onec_total = _sum_balances(onec_balances)
        onec_candidate = {
            "meta": onec_meta,
            "raw_row_count": onec_row_count,
            "counterparty_count": len(onec_balances),
            "total_balance": onec_total,
            "candidate_on_file_names_total": _quantize_amount(
                sum(
                    (onec_balances.get(key, Decimal("0.00")) for key in file_balances),
                    Decimal("0.00"),
                )
            ),
            "candidate_minus_file_total": _quantize_amount(
                sum(
                    (
                        onec_balances.get(key, Decimal("0.00"))
                        - file_balances.get(key, Decimal("0.00"))
                    )
                    for key in file_balances
                )
            ),
            **_build_candidate_diff_summary(
                file_balances=file_balances,
                candidate_balances=onec_balances,
                display_names={
                    **file_display_names,
                    **onec_display_names,
                },
                top=top,
            ),
        }

    display_names = {
        **balance_display_names,
        **buyers_display_names,
        **file_display_names,
        **onec_display_names,
    }
    file_total = _sum_balances(file_balances)
    balance_total = _sum_balances(balance_snapshot_balances)
    buyers_rub_total = _sum_balances(buyers_rub_balances)

    result = {
        "report_path": str(report_path),
        "snapshot_date": snapshot_date,
        "counterparty_filter_mode": counterparty_filter_mode,
        "file": {
            "counterparty_count": len(file_balances),
            "total_balance": file_total,
        },
        "balance_snapshot": {
            "raw_row_count": balance_row_count,
            "counterparty_count": len(balance_snapshot_balances),
            "total_balance": balance_total,
            "candidate_minus_file_total": _quantize_amount(balance_total - file_total),
            **_build_candidate_diff_summary(
                file_balances=file_balances,
                candidate_balances=balance_snapshot_balances,
                display_names=display_names,
                top=top,
            ),
        },
        "buyers_rub_only": {
            "mode": buyers_rub_resolution["mode"],
            "base_snapshot_date": buyers_rub_resolution["base_snapshot_date"],
            "direct_snapshot_total": buyers_rub_resolution["direct_total"],
            "raw_row_count": buyers_rub_row_count,
            "counterparty_count": len(buyers_rub_balances),
            "total_balance": buyers_rub_total,
            "candidate_minus_file_total": _quantize_amount(buyers_rub_total - file_total),
            **_build_candidate_diff_summary(
                file_balances=file_balances,
                candidate_balances=buyers_rub_balances,
                display_names=display_names,
                top=top,
            ),
        },
    }
    if onec_candidate is not None:
        result["onec_canonical_candidate"] = onec_candidate
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare 1C current balances report with persisted receivable snapshots"
    )
    parser.add_argument(
        "report_path", type=Path, help="Path to 1C current balances .xlsx/.xls/.csv"
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of largest per-counterparty differences to return (default: 20)",
    )
    parser.add_argument(
        "--compare-onec-canonical",
        action="store_true",
        help="Also compare the report with the current 1C canonical SQL candidate.",
    )
    parser.add_argument(
        "--counterparty-filter-mode",
        choices=("buyers", "all"),
        default="buyers",
        help=(
            "How to parse report counterparties: buyers keeps legacy buyer-only "
            "filter, all keeps every counterparty row (default: buyers)."
        ),
    )
    args = parser.parse_args()

    if not args.report_path.exists():
        raise SystemExit(f"Файл не найден: {args.report_path}")

    settings = get_settings()
    engine = build_engine(settings.database_url)
    onec_engine = None
    if args.compare_onec_canonical:
        if not settings.onec_database_url:
            raise SystemExit("ONEC_DATABASE_URL is not configured")
        onec_engine = build_engine(
            settings.onec_database_url,
            connect_args={
                "timeout": float(settings.onec_query_timeout_seconds),
                "login_timeout": float(settings.onec_login_timeout_seconds),
            },
        )
    with Session(engine) as session:
        result = compare_receivable_current_report(
            session,
            args.report_path,
            counterparty_filter_mode=args.counterparty_filter_mode,
            onec_engine=onec_engine,
            top=args.top,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
