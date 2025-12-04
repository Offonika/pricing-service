from datetime import date
from io import BytesIO

from openpyxl import Workbook

from app.services.importers.competitor_ftp import parse_ftp_xlsx


def _build_workbook(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_parse_valid_rows_with_amount_and_stock():
    content = _build_workbook(
        [
            ["group", "sku", "name", "price_opt", "price_roz", "link", "stock", "amount", "time"],
            ["A", "SKU1", "Name1", 10, 12, "https://x", True, 5, "2025.11.30 00:10:11"],
            ["A", "SKU2", "Name2", 9.5, 11.5, "https://y", "", "", "2025.11.30 00:10:11"],
        ]
    )
    rows, mismatch = parse_ftp_xlsx(content, file_date=date(2025, 11, 30), source="moba")
    assert mismatch is False
    assert len(rows) == 2
    assert rows[0].is_valid is True
    assert rows[0].amount == 5
    assert rows[0].stock is True
    assert rows[1].is_valid is False
    assert rows[1].error == "missing amount/stock"


def test_date_mismatch_flag():
    content = _build_workbook(
        [
            ["group", "sku", "name", "price_opt", "price_roz", "link", "stock", "time"],
            ["A", "SKU1", "Name1", 10, 12, "https://x", True, "2025.11.29 00:10:11"],
        ]
    )
    rows, mismatch = parse_ftp_xlsx(content, file_date=date(2025, 11, 30), source="moba")
    assert len(rows) == 1
    assert rows[0].is_valid is True
    assert mismatch is True

