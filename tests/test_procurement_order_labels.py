from __future__ import annotations

from datetime import date
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook
from PIL import ImageChops

import app.services.procurement_order_labels as label_service
from app.core.config import Settings
from app.services.procurement_order_labels import (
    LabelSourceChangedError,
    _label_image,
    _order_marker,
    build_export_archive,
    build_order_label_preview,
    build_pdf,
    build_preview_from_rows,
    build_xlsx,
    ensure_label_source_checksum,
    link_order_label_source,
    split_preview_for_export,
)


def _preview(size: str = "50x40") -> dict:
    return build_preview_from_rows(
        order_id=7,
        onec_number="РБГУ0000543",
        onec_date=date(2026, 8, 3),
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
        onec_date=date(2026, 8, 3),
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


def test_label_contains_order_date_and_last_four_number_digits() -> None:
    preview = _preview()
    marker = _order_marker(preview)
    row = preview["rows"][0]
    with_marker = _label_image(row, preview["label_size"], marker)
    without_marker = _label_image(row, preview["label_size"], "")

    assert marker == "03.08.26/0543"
    assert ImageChops.difference(with_marker, without_marker).getbbox() is not None


@pytest.mark.parametrize(
    ("size", "expected_aspect_ratio"),
    (("50x40", 1.25), ("40x30", 4 / 3)),
)
def test_xlsx_contains_one_visual_block_per_product_label(
    size: str, expected_aspect_ratio: float
) -> None:
    workbook = load_workbook(BytesIO(build_xlsx(_preview(size))), data_only=True)
    sheet = workbook["Этикетки"]

    order_headers = [
        cell.value
        for cell in sheet["B"]
        if isinstance(cell.value, str) and "Заказ РБГУ0000543" in cell.value
    ]
    assert len(order_headers) == 3
    assert len(sheet._images) == 3
    anchor = sheet._images[0].anchor
    assert anchor.ext.cx / anchor.ext.cy == pytest.approx(expected_aspect_ratio)
    assert sheet.row_dimensions[11].height > 200


def test_preview_splits_more_than_configured_page_limit() -> None:
    preview = build_preview_from_rows(
        order_id=1,
        onec_number="РБГУ0000543",
        onec_date=date(2026, 8, 3),
        label_size="50x40",
        rows=[
            {
                "line_no": 1,
                "onec_item_code": "062852",
                "item_name": "Дисплей",
                "article_1c": "062852",
                "barcode": "2900000636873",
                "quantity": 1001,
            }
        ],
        max_page_count=1000,
    )

    assert preview["ready"] is True
    assert preview["total_page_count"] == 1001
    assert preview["export_file_count"] == 2
    assert preview["blockers"] == []

    chunks = split_preview_for_export(preview)

    assert [chunk["total_page_count"] for chunk in chunks] == [1000, 1]
    assert sum(chunk["product_label_count"] for chunk in chunks) == 1001


def test_split_uses_separators_only_between_positions_inside_each_file() -> None:
    preview = build_preview_from_rows(
        order_id=1,
        onec_number="РБГУ0000590",
        onec_date=date(2026, 8, 31),
        label_size="50x40",
        rows=[
            {"line_no": 1, "barcode": "1", "quantity": 1},
            {"line_no": 2, "barcode": "2", "quantity": 1},
            {"line_no": 3, "barcode": "3", "quantity": 1},
        ],
        max_page_count=3,
    )

    chunks = split_preview_for_export(preview)

    assert [chunk["total_page_count"] for chunk in chunks] == [3, 1]
    assert sum(chunk["product_label_count"] for chunk in chunks) == 3
    assert sum(chunk["separator_count"] for chunk in chunks) == 1


def test_known_large_order_shape_produces_seven_files() -> None:
    rows = [
        {
            "line_no": line_no,
            "onec_item_code": str(line_no),
            "item_name": f"Товар {line_no}",
            "barcode": str(line_no),
            "quantity": 5517 if line_no == 1 else 1,
        }
        for line_no in range(1, 250)
    ]
    preview = build_preview_from_rows(
        order_id=94,
        onec_number="РБГУ0000590",
        onec_date=date(2026, 8, 31),
        label_size="50x40",
        rows=rows,
        max_page_count=1000,
    )

    chunks = split_preview_for_export(preview)

    assert preview["position_count"] == 249
    assert preview["product_label_count"] == 5765
    assert preview["total_page_count"] == 6013
    assert preview["export_file_count"] == 7
    assert len(chunks) == 7
    assert all(chunk["total_page_count"] <= 1000 for chunk in chunks)


@pytest.mark.parametrize(
    ("format_", "signature"),
    (("pdf", b"%PDF-1.4"), ("xlsx", b"PK")),
)
def test_multi_file_export_is_a_named_zip_archive(format_: str, signature: bytes) -> None:
    preview = build_preview_from_rows(
        order_id=1,
        onec_number="РБГУ0000590",
        onec_date=date(2026, 8, 31),
        label_size="40x30",
        rows=[
            {
                "line_no": 1,
                "onec_item_code": "0001",
                "item_name": "Товар",
                "article_1c": "A-1",
                "barcode": "460000000001",
                "quantity": 4,
            }
        ],
        max_page_count=3,
    )

    with ZipFile(BytesIO(build_export_archive(preview, format_))) as archive:
        assert archive.namelist() == [
            f"supplier-order-РБГУ0000590-labels-40x30-part-01-of-02.{format_}",
            f"supplier-order-РБГУ0000590-labels-40x30-part-02-of-02.{format_}",
        ]
        assert all(archive.read(name).startswith(signature) for name in archive.namelist())


def test_download_checksum_detects_changed_onec_rows() -> None:
    preview = _preview()
    changed = _preview()
    changed["rows"][0]["quantity"] = 3
    changed["source_checksum"] = label_service._preview_checksum(changed)

    with pytest.raises(LabelSourceChangedError, match="изменился"):
        ensure_label_source_checksum(changed, preview["source_checksum"])


def test_preview_checksum_uses_current_onec_date_instead_of_stored_link_date() -> None:
    row = {
        "order_number": "РБГУ0000543",
        "order_date": date(2026, 8, 20),
        "line_no": 1,
        "onec_item_code": "0001",
        "item_name": "Товар",
        "article_1c": "A-1",
        "barcode": "460000000001",
        "quantity": 1,
    }
    preview = build_preview_from_rows(
        order_id=14,
        onec_number="РБГУ0000543",
        onec_date=date(2026, 8, 19),
        label_size="50x40",
        rows=[row],
    )

    assert preview["onec_date"] == date(2026, 8, 20)

    changed = build_preview_from_rows(
        order_id=14,
        onec_number="РБГУ0000543",
        onec_date=date(2026, 8, 19),
        label_size="50x40",
        rows=[{**row, "order_date": date(2026, 8, 21)}],
    )

    with pytest.raises(LabelSourceChangedError, match="изменился"):
        ensure_label_source_checksum(changed, preview["source_checksum"])


def test_full_preview_path_uses_keyword_only_engine_and_disposes_it(monkeypatch) -> None:
    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()

    def engine_factory(*, poolclass=None):
        assert poolclass is None
        return engine

    monkeypatch.setattr(label_service, "build_onec_engine_from_settings", engine_factory)
    monkeypatch.setattr(
        label_service,
        "fetch_onec_supplier_order_lines",
        lambda actual_engine, number: [
            {
                "order_number": number,
                "order_date": date(2026, 8, 3),
                "line_no": 1,
                "onec_item_code": "062852",
                "item_name": "Дисплей",
                "article_1c": "062852",
                "barcode": "2900000636873",
                "quantity": 1,
            }
        ],
    )
    monkeypatch.setattr(
        label_service,
        "get_order",
        lambda _db, _order_id: SimpleNamespace(
            onec_document_number="РБГУ0000543",
            onec_document_date=date(2026, 8, 3),
            label_onec_document_number=None,
            label_onec_document_date=None,
            label_source_linked_at=None,
        ),
    )

    preview = build_order_label_preview(object(), 7, label_size="50x40", settings=Settings())

    assert preview["ready"] is True
    assert engine.disposed is True


def test_manual_label_source_link_replaces_previous_source_with_canonical_readback(
    monkeypatch,
) -> None:
    order = SimpleNamespace(
        id=7,
        onec_document_number=None,
        onec_document_date=None,
        label_onec_document_number="РБГУ0000496",
        label_onec_document_date=date(2026, 8, 1),
        label_source_linked_at=None,
    )

    class FakeDb:
        flushed = False

        def flush(self) -> None:
            self.flushed = True

    db = FakeDb()
    monkeypatch.setattr(label_service, "get_order", lambda _db, _order_id: order)
    monkeypatch.setattr(
        label_service,
        "_read_onec_order_rows",
        lambda _number: [
            {
                "order_number": "РБГУ0000543",
                "order_date": date(2026, 8, 3),
                "line_no": 1,
                "onec_item_code": "062852",
                "item_name": "Дисплей",
                "article_1c": "062852",
                "barcode": "2900000636873",
                "quantity": 1,
            }
        ],
    )

    source, preview = link_order_label_source(
        db,
        7,
        onec_number="РБГУ000543",
        label_size="50x40",
        settings=Settings(),
    )

    assert db.flushed is True
    assert source["origin"] == "manual"
    assert source["onec_number"] == "РБГУ0000543"
    assert order.label_onec_document_date == date(2026, 8, 3)
    assert preview["ready"] is True
