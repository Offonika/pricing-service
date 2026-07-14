from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.services.importers.onec_mutual_settlements import (
    CurrentBalanceCounterpartyFilterMode,
    OneCMutualSettlementCurrentBalanceRow,
    _load_rows,
    _parse_report_end_date,
    load_onec_mutual_settlements_current_balances_file,
)


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _sum_amounts(rows: list[OneCMutualSettlementCurrentBalanceRow]) -> Decimal:
    return _quantize_amount(sum((row.current_balance_rub for row in rows), Decimal("0.00")))


def _export_current_balances_csv(
    rows: list[OneCMutualSettlementCurrentBalanceRow],
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["snapshot_date", "counterparty_name", "current_balance_rub", "source_row"])
        for row in rows:
            writer.writerow(
                [
                    row.snapshot_date.isoformat(),
                    row.counterparty_name,
                    f"{row.current_balance_rub:f}",
                    row.source_row,
                ]
            )
    return output_path


def _xlsx_diagnostics(report_path: Path) -> dict[str, Any]:
    if report_path.suffix.lower() != ".xlsx":
        return {"available": False, "reason": "Подробная структура доступна только для .xlsx"}

    sheet_rows = _load_rows(report_path.read_bytes())
    header_rows = [
        {"row": row.row_number, "label": row.label} for row in sheet_rows[:15] if row.label
    ]
    header_text = " ".join(row.label for row in sheet_rows[:20] if row.label)
    looks_like_current_counterparty_report = (
        all(
            token in header_text
            for token in (
                "Группировки строк",
                "Организация",
                "Контрагент",
            )
        )
        and "Показатели: Сумма (руб)" in header_text
    )
    warnings = []
    if not looks_like_current_counterparty_report:
        warnings.append(
            "Файл не похож на текущую сводную ведомость с группировками по "
            "организации и контрагенту. "
            "Суммы current_balances могут быть непригодны для контроля дебиторки."
        )
    outline_levels = Counter(row.outline_level for row in sheet_rows)
    org_rows = [
        {
            "row": row.row_number,
            "label": row.label or "(пустая организация)",
            "current_balance_rub": _quantize_amount(row.current_balance_rub),
        }
        for row in sheet_rows
        if row.outline_level == 0 and row.current_balance_rub is not None
    ]

    return {
        "available": True,
        "report_end_date": _parse_report_end_date(sheet_rows),
        "looks_like_current_counterparty_report": looks_like_current_counterparty_report,
        "warnings": warnings,
        "header_rows": header_rows,
        "outline_levels": dict(sorted(outline_levels.items())),
        "org_or_total_rows": org_rows,
    }


def inspect_onec_mutual_settlements_report(
    report_path: Path,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "all",
    export_csv_path: Path | None = None,
    top: int = 20,
) -> dict[str, Any]:
    rows = load_onec_mutual_settlements_current_balances_file(
        report_path,
        counterparty_filter_mode=counterparty_filter_mode,
    )
    snapshot_dates = {row.snapshot_date for row in rows}
    if not snapshot_dates:
        raise ValueError(f"В файле нет строк текущих остатков: {report_path}")
    if len(snapshot_dates) != 1:
        raise ValueError(f"В файле несколько дат snapshot: {sorted(snapshot_dates)}")

    positive_rows = [row for row in rows if row.current_balance_rub > 0]
    negative_rows = [row for row in rows if row.current_balance_rub < 0]
    zero_rows = [row for row in rows if row.current_balance_rub == 0]
    top_abs = sorted(
        rows,
        key=lambda row: (abs(row.current_balance_rub), row.counterparty_name),
        reverse=True,
    )
    if top >= 0:
        top_abs = top_abs[:top]

    exported_csv = None
    if export_csv_path is not None:
        exported_csv = str(_export_current_balances_csv(rows, export_csv_path))

    return {
        "report_path": str(report_path),
        "counterparty_filter_mode": counterparty_filter_mode,
        "snapshot_date": next(iter(snapshot_dates)),
        "current_balances": {
            "counterparty_count": len(rows),
            "total_balance": _sum_amounts(rows),
            "positive_total": _sum_amounts(positive_rows),
            "negative_total": _sum_amounts(negative_rows),
            "zero_count": len(zero_rows),
        },
        "top_abs_balances": [
            {
                "counterparty_name": row.counterparty_name,
                "current_balance_rub": _quantize_amount(row.current_balance_rub),
                "source_row": row.source_row,
            }
            for row in top_abs
        ],
        "xlsx_diagnostics": _xlsx_diagnostics(report_path),
        "exported_csv": exported_csv,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect full 1C mutual settlements report without DB/1C queries"
    )
    parser.add_argument("report_path", type=Path, help="Path to 1C report .xlsx/.xls/.csv")
    parser.add_argument(
        "--counterparty-filter-mode",
        choices=("buyers", "all"),
        default="all",
        help="all keeps every counterparty row; buyers keeps legacy buyer-only parser.",
    )
    parser.add_argument(
        "--export-csv",
        type=Path,
        help="Optional path for normalized current balances CSV.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of largest absolute counterparty balances to show (default: 20).",
    )
    args = parser.parse_args()

    if not args.report_path.exists():
        raise SystemExit(f"Файл не найден: {args.report_path}")

    result = inspect_onec_mutual_settlements_report(
        args.report_path,
        counterparty_filter_mode=args.counterparty_filter_mode,
        export_csv_path=args.export_csv,
        top=args.top,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
