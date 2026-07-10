from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Product

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_apply_ut103_sku_results_updates_successful_sku_result(tmp_path: Path) -> None:
    db_url = _prepare_db(
        tmp_path,
        Product(
            article="1001",
            code_1c="РБ0001001",
            name="Аккумулятор тестовый",
            planned_sku="OEM-BAT-TEST-1000",
            sku_sync_status="missing_in_1c",
        ),
    )
    exchange_root = tmp_path / "exchange"
    _write_result(
        exchange_root,
        "sku-nightly-test-001",
        """
    <ItemResult>
      <IdempotencyKey>nom-prop:РБ0001001:SKU:OEM-BAT-TEST-1000:2026-07-03:r1</IdempotencyKey>
      <NomenclatureCode>РБ0001001</NomenclatureCode>
      <PropertyName>SKU</PropertyName>
      <Result>applied</Result>
      <Message>Значение реквизита записано</Message>
      <CurrentValue></CurrentValue>
      <NewValue>OEM-BAT-TEST-1000</NewValue>
    </ItemResult>
    <ItemResult>
      <IdempotencyKey>nom-prop:РБ0001001:Предмет:2026-07-03:r1</IdempotencyKey>
      <NomenclatureCode>РБ0001001</NomenclatureCode>
      <PropertyName>Предмет</PropertyName>
      <Result>applied</Result>
      <Message>Значение свойства записано</Message>
      <CurrentValue></CurrentValue>
      <NewValue>аккумулятор</NewValue>
    </ItemResult>
""",
    )

    summary = _run_task(db_url, exchange_root)

    assert summary["files"] == 1
    assert summary["sku_items"] == 1
    assert summary["success_items"] == 1
    assert summary["updated_products"] == 1
    assert summary["error_items"] == 0

    engine = create_engine(db_url)
    with Session(engine) as session:
        product = session.query(Product).filter_by(article="1001").one()
        assert product.fact_sku == "OEM-BAT-TEST-1000"
        assert product.sku_sync_status == "match"
        assert product.sku_sync_error is None
    engine.dispose()


def test_apply_ut103_sku_results_marks_failed_sku_result(tmp_path: Path) -> None:
    db_url = _prepare_db(
        tmp_path,
        Product(
            article="1002",
            code_1c="РБ0001002",
            name="Дисплей тестовый",
            planned_sku="OEM-DSP-TEST-BLK",
            sku_sync_status="missing_in_1c",
        ),
    )
    exchange_root = tmp_path / "exchange"
    _write_result(
        exchange_root,
        "sku-nightly-test-002",
        """
    <ItemResult>
      <IdempotencyKey>nom-prop:РБ0001002:SKU:OEM-DSP-TEST-BLK:2026-07-03:r1</IdempotencyKey>
      <NomenclatureCode>РБ0001002</NomenclatureCode>
      <PropertyName>SKU</PropertyName>
      <Result>failed</Result>
      <Message>Не найден реквизит SKU</Message>
      <CurrentValue></CurrentValue>
      <NewValue>OEM-DSP-TEST-BLK</NewValue>
    </ItemResult>
""",
    )

    summary = _run_task(db_url, exchange_root)

    assert summary["sku_items"] == 1
    assert summary["success_items"] == 0
    assert summary["error_items"] == 1
    assert summary["updated_products"] == 0

    engine = create_engine(db_url)
    with Session(engine) as session:
        product = session.query(Product).filter_by(article="1002").one()
        assert product.fact_sku is None
        assert product.sku_sync_status == "error"
        assert product.sku_sync_error == "Не найден реквизит SKU"
    engine.dispose()


def _prepare_db(tmp_path: Path, *products: Product) -> str:
    db_path = tmp_path / "sku-results.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(products)
        session.commit()
    engine.dispose()
    return db_url


def _write_result(exchange_root: Path, message_id: str, item_results_xml: str) -> Path:
    result_dir = exchange_root / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    path = result_dir / f"nomenclature_properties_{message_id}.result.xml"
    path.write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>{message_id}</MessageId>
  <Status>success</Status>
  <ProcessedAt>2026-07-03T02:36:29</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <ItemResults>
{item_results_xml}
  </ItemResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )
    return path


def _run_task(db_url: str, exchange_root: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.apply_ut103_sku_results",
            "--database-url",
            db_url,
            "--exchange-root",
            str(exchange_root),
            "--json",
        ],
        check=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        text=True,
    )
    return json.loads(result.stdout)
