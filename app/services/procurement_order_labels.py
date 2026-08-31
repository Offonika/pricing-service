from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Font
from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infrastructure.db import build_onec_engine_from_settings
from app.services.procurement_labels import _draw_barcode, fetch_onec_supplier_order_lines
from app.services.procurement_order_formation import get_order

LABEL_SIZES_MM = {"50x40": (50, 40), "40x30": (40, 30)}
LABEL_DPI = 300


def _positive_integer(value: Any) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Количество этикеток должно быть целым числом") from exc
    if number <= 0 or number != number.to_integral_value():
        raise ValueError("Количество этикеток должно быть положительным целым числом")
    return int(number)


def build_preview_from_rows(
    *, order_id: int, onec_number: str, label_size: str, rows: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    if label_size not in LABEL_SIZES_MM:
        raise ValueError("Поддерживаются размеры 50x40 и 40x30")
    result_rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    for raw in rows:
        line_no = int(raw.get("line_no") or 0)
        try:
            quantity = _positive_integer(raw.get("quantity"))
        except ValueError as exc:
            blockers.append(f"строка {line_no}: {exc}")
            quantity = 0
        barcode = str(raw.get("barcode") or "").strip()
        if not barcode:
            blockers.append(f"строка {line_no}: не найден штрихкод 1С")
        result_rows.append(
            {
                "line_no": line_no,
                "onec_item_code": str(raw.get("onec_item_code") or "").strip(),
                "item_name": str(raw.get("item_name") or "").strip(),
                "article_1c": str(raw.get("article_1c") or "").strip(),
                "barcode": barcode,
                "quantity": quantity,
            }
        )
    if not result_rows:
        blockers.append(f"Не найдены строки заказа поставщику {onec_number} в 1С")
    total_labels = sum(row["quantity"] for row in result_rows)
    separators = max(0, len(result_rows) - 1)
    return {
        "order_id": order_id,
        "onec_number": onec_number,
        "label_size": label_size,
        "position_count": len(result_rows),
        "product_label_count": total_labels,
        "separator_count": separators,
        "total_page_count": total_labels + separators,
        "ready": not blockers,
        "blockers": blockers,
        "rows": result_rows,
    }


def build_order_label_preview(
    db: Session, order_id: int, *, label_size: str, settings: Settings
) -> dict[str, Any]:
    order = get_order(db, order_id)
    onec_number = str(order.onec_document_number or "").strip()
    if not onec_number:
        raise ValueError("Сначала создайте черновик заказа в 1С и получите его номер")
    engine = build_onec_engine_from_settings(settings)
    rows = fetch_onec_supplier_order_lines(engine, onec_number)
    return build_preview_from_rows(
        order_id=order_id,
        onec_number=onec_number,
        label_size=label_size,
        rows=rows,
    )


def _label_image(row: dict[str, Any], label_size: str) -> Image.Image:
    width_mm, height_mm = LABEL_SIZES_MM[label_size]
    width = round(width_mm / 25.4 * LABEL_DPI)
    height = round(height_mm / 25.4 * LABEL_DPI)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    margin = max(12, round(width * 0.04))
    from app.services.procurement_labels import _font, _trim_text

    code_font = _font(max(22, round(height * 0.075)), bold=True)
    text_font = _font(max(18, round(height * 0.06)))
    barcode_font = _font(max(20, round(height * 0.065)))
    usable_width = width - margin * 2
    code_line = row["onec_item_code"]
    if row.get("article_1c"):
        code_line += f"   {row['article_1c']}"
    draw.text(
        (margin, margin),
        _trim_text(draw, code_line, code_font, usable_width),
        fill="black",
        font=code_font,
    )
    draw.text(
        (margin, margin + round(height * 0.10)),
        _trim_text(draw, row["item_name"], text_font, usable_width),
        fill="black",
        font=text_font,
    )
    barcode_top = round(height * 0.40)
    barcode_height = round(height * 0.30)
    _draw_barcode(
        draw,
        barcode=row["barcode"],
        left=margin,
        top=barcode_top,
        width=usable_width,
        height=barcode_height,
    )
    barcode_text = row["barcode"]
    text_width = draw.textlength(barcode_text, font=barcode_font)
    draw.text(
        ((width - text_width) / 2, barcode_top + barcode_height + 8),
        barcode_text,
        fill="black",
        font=barcode_font,
    )
    return image


def _pdf_bytes(
    pages: list[tuple[bytes | None, int, int]], width_pt: float, height_pt: float
) -> bytes:
    objects: list[bytes] = [b"", b""]
    page_refs: list[int] = []
    image_refs: dict[bytes, int] = {}
    for jpeg, width_px, height_px in pages:
        image_ref = None
        if jpeg is not None:
            image_ref = image_refs.get(jpeg)
            if image_ref is None:
                image_ref = len(objects) + 1
                image_refs[jpeg] = image_ref
                objects.append(
                    (
                        f"<< /Type /XObject /Subtype /Image /Width {width_px} /Height {height_px} "
                        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n"
                    ).encode()
                    + jpeg
                    + b"\nendstream"
                )
        content = (
            b""
            if image_ref is None
            else f"q {width_pt:.4f} 0 0 {height_pt:.4f} 0 0 cm /Im0 Do Q".encode()
        )
        content_ref = len(objects) + 1
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        )
        page_ref = len(objects) + 1
        resources = "<< >>" if image_ref is None else f"<< /XObject << /Im0 {image_ref} 0 R >> >>"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:.4f} {height_pt:.4f}] /Resources {resources} /Contents {content_ref} 0 R >>".encode()
        )
        page_refs.append(page_ref)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        f"<< /Type /Pages /Count {len(page_refs)} /Kids [{' '.join(f'{ref} 0 R' for ref in page_refs)}] >>".encode()
    )
    output = BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode() + payload + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return output.getvalue()


