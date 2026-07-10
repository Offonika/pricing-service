from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, PhoneModel, Product, ProductPhoneModel, ProductSkuPlan


def test_generate_product_skus_task_writes_ut103_sku_property_update(tmp_path: Path) -> None:
    db_path = tmp_path / "sku.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        product = Product(
            article="1008",
            code_1c="РБ0001008",
            name="Дисплей для Apple iPhone 12",
            brand="Apple",
            manufacturer="F5ENERGY",
            category="Дисплеи",
            display_type="OLED",
            display_quality="Copy High",
            color="Black",
        )
        phone_model = PhoneModel(brand="apple", model_name="iphone 12", variant=None)
        session.add_all([product, phone_model])
        session.flush()
        session.add(
            ProductPhoneModel(
                product_id=product.id,
                phone_model_id=phone_model.id,
                source="onec",
            )
        )
        session.commit()

    project_root = Path(__file__).resolve().parents[1]
    exchange_root = tmp_path / "exchange"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.generate_product_skus",
            "--write",
            "--write-ready",
            "--exchange-root",
            str(exchange_root),
            "--message-id",
            "sku-properties-task-test-001",
            "--changed-at",
            "2026-07-01",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=project_root,
        env={**os.environ, "DATABASE_URL": db_url},
    )

    summary = json.loads(result.stdout)
    assert summary["generated"] == 1
    assert summary["ut103_property_rows"] == 1
    assert summary["ut103_property_skipped"] == []

    output_path = Path(summary["ut103_property_path"])
    assert output_path == (
        exchange_root
        / "to_1c"
        / "new"
        / "nomenclature_properties_sku-properties-task-test-001.ready.xml"
    )
    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Header/Schema") == "nomenclature_property_updates.v1"
    assert root.findtext("Header/Mode") == "dry_run"
    assert root.findtext("Items/Item/IdempotencyKey") == (
        "nom-prop:РБ0001008:SKU:F5-DSP-IPH12-OLD-BLK-CPH:2026-07-01:r1"
    )
    assert root.findtext("Items/Item/NomenclatureCode") == "РБ0001008"
    assert root.findtext("Items/Item/TargetKind") == "requisite"
    assert root.findtext("Items/Item/PropertyName") == "SKU"
    assert root.findtext("Items/Item/ValueType") == "string"
    assert root.findtext("Items/Item/NewValue") == "F5-DSP-IPH12-OLD-BLK-CPH"

    with Session(engine) as session:
        product = session.query(Product).filter_by(article="1008").one()
        assert product.planned_sku == "F5-DSP-IPH12-OLD-BLK-CPH"
        assert product.sku_sync_status == "missing_in_1c"


def test_generate_product_skus_task_exports_existing_missing_sku(tmp_path: Path) -> None:
    db_path = tmp_path / "sku.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        product = Product(
            article="1009",
            code_1c="РБ0001009",
            name="Аккумулятор для Meizu M1 Note (BT42)",
            planned_sku="OEM-BAT-MEI-M1N-3100",
            sku_sync_status="missing_in_1c",
        )
        session.add(product)
        session.flush()
        session.add(
            ProductSkuPlan(
                product_id=product.id,
                planned_sku="OEM-BAT-MEI-M1N-3100",
                brand_code="OEM",
                category_code="BAT",
                device_code="MEI-M1N",
                key_code="3100",
                status="generated",
                source="rules",
                is_active=True,
            )
        )
        session.commit()

    project_root = Path(__file__).resolve().parents[1]
    exchange_root = tmp_path / "exchange"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.generate_product_skus",
            "--export-existing",
            "--write-ready",
            "--exchange-root",
            str(exchange_root),
            "--message-id",
            "sku-properties-existing-test-001",
            "--changed-at",
            "2026-07-03",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=project_root,
        env={**os.environ, "DATABASE_URL": db_url},
    )

    summary = json.loads(result.stdout)
    assert summary["generated"] == 0
    assert summary["existing_sku_export_items"] == 1
    assert summary["ut103_property_rows"] == 1
    assert summary["ut103_property_skipped"] == []

    output_path = Path(summary["ut103_property_path"])
    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Items/Item/IdempotencyKey") == (
        "nom-prop:РБ0001009:SKU:OEM-BAT-MEI-M1N-3100:2026-07-03:r1"
    )
    assert root.findtext("Items/Item/NomenclatureCode") == "РБ0001009"
    assert root.findtext("Items/Item/TargetKind") == "requisite"
    assert root.findtext("Items/Item/NewValue") == "OEM-BAT-MEI-M1N-3100"


def test_generate_product_skus_task_allows_empty_nightly_export(tmp_path: Path) -> None:
    db_path = tmp_path / "sku.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    project_root = Path(__file__).resolve().parents[1]
    exchange_root = tmp_path / "exchange"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.generate_product_skus",
            "--export-existing",
            "--write-ready",
            "--allow-empty",
            "--exchange-root",
            str(exchange_root),
            "--message-id",
            "sku-properties-empty-test-001",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=project_root,
        env={**os.environ, "DATABASE_URL": db_url},
    )

    summary = json.loads(result.stdout)
    assert summary["ut103_property_rows"] == 0
    assert summary["ut103_property_path"] is None
    assert not list(exchange_root.glob("**/*.xml"))
