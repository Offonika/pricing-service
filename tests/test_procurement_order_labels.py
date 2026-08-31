from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from app.services.procurement_order_labels import build_pdf, build_preview_from_rows, build_xlsx


def _preview(size: str = "50x40") -> dict:
    return build_preview_from_rows(
        order_id=7,
        onec_number="0543",
        label_size=size,
        rows=[
            {
                "line_no": 1,
                "onec_item_code": "062852",
                "item_name": "Дисплей HUA NV 10 Pro",
                "article_1c": "062852",
                "barcode": "2900000636873",
                "quantity": 2,
            },
            {
                "line_no": 2,
                "onec_item_code": "076990",
                "item_name": "Дисплей XIA 15T",
                "article_1c": "076990",
                "barcode": "2900000778320",
                "quantity": 1,
            },
        ],
    )


def test_preview_repeats_quantity_and_adds_only_between_position_separator() -> None:
    preview = _preview()

    assert preview["ready"] is True
    assert preview["position_count"] == 2
    assert preview["product_label_count"] == 3
    assert preview["separator_count"] == 1
    assert preview["total_page_count"] == 4


def test_preview_blocks_missing_barcode_and_fractional_quantity() -> None:
    preview = build_preview_from_rows(
        order_id=1,
        onec_number="0496",
        label_size="40x30",
        rows=[{"line_no": 3, "quantity": "1.5", "barcode": ""}],
    )

    assert preview["ready"] is False
    assert any("целым числом" in blocker for blocker in preview["blockers"])
    assert any("штрихкод" in blocker for blocker in preview["blockers"])


def test_pdf_has_exact_physical_size_and_page_count() -> None:
    payload = build_pdf(_preview("50x40"))

    assert payload.startswith(b"%PDF-1.4")
    assert payload.count(b"/Type /Page ") == 4
    assert b"/MediaBox [0 0 141.7323 113.3858]" in payload


def test_xlsx_contains_one_visual_block_per_product_label() -> None:
    workbook = load_workbook(BytesIO(build_xlsx(_preview())), data_only=True)
    sheet = workbook["Этикетки"]

    order_headers = [
        cell.value
        for cell in sheet["B"]
        if isinstance(cell.value, str) and "Заказ 0543" in cell.value
    ]
    assert len(order_headers) == 3
    assert len(sheet._images) == 3
    assert sheet.row_dimensions[11].height > 200