def build_pdf(preview: dict[str, Any]) -> bytes:
    if not preview["ready"]:
        raise ValueError("Нельзя сформировать этикетки: исправьте ошибки preview")
    pages: list[tuple[bytes | None, int, int]] = []
    for index, row in enumerate(preview["rows"]):
        image = _label_image(row, preview["label_size"])
        jpeg_io = BytesIO()
        image.save(jpeg_io, format="JPEG", quality=92, dpi=(LABEL_DPI, LABEL_DPI))
        jpeg = jpeg_io.getvalue()
        pages.extend([(jpeg, image.width, image.height)] * row["quantity"])
        if index + 1 < len(preview["rows"]):
            pages.append((None, image.width, image.height))
    width_mm, height_mm = LABEL_SIZES_MM[preview["label_size"]]
    return _pdf_bytes(pages, width_mm / 25.4 * 72, height_mm / 25.4 * 72)


def build_xlsx(preview: dict[str, Any]) -> bytes:
    if not preview["ready"]:
        raise ValueError("Нельзя сформировать этикетки: исправьте ошибки preview")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Этикетки"
    sheet.column_dimensions["A"].width = 5
    sheet.column_dimensions["B"].width = 62 if preview["label_size"] == "50x40" else 48
    current = 1
    for position, row in enumerate(preview["rows"]):
        barcode_image = _label_image(row, preview["label_size"])
        for copy_no in range(1, row["quantity"] + 1):
            sheet.cell(
                current, 2, f"{row['onec_item_code']}    Заказ {preview['onec_number']}"
            ).font = Font(bold=True, size=12)
            sheet.cell(current + 1, 2, row["item_name"]).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
            image_io = BytesIO()
            barcode_image.save(image_io, format="PNG", optimize=True)
            image_io.seek(0)
            image = ExcelImage(image_io)
            image.width = 360 if preview["label_size"] == "50x40" else 300
            image.height = 240 if preview["label_size"] == "50x40" else 225
            sheet.add_image(image, f"B{current + 2}")
            sheet.row_dimensions[current].height = 20
            sheet.row_dimensions[current + 1].height = 34
            sheet.row_dimensions[current + 2].height = image.height * 0.75
            sheet.cell(current + 3, 1, f"{position + 1}.{copy_no}").font = Font(
                size=8, color="808080"
            )
            current += 5
        if position + 1 < len(preview["rows"]):
            # Пустой разделитель занимает высоту полноценной этикетки, а не
            # обычной строки Excel: на рулонной печати это физический пропуск.
            sheet.row_dimensions[current].height = image.height * 0.75 + 54
            current += 5
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
