"""Ретропроверка (backtest) формулы автозаказа дисплеев.

Решение владельца 2026-08-09: одноразовый «показ топ-20 строк человеку»
заменяется автоматическим экзаменом формулы. Расчёт запускается на данных
прошлой даты (`--as-of-past`), его предсказание скорости сравнивается с
фактическими продажами и наличием за последующий горизонт. Итог — метрики
«лишний заказ» и «упущенные продажи» по каждой карточке и по каталогу.

Read-only: пишет только CSV/JSON отчёта, не трогает БД, Bitrix и 1С.

Честные границы v1 (см. docs/specs/assortment-status-legacy-rule-inventory.md):

- остатки и статусы в прошлый расчёт утекают из настоящего, поэтому экзаменуется
  предсказание СПРОСА (скорость), а не количество заказа целиком;
- витрина дней наличия начинается с ``2026-01-28``: если карточка была на полке
  всю наблюдаемую историю, поправка наличия на прошлой дате перегрета обрезанным
  окном — для таких карточек предсказанием считается календарная скорость
  (см. ``observable_cap_days``).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine, session_scope
from tasks.build_display_auto_order_dry_run import (
    MIN_RELIABLE_AVAILABILITY_DAYS,
    fetch_days_in_sale_totals,
    fetch_sales_totals,
    load_warehouse_policy,
)

DEFAULT_HORIZON_DAYS = 60
# Порог значимости расхождения: меньше этого числа штук за горизонт не считаем
# ни перезаказом, ни недозаказом - шум малых чисел.
MIN_ABS_GAP_QTY = Decimal("5")
OVER_FORECAST_RATIO = Decimal("2")
UNDER_FORECAST_RATIO = Decimal("0.5")
# Упущенные продажи считаем только там, где дефицит реально был заметным.
LOST_SALES_MIN_OUT_DAYS = 14
LOST_SALES_MIN_ACTUAL_QTY = Decimal("3")

FIELDNAMES = [
    "nomenclature_code",
    "name",
    "status_label",
    "speed_tier",
    "dry_run_decision",
    "predicted_rate",
    "predicted_rate_source",
    "predicted_qty_horizon",
    "actual_sales_qty",
    "actual_available_days",
    "actual_out_of_stock_days",
    "actual_rate_soft",
    "actual_qty_soft",
    "verdict",
    "gap_qty",
    "over_order_rub",
    "lost_sales_qty",
    "lost_sales_rub",
    "latest_purchase_price",
]


@dataclass
class BacktestSummary:
    horizon_days: int
    as_of_past: str
    cards_total: int = 0
    cards_scored: int = 0
    verdict_ok: int = 0
    verdict_over: int = 0
    verdict_under: int = 0
    verdict_no_signal: int = 0
    predicted_qty_total: Decimal = Decimal("0")
    actual_qty_soft_total: Decimal = Decimal("0")
    over_order_qty_total: Decimal = Decimal("0")
    over_order_rub_total: Decimal = Decimal("0")
    lost_sales_qty_total: Decimal = Decimal("0")
    lost_sales_rub_total: Decimal = Decimal("0")
    cards_capped_by_coverage: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in self.__dict__.items()
        }


def _dec(value: Any) -> Decimal:
    raw = str(value or "").strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except ArithmeticError:
        return Decimal("0")


def _soft_rate(qty: Decimal, horizon_days: int, available_days: Decimal | None) -> Decimal:
    """Та же мягкая формула, что в расчёте: дни без товара добираются по
    календарной средней; при короткой базе наличия (< MIN_RELIABLE_
    AVAILABILITY_DAYS) поправка не применяется."""
    window = Decimal(str(horizon_days))
    if window <= 0:
        return Decimal("0")
    base = qty / window
    if available_days is None or available_days <= 0:
        return base
    if available_days < MIN_RELIABLE_AVAILABILITY_DAYS:
        return base
    days_without = max(Decimal("0"), window - min(available_days, window))
    return (qty + days_without * base) / window


def build_backtest_rows(
    predicted_rows: list[dict[str, Any]],
    actual_sales: dict[str, dict[str, Any]],
    actual_days: dict[str, dict[int, Decimal]],
    *,
    horizon_days: int,
    as_of_past: date,
    observable_cap_days: int,
) -> tuple[list[dict[str, str]], BacktestSummary]:
    summary = BacktestSummary(horizon_days=horizon_days, as_of_past=as_of_past.isoformat())
    horizon = Decimal(str(horizon_days))
    out: list[dict[str, str]] = []
    for row in predicted_rows:
        code = str(row.get("nomenclature_code") or "").strip()
        if not code:
            continue
        summary.cards_total += 1
        predicted_soft = _dec(row.get("avg_daily_sales_qty"))
        predicted_base = _dec(row.get("base_avg_daily_sales_qty"))
        days_long_raw = str(row.get("days_in_sale_long") or "").strip()
        rate_source = "soft"
        predicted_rate = predicted_soft
        if days_long_raw and observable_cap_days > 0:
            observed = _dec(days_long_raw)
            if observed >= Decimal("0.95") * Decimal(str(observable_cap_days)):
                # Полка занята всю наблюдаемую историю витрины: поправка на
                # прошлой дате перегрета обрезанным окном, честнее календарь.
                predicted_rate = predicted_base
                rate_source = "base_coverage_cap"
                summary.cards_capped_by_coverage += 1
        predicted_qty = predicted_rate * horizon

        actual_qty = _dec((actual_sales.get(code) or {}).get("sales_qty_window"))
        available = (actual_days.get(code) or {}).get(horizon_days)
        available_days = available if available is not None else None
        out_days = (
            max(Decimal("0"), horizon - min(available_days, horizon))
            if available_days is not None
            else Decimal("0")
        )
        actual_rate_soft = _soft_rate(actual_qty, horizon_days, available_days)
        actual_qty_soft = actual_rate_soft * horizon

        gap = predicted_qty - actual_qty_soft
        verdict = "ok"
        over_rub = Decimal("0")
        price = _dec(row.get("latest_purchase_price"))
        if predicted_qty == 0 and actual_qty == 0:
            verdict = "no_signal"
            summary.verdict_no_signal += 1
        elif gap >= MIN_ABS_GAP_QTY and (
            actual_qty_soft == 0 or predicted_qty >= actual_qty_soft * OVER_FORECAST_RATIO
        ):
            verdict = "over_forecast"
            summary.verdict_over += 1
            summary.over_order_qty_total += gap
            over_rub = gap * price
            summary.over_order_rub_total += over_rub
        elif -gap >= MIN_ABS_GAP_QTY and (
            predicted_qty == 0 or predicted_qty <= actual_qty_soft * UNDER_FORECAST_RATIO
        ):
            verdict = "under_forecast"
            summary.verdict_under += 1
        else:
            summary.verdict_ok += 1
        if verdict != "no_signal":
            summary.cards_scored += 1
            summary.predicted_qty_total += predicted_qty
            summary.actual_qty_soft_total += actual_qty_soft

        lost_qty = Decimal("0")
        lost_rub = Decimal("0")
        decision = str(row.get("dry_run_decision") or "").strip()
        if (
            decision in ("do_not_order", "manual_review")
            and available_days is not None
            and available_days > 0
            and out_days >= Decimal(str(LOST_SALES_MIN_OUT_DAYS))
            and actual_qty >= LOST_SALES_MIN_ACTUAL_QTY
        ):
            lost_qty = (actual_qty / available_days) * out_days
            lost_rub = lost_qty * price
            summary.lost_sales_qty_total += lost_qty
            summary.lost_sales_rub_total += lost_rub

        out.append(
            {
                "nomenclature_code": code,
                "name": str(row.get("name") or ""),
                "status_label": str(row.get("status_label") or ""),
                "speed_tier": str(row.get("speed_tier") or ""),
                "dry_run_decision": decision,
                "predicted_rate": f"{predicted_rate:.4f}",
                "predicted_rate_source": rate_source,
                "predicted_qty_horizon": f"{predicted_qty:.1f}",
                "actual_sales_qty": f"{actual_qty:.0f}",
                "actual_available_days": (
                    f"{available_days:.1f}" if available_days is not None else ""
                ),
                "actual_out_of_stock_days": f"{out_days:.1f}",
                "actual_rate_soft": f"{actual_rate_soft:.4f}",
                "actual_qty_soft": f"{actual_qty_soft:.1f}",
                "verdict": verdict,
                "gap_qty": f"{gap:.1f}",
                "over_order_rub": f"{over_rub:.0f}",
                "lost_sales_qty": f"{lost_qty:.1f}",
                "lost_sales_rub": f"{lost_rub:.0f}",
                "latest_purchase_price": f"{price:.2f}" if price else "",
            }
        )
    out.sort(key=lambda r: -abs(Decimal(r["gap_qty"])))
    return out, summary


def _fetch_coverage_start(session: Any) -> date | None:
    return session.execute(
        text("SELECT MIN(available_from) FROM onec_stock_availability_interval")
    ).scalar()


def _run_past_dry_run(as_of_past: date, policy_json: Path, output_csv: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "tasks.build_display_auto_order_dry_run",
        "--as-of",
        as_of_past.isoformat(),
        "--auto-order-policy-json",
        str(policy_json),
        "--output-csv",
        str(output_csv),
        "--output-json",
        str(output_csv.with_suffix(".json")),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest display auto-order demand forecast.")
    parser.add_argument("--as-of-past", type=date.fromisoformat, default=None)
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    parser.add_argument(
        "--dry-run-csv",
        type=Path,
        default=None,
        help="Готовый CSV прошлого расчёта; если не задан, расчёт запускается сам.",
    )
    parser.add_argument(
        "--auto-order-policy-json",
        type=Path,
        default=Path("config/assortment/display-auto-order-policy.json"),
    )
    parser.add_argument(
        "--warehouse-policy-json",
        type=Path,
        default=Path("config/assortment/display-warehouse-policy.json"),
    )
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    today = date.today()
    horizon = int(args.horizon_days)
    if horizon <= 0:
        raise SystemExit("horizon-days must be positive")
    as_of_past = args.as_of_past or (today - timedelta(days=horizon))
    if as_of_past + timedelta(days=horizon) > today:
        raise SystemExit(
            f"as-of-past {as_of_past} + horizon {horizon} выходит в будущее: "
            "фактов для сравнения ещё нет"
        )

    dry_run_csv = args.dry_run_csv
    if dry_run_csv is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="auto-order-backtest-"))
        dry_run_csv = tmp_dir / f"dry-run-{as_of_past.isoformat()}.csv"
        _run_past_dry_run(as_of_past, args.auto_order_policy_json, dry_run_csv)

    with open(dry_run_csv, encoding="utf-8-sig") as handle:
        predicted_rows = list(csv.DictReader(handle))
    codes = [str(r.get("nomenclature_code") or "").strip() for r in predicted_rows]
    codes = [c for c in codes if c]

    settings = get_settings()
    policy = load_warehouse_policy(args.warehouse_policy_json)
    future_from = as_of_past + timedelta(days=1)
    future_to = as_of_past + timedelta(days=horizon)

    onec_url = os.environ.get("ONEC_DATABASE_URL", "") or settings.onec_database_url or ""
    onec_engine = build_onec_engine(
        onec_url,
        query_timeout_seconds=settings.onec_query_timeout_seconds,
        login_timeout_seconds=settings.onec_login_timeout_seconds,
    )
    try:
        actual_sales = fetch_sales_totals(
            onec_engine,
            codes=codes,
            sellable_codes=policy.sellable_codes,
            date_from=future_from,
            date_to=future_to + timedelta(days=1),
        )
    finally:
        onec_engine.dispose()

    app_url = os.environ.get("DATABASE_URL") or None
    with session_scope(read_only=True, database_url=app_url) as session:
        actual_days = fetch_days_in_sale_totals(
            session.get_bind(),
            codes=codes,
            physical_sales_point_codes=policy.sellable_codes,
            date_to=future_to,
            windows_days=(horizon,),
        )
        coverage_start = _fetch_coverage_start(session)

    window_days = 180
    window_start = as_of_past - timedelta(days=window_days - 1)
    if coverage_start is None:
        observable_cap = 0
    else:
        observable_cap = max(0, (as_of_past - max(window_start, coverage_start)).days + 1)
        if observable_cap >= window_days:
            observable_cap = 0  # витрина покрывает всё окно, колпак не нужен

    rows, summary = build_backtest_rows(
        predicted_rows,
        actual_sales,
        actual_days,
        horizon_days=horizon,
        as_of_past=as_of_past,
        observable_cap_days=observable_cap,
    )

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_csv, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    payload = summary.as_dict()
    payload["dry_run_csv"] = str(dry_run_csv)
    payload["observable_cap_days"] = observable_cap
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
