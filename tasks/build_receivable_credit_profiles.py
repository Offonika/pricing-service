from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date
from pathlib import Path
from typing import Sequence

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.services.counterparty_folder_recommendations import fetch_counterparty_refs_from_onec_group
from app.services.receivable_credit_profile import (
    ReceivableCreditProfile,
    build_credit_profile_summary,
    build_receivable_credit_profiles,
)
from app.services.receivable_decision_onec_metrics import (
    fetch_counterparty_payment_form_metrics_from_onec,
    fetch_counterparty_profitability_metrics_from_onec,
)
from app.services.receivable_decision_portrait import (
    DEFAULT_ONEC_FOLDER_FILTER,
    FolderFilterResult,
    load_counterparty_refs_for_folder,
)

DEFAULT_OUTPUT_DIR = Path("reports/receivables/credit_profiles")

CSV_COLUMNS = [
    "snapshot_date",
    "counterparty_code",
    "counterparty_name",
    "department_name",
    "manager_name",
    "current_balance",
    "credit_depth_days",
    "shipment_ban",
    "last_sale_at",
    "last_payment_at",
    "last_activity_at",
    "activity_reason",
    "sales_90",
    "payment_total_90",
    "payment_behavior_group",
    "payment_behavior_label",
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
    "recommended_first_payment_pct",
    "recommended_decision",
    "gross_profit_90",
    "gross_margin_pct_90",
    "profitability_pct_90",
    "source_status",
    "source_notes",
]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    engine = create_engine(database_url, pool_pre_ping=True)
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
            if not folder_filter.applied:
                raise SystemExit("Для кредитных профилей нужен фильтр папки `Покупатели`.")
            profiles = build_receivable_credit_profiles(
                session,
                snapshot_date=snapshot_date,
                counterparty_refs=folder_filter.counterparty_refs,
                active_window_days=args.active_window_days,
                limit=args.limit,
            )
            if args.with_onec_metrics and profiles:
                profile_refs = [profile.counterparty_ref for profile in profiles]
                onec_database_url = (
                    args.onec_database_url
                    or os.environ.get("ONEC_DATABASE_URL")
                    or settings.onec_database_url
                )
                if not onec_database_url:
                    raise SystemExit("ONEC_DATABASE_URL is required for --with-onec-metrics")
                onec_engine = create_engine(onec_database_url, pool_pre_ping=True)
                try:
                    profitability_by_ref = fetch_counterparty_profitability_metrics_from_onec(
                        onec_engine,
                        snapshot_date=snapshot_date,
                        counterparty_refs=profile_refs,
                    )
                    payment_form_by_ref = fetch_counterparty_payment_form_metrics_from_onec(
                        onec_engine,
                        snapshot_date=snapshot_date,
                        counterparty_refs=profile_refs,
                    )
                finally:
                    onec_engine.dispose()
                profiles = build_receivable_credit_profiles(
                    session,
                    snapshot_date=snapshot_date,
                    counterparty_refs=folder_filter.counterparty_refs,
                    active_window_days=args.active_window_days,
                    limit=args.limit,
                    profitability_by_ref=profitability_by_ref,
                    payment_form_by_ref=payment_form_by_ref,
                )
        output_dir = args.output_dir / snapshot_date.isoformat()
        json_path = output_dir / "receivable-credit-profiles.json"
        csv_path = output_dir / "receivable-credit-profiles.csv"
        payload = build_payload(
            snapshot_date=snapshot_date,
            profiles=profiles,
            folder_filter=folder_filter,
            active_window_days=args.active_window_days,
        )
        write_json(json_path, payload)
        write_csv(csv_path, profiles)
    finally:
        engine.dispose()

    result = {
        "status": "ready",
        "mode": "dry-run",
        "bitrix_writes": False,
        "snapshot_date": snapshot_date.isoformat(),
        "items": len(profiles),
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "summary": payload["summary"],
        "folder_filter": payload["folder_filter"],
        "active_window_days": args.active_window_days,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def get_latest_snapshot_date(session: Session) -> date | None:
    return session.execute(select(func.max(ReceivableBalanceSnapshot.snapshot_date))).scalar()


def build_payload(
    *,
    snapshot_date: date,
    profiles: Sequence[ReceivableCreditProfile],
    folder_filter: FolderFilterResult,
    active_window_days: int,
) -> dict[str, object]:
    return {
        "status": "ready",
        "mode": "dry-run",
        "bitrix_writes": False,
        "profile_title": "Кредитные профили покупателей",
        "snapshot_date": snapshot_date.isoformat(),
        "active_window_days": active_window_days,
        "folder_filter": folder_filter.to_dict(),
        "summary": build_credit_profile_summary(profiles),
        "items": [profile.to_dict() for profile in profiles],
    }


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
    onec_engine = create_engine(onec_database_url, pool_pre_ping=True)
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


def write_csv(path: Path, profiles: Sequence[ReceivableCreditProfile]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for profile in profiles:
            writer.writerow(_csv_row(profile))
    return path


def _csv_row(profile: ReceivableCreditProfile) -> dict[str, object]:
    return {
        "snapshot_date": profile.snapshot_date.isoformat(),
        "counterparty_code": profile.counterparty_code or "",
        "counterparty_name": profile.counterparty_name or profile.counterparty_ref,
        "department_name": profile.department_name or "",
        "manager_name": profile.manager_name or "",
        "current_balance": profile.current_balance,
        "credit_depth_days": (
            profile.credit_depth_days if profile.credit_depth_days is not None else ""
        ),
        "shipment_ban": "" if profile.shipment_ban is None else int(profile.shipment_ban),
        "last_sale_at": profile.last_sale_at.isoformat() if profile.last_sale_at else "",
        "last_payment_at": profile.last_payment_at.isoformat() if profile.last_payment_at else "",
        "last_activity_at": (
            profile.last_activity_at.isoformat() if profile.last_activity_at else ""
        ),
        "activity_reason": profile.activity_reason,
        "sales_90": profile.sales_90,
        "payment_total_90": profile.payment_total_90,
        "payment_behavior_group": profile.payment_behavior_group,
        "payment_behavior_label": profile.payment_behavior_label,
        "payment_form_primary": profile.payment_form.payment_form_primary,
        "cash_share_90": profile.payment_form.cash_share_90 or "",
        "bank_share_90": profile.payment_form.bank_share_90 or "",
        "payment_form_source_status": profile.payment_form.source_status,
        "credit_discipline_grade": profile.credit_discipline_grade,
        "credit_discipline_coefficient": profile.credit_discipline_coefficient,
        "avg_monthly_sales_90": profile.avg_monthly_sales_90,
        "recommended_credit_limit": profile.recommended_credit_limit,
        "over_limit_amount": profile.over_limit_amount,
        "recommended_first_payment_amount": profile.recommended_first_payment_amount,
        "recommended_first_payment_pct": profile.recommended_first_payment_pct,
        "recommended_decision": profile.recommended_decision,
        "gross_profit_90": profile.profitability.gross_profit_90 or "",
        "gross_margin_pct_90": profile.profitability.gross_margin_pct_90 or "",
        "profitability_pct_90": profile.profitability.profitability_pct_90 or "",
        "source_status": profile.source_status,
        "source_notes": " | ".join(profile.source_notes),
    }


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build local dry-run credit profiles for buyers from `Покупатели`."
    )
    parser.add_argument("--snapshot-date", type=_parse_date, help="Дата снимка YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Ограничить число строк для проверки")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--database-url", help="Переопределить DATABASE_URL для dry-run")
    parser.add_argument(
        "--onec-folder",
        default=DEFAULT_ONEC_FOLDER_FILTER,
        help="Фильтр по группе контрагента 1С",
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
        "--active-window-days",
        type=int,
        default=365,
        help="Окно активности покупателя: продажи/оплаты/возвраты за N дней",
    )
    parser.add_argument(
        "--with-onec-metrics",
        action="store_true",
        help="Добрать из 1С прибыльность и форму оплаты нал/безнал",
    )
    parser.add_argument(
        "--allow-missing-folder-filter",
        action="store_true",
        help="Разрешить расчет без снимка папок; по умолчанию команда останавливается",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
