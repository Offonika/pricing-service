from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date
from pathlib import Path
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.services.counterparty_folder_recommendations import fetch_counterparty_refs_from_onec_group
from app.services.receivable_decision_onec_metrics import (
    fetch_counterparty_payment_form_metrics_from_onec,
    fetch_counterparty_profitability_metrics_from_onec,
)
from app.services.receivable_decision_portrait import (
    DEFAULT_ONEC_FOLDER_FILTER,
    FolderFilterResult,
    ReceivableDecisionPortrait,
    build_portrait_summary,
    build_receivable_decision_portraits,
    load_counterparty_refs_for_folder,
)

DEFAULT_OUTPUT_DIR = Path("reports/receivables/decision_portraits")

CSV_COLUMNS = [
    "snapshot_date",
    "counterparty_code",
    "counterparty_name",
    "department_name",
    "manager_name",
    "current_balance",
    "overdue_days",
    "sales_30",
    "sales_60",
    "sales_90",
    "trend_coefficient",
    "payment_form_primary",
    "cash_share_90",
    "bank_share_90",
    "payment_form_source_status",
    "credit_discipline_grade",
    "credit_discipline_coefficient",
    "avg_monthly_sales_90",
    "recommended_credit_limit",
    "over_limit_amount",
    "recommended_first_payment_amount",
    "revenue_90",
    "cost_of_sales_90",
    "gross_profit_90",
    "gross_margin_pct_90",
    "profitability_pct_90",
    "defect_return_amount_90",
    "payment_total_90",
    "payment_behavior_group",
    "payment_behavior_label",
    "recommended_decision",
    "recommended_first_payment_pct",
    "recommended_payment_window_days",
    "advisor_summary",
    "negotiation_goal",
    "source_status",
    "source_notes",
]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    engine = build_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            snapshot_date = args.snapshot_date or get_latest_snapshot_date(session)
            if snapshot_date is None:
                raise SystemExit("Нет снимков receivable_balance_snapshot для расчета")
            folder_filter = resolve_folder_filter(
                session,
                snapshot_date=snapshot_date,
                folder_name=args.onec_folder,
                source=args.folder_filter_source,
                onec_database_url=args.onec_database_url
                or os.environ.get("ONEC_DATABASE_URL")
                or settings.onec_database_url,
            )
            if (
                folder_filter.status == "missing_folder_snapshot"
                and not args.allow_missing_folder_filter
            ):
                raise SystemExit(
                    "Нет локального снимка папок counterparty_folder_snapshot: "
                    "нельзя безопасно ограничить расчет папкой "
                    f"`{args.onec_folder}`."
                )
            counterparty_refs = _effective_counterparty_refs(
                explicit_refs=args.counterparty_ref,
                folder_filter=folder_filter,
            )
            if counterparty_refs is not None and not counterparty_refs:
                portraits = []
            else:
                portraits = build_receivable_decision_portraits(
                    session,
                    snapshot_date=snapshot_date,
                    limit=args.limit,
                    counterparty_refs=counterparty_refs or (),
                )
                if args.with_onec_profitability and portraits:
                    portrait_refs = [portrait.counterparty_ref for portrait in portraits]
                    onec_database_url = (
                        args.onec_database_url
                        or os.environ.get("ONEC_DATABASE_URL")
                        or settings.onec_database_url
                    )
                    if not onec_database_url:
                        raise SystemExit(
                            "ONEC_DATABASE_URL is required for --with-onec-profitability"
                        )
                    onec_engine = build_engine(onec_database_url, pool_pre_ping=True)
                    try:
                        profitability_by_ref = fetch_counterparty_profitability_metrics_from_onec(
                            onec_engine,
                            snapshot_date=snapshot_date,
                            counterparty_refs=portrait_refs,
                        )
                        payment_form_by_ref = fetch_counterparty_payment_form_metrics_from_onec(
                            onec_engine,
                            snapshot_date=snapshot_date,
                            counterparty_refs=portrait_refs,
                        )
                    finally:
                        onec_engine.dispose()
                    portraits = build_receivable_decision_portraits(
                        session,
                        snapshot_date=snapshot_date,
                        counterparty_refs=portrait_refs,
                        profitability_by_ref=profitability_by_ref,
                        payment_form_by_ref=payment_form_by_ref,
                    )
        output_dir = args.output_dir / snapshot_date.isoformat()
        json_path = output_dir / "receivable-decision-portraits.json"
        csv_path = output_dir / "receivable-decision-portraits.csv"
        payload = build_payload(
            snapshot_date=snapshot_date,
            portraits=portraits,
            folder_filter=folder_filter,
        )
        write_json(json_path, payload)
        write_csv(csv_path, portraits)
    finally:
        engine.dispose()

    result = {
        "status": "ready",
        "mode": "dry-run",
        "bitrix_writes": False,
        "snapshot_date": snapshot_date.isoformat(),
        "items": len(portraits),
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "summary": payload["summary"],
        "folder_filter": payload["folder_filter"],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def get_latest_snapshot_date(session: Session) -> date | None:
    return session.execute(select(func.max(ReceivableBalanceSnapshot.snapshot_date))).scalar()


def build_payload(
    *,
    snapshot_date: date,
    portraits: Sequence[ReceivableDecisionPortrait],
    folder_filter: FolderFilterResult | None = None,
) -> dict[str, object]:
    return {
        "status": "ready",
        "mode": "dry-run",
        "bitrix_writes": False,
        "process_title": "Дебиторка Решение",
        "snapshot_date": snapshot_date.isoformat(),
        "folder_filter": (
            folder_filter.to_dict()
            if folder_filter
            else {
                "folder_name": None,
                "snapshot_date": None,
                "status": "not_requested",
                "source": "none",
                "counterparty_ref_count": 0,
                "counterparty_refs_sample": [],
            }
        ),
        "summary": build_portrait_summary(portraits),
        "items": [portrait.to_dict() for portrait in portraits],
    }


def _effective_counterparty_refs(
    *,
    explicit_refs: Sequence[str],
    folder_filter: FolderFilterResult,
) -> list[str] | None:
    ref_filter: set[str] | None = None
    if folder_filter.applied:
        ref_filter = set(folder_filter.counterparty_refs)
    explicit_ref_set = {value for value in explicit_refs if value}
    if explicit_ref_set:
        ref_filter = explicit_ref_set if ref_filter is None else ref_filter & explicit_ref_set
    if ref_filter is None:
        return None
    return sorted(ref_filter)


def resolve_folder_filter(
    session: Session,
    *,
    snapshot_date: date,
    folder_name: str | None,
    source: str,
    onec_database_url: str | None,
) -> FolderFilterResult:
    if source == "snapshot":
        return load_counterparty_refs_for_folder(
            session,
            snapshot_date=snapshot_date,
            folder_name=folder_name,
        )
    if not folder_name:
        return FolderFilterResult(
            folder_name=None,
            snapshot_date=None,
            status="not_requested",
            source="none",
            counterparty_refs=(),
        )
    if not onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is required for --folder-filter-source onec")
    onec_engine = build_engine(onec_database_url, pool_pre_ping=True)
    try:
        refs = fetch_counterparty_refs_from_onec_group(
            onec_engine,
            group_name=folder_name,
        )
    finally:
        onec_engine.dispose()
    return FolderFilterResult(
        folder_name=folder_name,
        snapshot_date=snapshot_date,
        status="ready",
        source="onec_group_subtree",
        counterparty_refs=tuple(sorted(refs)),
    )


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_csv(path: Path, portraits: Sequence[ReceivableDecisionPortrait]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for portrait in portraits:
            writer.writerow(_csv_row(portrait))
    return path


def _csv_row(portrait: ReceivableDecisionPortrait) -> dict[str, object]:
    return {
        "snapshot_date": portrait.snapshot_date.isoformat(),
        "counterparty_code": portrait.counterparty_code or "",
        "counterparty_name": portrait.counterparty_name or portrait.counterparty_ref,
        "department_name": portrait.department_name or "",
        "manager_name": portrait.manager_name or "",
        "current_balance": portrait.current_balance,
        "overdue_days": portrait.overdue_days if portrait.overdue_days is not None else "",
        "sales_30": portrait.sales.sales_30,
        "sales_60": portrait.sales.sales_60,
        "sales_90": portrait.sales.sales_90,
        "trend_coefficient": portrait.sales.trend_coefficient,
        "payment_form_primary": portrait.payment_form.payment_form_primary,
        "cash_share_90": portrait.payment_form.cash_share_90 or "",
        "bank_share_90": portrait.payment_form.bank_share_90 or "",
        "payment_form_source_status": portrait.payment_form.source_status,
        "credit_discipline_grade": portrait.credit_policy.credit_discipline_grade,
        "credit_discipline_coefficient": portrait.credit_policy.credit_discipline_coefficient,
        "avg_monthly_sales_90": portrait.credit_policy.avg_monthly_sales_90,
        "recommended_credit_limit": portrait.credit_policy.recommended_credit_limit,
        "over_limit_amount": portrait.credit_policy.over_limit_amount,
        "recommended_first_payment_amount": (
            portrait.credit_policy.recommended_first_payment_amount
        ),
        "revenue_90": portrait.profitability.revenue_90 or "",
        "cost_of_sales_90": portrait.profitability.cost_of_sales_90 or "",
        "gross_profit_90": portrait.profitability.gross_profit_90 or "",
        "gross_margin_pct_90": portrait.profitability.gross_margin_pct_90 or "",
        "profitability_pct_90": portrait.profitability.profitability_pct_90 or "",
        "defect_return_amount_90": portrait.profitability.defect_return_amount_90 or "",
        "payment_total_90": portrait.payments.payment_total_90,
        "payment_behavior_group": portrait.payment_behavior_group,
        "payment_behavior_label": portrait.payment_behavior_label,
        "recommended_decision": portrait.advisor.recommended_decision_label,
        "recommended_first_payment_pct": portrait.advisor.recommended_first_payment_pct,
        "recommended_payment_window_days": portrait.advisor.recommended_payment_window_days,
        "advisor_summary": portrait.advisor.advisor_summary,
        "negotiation_goal": portrait.advisor.negotiation_goal,
        "source_status": portrait.source_status,
        "source_notes": " | ".join(portrait.source_notes),
    }


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build local dry-run portraits for `Дебиторка Решение`."
    )
    parser.add_argument("--snapshot-date", type=_parse_date, help="Дата снимка YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Ограничить число строк для проверки")
    parser.add_argument(
        "--counterparty-ref",
        action="append",
        default=[],
        help="Ограничить расчет одним или несколькими ref контрагента 1С",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--database-url", help="Переопределить DATABASE_URL для dry-run")
    parser.add_argument(
        "--onec-folder",
        default=DEFAULT_ONEC_FOLDER_FILTER,
        help="Фильтр по текущей папке контрагента 1С; пустая строка отключает фильтр",
    )
    parser.add_argument(
        "--folder-filter-source",
        choices=("snapshot", "onec"),
        default="snapshot",
        help="Источник refs для фильтра папки: локальный снимок или subtree группы 1С",
    )
    parser.add_argument(
        "--onec-database-url",
        help="Переопределить ONEC_DATABASE_URL для 1С read-only расчетов",
    )
    parser.add_argument(
        "--with-onec-profitability",
        action="store_true",
        help="Добрать из 1С выручку, себестоимость, прибыль и возвраты по браку",
    )
    parser.add_argument(
        "--allow-missing-folder-filter",
        action="store_true",
        help="Разрешить расчет без снимка папок; по умолчанию команда останавливается",
    )
    parser.add_argument("--json", action="store_true", help="Печатать краткий JSON в одну строку")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
