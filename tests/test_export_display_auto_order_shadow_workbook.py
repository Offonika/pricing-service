import csv
from pathlib import Path

from openpyxl import load_workbook

from tasks.export_display_auto_order_shadow_workbook import build_workbook, validate_workbook


def test_build_shadow_workbook_has_filters_summary_and_manual_columns(tmp_path: Path) -> None:
    source = tmp_path / "shadow.csv"
    rows = [
        {
            "decision_date": "2026-06-01",
            "nomenclature_code": "SKU-1",
            "name": "Товар 1",
            "status": "sale",
            "reason_code": "stock_above_min_accelerating_shortage",
            "reason": "Причина",
            "ordinary_min_stock_qty": "10",
            "free_stock_qty": "12",
            "recent_sales_qty": "4",
            "baseline_sales_qty": "1",
            "recent_rate": "0.57",
            "baseline_rate": "0.04",
            "lead_quantile": "p50",
            "projected_demand_qty": "20",
            "inventory_position_qty": "15",
            "projected_shortage_qty": "5",
            "dynamic_minmax_increment_qty": "3",
            "human_check": "Проверить",
            "production_action": "none_read_only",
        },
        {
            "decision_date": "2026-06-08",
            "nomenclature_code": "SKU-1",
            "name": "Товар 1",
            "status": "sale",
            "reason_code": "stock_above_min_accelerating_shortage",
            "reason": "Причина",
            "ordinary_min_stock_qty": "11",
            "free_stock_qty": "13",
            "recent_sales_qty": "5",
            "baseline_sales_qty": "1",
            "recent_rate": "0.71",
            "baseline_rate": "0.04",
            "lead_quantile": "p50",
            "projected_demand_qty": "22",
            "inventory_position_qty": "16",
            "projected_shortage_qty": "6",
            "dynamic_minmax_increment_qty": "4",
            "human_check": "Проверить",
            "production_action": "none_read_only",
        },
    ]
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    output = tmp_path / "review.xlsx"

    summary = build_workbook(source, output)
    validation = validate_workbook(output)

    assert summary["signal_rows"] == 2
    assert summary["unique_sku_count"] == 1
    assert summary["repeat_sku_count"] == 1
    assert validation["validated"] is True
    workbook = load_workbook(output)
    sku_sheet = workbook["SKU-сводка"]
    detail_sheet = workbook["История сигналов"]
    assert sku_sheet["A2"].value == "Не проверено"
    assert sku_sheet["I2"].value == 2
    assert sku_sheet["L2"].value == 11
    assert sku_sheet.auto_filter.ref == "A1:Z2"
    assert detail_sheet.auto_filter.ref == "A1:AC3"
    assert sku_sheet.freeze_panes == "F2"
    assert detail_sheet.freeze_panes == "G2"
