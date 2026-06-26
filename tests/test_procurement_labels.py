from __future__ import annotations

import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.procurement_labels import ProcurementLabelOrderPreview, ProcurementLabelRow
from app.services import procurement_labels


def _mapping() -> dict:
    return {
        "process": {"entity_type_id": 1056},
        "category_map": {"ved_import": {"id": 12}, "ordinary": {"id": 52}},
        "field_map": {
            "procurement_contour": "UF_CRM_8_PROCUREMENTCONTOUR",
            "onec_source_number": "UF_CRM_8_ONECSOURCENUMBER",
        },
        "enum_map": {"procurement_contour": {"ved_import": "369", "ordinary": "367"}},
    }


def _line(**overrides) -> dict:
    row = {
        "line_no": 1,
        "onec_item_code": "РБ000046282",
        "item_name": "Аккумулятор для Apple iPhone 11 F5ENERGY",
        "sku": "052841",
        "unit": "шт",
        "quantity": Decimal("160"),
        "price": Decimal("38"),
        "amount": Decimal("6080"),
    }
    row.update(overrides)
    return row


def _ready_preview() -> ProcurementLabelOrderPreview:
    return ProcurementLabelOrderPreview(
        item_id="134",
        entity_type_id=1056,
        onec_number="РБГУ0000377",
        title="ВЭД импорт РБГУ0000377",
        contour="ved_import",
        status="draft",
        ready=True,
        blocked=False,
        rows=[
            ProcurementLabelRow(
                line_no=1,
                onec_item_code="РБ000046282",
                item_name="Аккумулятор для Apple iPhone 11 F5ENERGY",
                sku="052841",
                barcode="4601234567892",
                unit="шт",
                quantity=Decimal("160"),
                price=Decimal("38"),
                amount=Decimal("6080"),
                certificate_id="ДС-TEST-1",
                certificate_status="covered",
                eac_allowed=True,
                status="ready",
                blockers=[],
            )
        ],
    )


def test_non_ved_order_is_blocked() -> None:
    preview = procurement_labels.build_preview_from_sources(
        item_id="134",
        entity_type_id=1056,
        bitrix_item={
            "categoryId": 52,
            "title": "Закупка РБГУ0000377",
            "ufCrm8Onecsourcenumber": "РБГУ0000377",
        },
        onec_lines=[_line()],
        mapping=_mapping(),
        barcode_catalog={"РБ000046282": "4601234567892"},
        certificate_catalog={"РБ000046282": {"certificate_id": "ДС-TEST-1", "status": "covered"}},
    )

    assert preview.blocked is True
    assert procurement_labels.BLOCK_NON_VED in preview.blockers


def test_missing_barcode_blocks_zip_generation() -> None:
    preview = procurement_labels.build_preview_from_sources(
        item_id="134",
        entity_type_id=1056,
        bitrix_item={
            "categoryId": 12,
            "title": "ВЭД импорт РБГУ0000377",
            "ufCrm8Onecsourcenumber": "РБГУ0000377",
        },
        onec_lines=[_line()],
        mapping=_mapping(),
        certificate_catalog={"РБ000046282": {"certificate_id": "ДС-TEST-1", "status": "covered"}},
    )

    assert preview.blocked is True
    assert procurement_labels.BLOCK_MISSING_BARCODE in preview.rows[0].blockers
    with pytest.raises(ValueError):
        procurement_labels.build_artifacts(preview, artifact_dir=Path("/tmp/noop"))


def test_eac_is_allowed_only_with_confirmed_certificate() -> None:
    covered = procurement_labels.build_preview_from_sources(
        item_id="134",
        entity_type_id=1056,
        bitrix_item={
            "categoryId": 12,
            "title": "ВЭД импорт РБГУ0000377",
            "ufCrm8Onecsourcenumber": "РБГУ0000377",
        },
        onec_lines=[_line()],
        mapping=_mapping(),
        barcode_catalog={"РБ000046282": "4601234567892"},
        certificate_catalog={
            "РБ000046282": {"certificate_id": "ДС-TEST-1", "status": "covered", "eac": True}
        },
    )
    missing = procurement_labels.build_preview_from_sources(
        item_id="134",
        entity_type_id=1056,
        bitrix_item={
            "categoryId": 12,
            "title": "ВЭД импорт РБГУ0000377",
            "ufCrm8Onecsourcenumber": "РБГУ0000377",
        },
        onec_lines=[_line()],
        mapping=_mapping(),
        barcode_catalog={"РБ000046282": "4601234567892"},
        certificate_catalog={},
    )

    assert covered.rows[0].eac_allowed is True
    assert covered.blocked is False
    assert missing.rows[0].eac_allowed is False
    assert procurement_labels.BLOCK_MISSING_CERTIFICATE in missing.rows[0].blockers


def test_repeated_generation_creates_new_zip_version(tmp_path: Path) -> None:
    preview = _ready_preview()

    v1, zip1, _ = procurement_labels.build_artifacts(preview, artifact_dir=tmp_path)
    v2, zip2, _ = procurement_labels.build_artifacts(preview, artifact_dir=tmp_path)

    assert v1 == 1
    assert v2 == 2
    assert zip1.exists()
    assert zip2.exists()
    assert zip1 != zip2
    with zipfile.ZipFile(zip2) as archive:
        names = set(archive.namelist())
    assert "label-register.xlsx" in names
    assert "factory-labels-readme.txt" in names
    assert any(name.startswith("labels-preview/") and name.endswith(".png") for name in names)
